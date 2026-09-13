"""The Context Manager: collect -> rank -> dedupe -> resolve -> budget -> compile.

The output is a compiled context plus a report that accounts for every
candidate. `candidates == injected + dropped_duplicate + dropped_budget` holds
by construction, so a context that came back thin can be explained rather than
guessed at.

`degraded` is the honest signal: a source that errored produced no items, and
that is not the same thing as a source that had nothing to say. Without the
flag, a dead Mem0 server looks exactly like an empty memory store.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from src.context.budget import BudgetReport, ContextBudget, LayerFloors
from src.context.compiler import CompiledContext, ContextCompiler
from src.context.conflicts import ConflictReport, ConflictResolver
from src.context.dedupe import Deduplicator
from src.context.ranker import ContextRanker, ScoringWeights
from src.context.sources.base import ContextSource
from src.context.sources.current_state import CurrentStateSource
from src.context.sources.instructions import InstructionsSource
from src.context.sources.memory import MemoryContextSource
from src.context.sources.repository import RepositorySource
from src.context.tokens import TokenCounter, get_token_counter
from src.context.types import ContextItem, Task

DEFAULT_MAX_TOKENS = 8000


@dataclass
class BuildReport:
    candidates: int = 0
    injected: int = 0
    dropped_duplicate: int = 0
    dropped_budget: int = 0
    duplicate_rate: float = 0.0
    degraded: bool = False
    source_errors: dict[str, str] = field(default_factory=dict)
    source_counts: dict[str, int] = field(default_factory=dict)
    stages: dict[str, float] = field(default_factory=dict)
    conflict_report: Optional[ConflictReport] = None
    budget_report: Optional[BudgetReport] = None

    def summary(self) -> str:
        lines = [
            f"{self.injected} of {self.candidates} candidates injected "
            f"({self.dropped_duplicate} duplicate, {self.dropped_budget} over budget)"
        ]
        if self.budget_report:
            lines.append(self.budget_report.summary())
        if self.conflict_report:
            lines.append(self.conflict_report.summary())
        if self.degraded:
            for name, error in self.source_errors.items():
                lines.append(f"DEGRADED: source {name!r} failed: {error}")
        return "\n".join(lines)


@dataclass(frozen=True)
class ContextRequest:
    task: Task
    max_tokens: Optional[int] = None
    weights: Optional[ScoringWeights] = None


@dataclass
class BuildResult:
    text: str
    items: list[ContextItem]
    report: BuildReport


class ContextManager:
    def __init__(
        self,
        sources: Sequence[ContextSource],
        max_tokens: int = DEFAULT_MAX_TOKENS,
        weights: Optional[ScoringWeights] = None,
        floors: Optional[LayerFloors] = None,
        counter: Optional[TokenCounter] = None,
        dedupe_threshold: float = 0.85,
    ) -> None:
        self._sources = list(sources)
        self._max_tokens = max_tokens
        self._weights = weights
        self._floors = floors or LayerFloors()
        self._counter = counter or get_token_counter()
        self._dedupe_threshold = dedupe_threshold

    @classmethod
    def from_repo(
        cls,
        root: Path,
        memory_source: Optional[MemoryContextSource] = None,
        **kwargs,
    ) -> "ContextManager":
        sources: list[ContextSource] = [
            InstructionsSource(root=root),
            CurrentStateSource(root=root),
            RepositorySource(root=root),
        ]
        if memory_source is not None:
            sources.append(memory_source)
        return cls(sources=sources, **kwargs)

    def build(self, request: ContextRequest) -> BuildResult:
        report = BuildReport()
        task = request.task

        started = time.perf_counter()
        candidates: list[ContextItem] = []
        for source in self._sources:
            produced = source.collect(task)
            report.source_counts[source.name] = len(produced)
            if source.last_error is not None:
                report.source_errors[source.name] = str(source.last_error)
                report.degraded = True
            candidates.extend(produced)
        report.candidates = len(candidates)
        report.stages["collect"] = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        ranker = ContextRanker(weights=request.weights or self._weights)
        ranked = ranker.rank(candidates, task)
        report.stages["rank"] = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        deduplicator = Deduplicator(threshold=self._dedupe_threshold)
        kept, dropped = deduplicator.run(ranked)
        report.dropped_duplicate = len(dropped)
        report.duplicate_rate = deduplicator.last_report.duplicate_rate
        report.stages["dedupe"] = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        resolved, conflict_report = ConflictResolver().run(kept)
        report.conflict_report = conflict_report
        # Staleness changed, so the scores that depended on it are stale too.
        resolved = ranker.rank(resolved, task)
        report.stages["conflicts"] = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        budget = ContextBudget(
            max_tokens=request.max_tokens or self._max_tokens,
            counter=self._counter,
            floors=self._floors,
        )
        budgeted = budget.apply(resolved)
        report.budget_report = budgeted.report
        report.dropped_budget = len(budgeted.excluded)
        report.injected = len(budgeted.included)
        report.stages["budget"] = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        compiled: CompiledContext = ContextCompiler().compile(budgeted.included)
        report.stages["compile"] = (time.perf_counter() - started) * 1000

        return BuildResult(text=compiled.text, items=compiled.items, report=report)
