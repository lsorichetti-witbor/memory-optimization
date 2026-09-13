"""Multi-signal ranking.

Handoff section 12: relevance + task_scope_match + project_scope_match +
confidence + recency + importance - redundancy - staleness. Every component is
kept separately in the returned `ScoreBreakdown` so it can be attributed and
evaluated on its own, rather than disappearing into one opaque number.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace

from src.context.lexical import Bm25
from src.context.types import ContextItem, Layer, ScoreBreakdown, Task

_POSITIVE = ("relevance", "task_scope_match", "project_scope_match", "confidence", "recency", "importance")
_NEGATIVE = ("redundancy", "staleness")


@dataclass(frozen=True)
class ScoringWeights:
    """Per-component weights.

    These defaults are UNMEASURED placeholders. Handoff section 12 is explicit
    that final weights must not be invented before measurement, so every
    component starts at 1.0 and the evaluation harness (phase 6) is what turns
    them into measured values. Do not tune them by eye against a single task -
    that is fitting the tolerance to the result rather than deriving the target.
    """

    relevance: float = 1.0
    task_scope_match: float = 1.0
    project_scope_match: float = 1.0
    confidence: float = 1.0
    recency: float = 1.0
    importance: float = 1.0
    redundancy: float = 1.0
    staleness: float = 1.0


class ContextRanker:
    def __init__(self, weights: ScoringWeights | None = None) -> None:
        self._weights = weights or ScoringWeights()

    @staticmethod
    def _scope_matches(item: ContextItem, task: Task) -> tuple[float, float]:
        """(task_scope_match, project_scope_match) in [0, 1].

        A global memory carries no repository key by design, so it scores the
        neutral 0.0 rather than being penalised for repository-agnostic scope.
        """
        scope = item.metadata.get("scope")
        key = item.metadata.get("scope_key")

        task_match = 0.0
        if task.files and item.source in task.files:
            task_match = 1.0
        elif scope in {"task", "session", "branch"} and key:
            task_match = 1.0 if key in (task.branch, task.description) else 0.0

        project_match = 0.0
        if scope in {"repository", "project"} and key and task.repository:
            project_match = 1.0 if key == task.repository else 0.0

        return task_match, project_match

    def rank(self, items: list[ContextItem], task: Task) -> list[ContextItem]:
        """Score every item. The input list is left untouched."""
        ranked = [copy.deepcopy(item) for item in items]

        # Relevance for the file layers only. Memory items already carry the
        # server's embedding similarity, which is the better signal.
        lexical_targets = [i for i in ranked if i.layer is not Layer.MEMORY]
        if lexical_targets:
            corpus = {i.id: f"{i.title}\n{i.content}" for i in lexical_targets}
            scores = Bm25(corpus).score(task.query_text())
            for item in lexical_targets:
                item.signals = replace(item.signals, relevance=scores.get(item.id, 0.0))

        for item in ranked:
            task_match, project_match = self._scope_matches(item, task)
            item.signals = replace(
                item.signals, task_scope_match=task_match, project_scope_match=project_match
            )
            contributions: dict[str, float] = {}
            for name in _POSITIVE:
                contributions[name] = getattr(self._weights, name) * getattr(item.signals, name)
            for name in _NEGATIVE:
                contributions[name] = -getattr(self._weights, name) * getattr(item.signals, name)
            item.breakdown = ScoreBreakdown(total=sum(contributions.values()), contributions=contributions)

        # Pinned first, then score, then id: the id tiebreak keeps the order
        # stable regardless of the order candidates arrived in.
        ranked.sort(key=lambda i: (not i.pinned, -i.breakdown.total, i.id))
        return ranked
