"""Deduplication by word-shingle Jaccard similarity.

Duplicates cost budget twice and say nothing new. Which copy survives is not
arbitrary: the higher-precedence layer wins first, score only breaks the tie.
A memory that merely restates the README should lose to the README.

The survivor records what it absorbed so the drop is traceable rather than
invisible.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.context.lexical import tokenize
from src.context.types import ContextItem, Layer

SHINGLE_SIZE = 3
# Below this many tokens, shingling is meaningless and near-matches are noise,
# so short items only collapse on exact equality.
MIN_TOKENS_FOR_SHINGLES = 5


@dataclass
class DedupeReport:
    total: int = 0
    kept: int = 0
    dropped: int = 0
    duplicate_rate: float = 0.0
    groups: dict[str, list[str]] = field(default_factory=dict)

    def summary(self) -> str:
        return f"{self.dropped} of {self.total} items dropped as duplicates ({self.duplicate_rate:.0%})"


def _shingles(text: str) -> frozenset[tuple[str, ...]]:
    tokens = tokenize(text)
    if len(tokens) < MIN_TOKENS_FOR_SHINGLES:
        return frozenset({tuple(tokens)})
    return frozenset(tuple(tokens[i : i + SHINGLE_SIZE]) for i in range(len(tokens) - SHINGLE_SIZE + 1))


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


class Deduplicator:
    def __init__(self, threshold: float = 0.85) -> None:
        self._threshold = threshold
        self.last_report = DedupeReport()

    def run(self, items: list[ContextItem]) -> tuple[list[ContextItem], list[ContextItem]]:
        self.last_report = DedupeReport(total=len(items))
        if not items:
            return [], []

        # Highest precedence layer first, then highest score, then id for
        # determinism. The first member of a group is the survivor.
        ordered = sorted(
            items,
            key=lambda i: (Layer.precedence_index(i.layer), -i.score, i.id),
        )

        survivors: list[ContextItem] = []
        signatures: list[frozenset] = []
        dropped: list[ContextItem] = []

        for item in ordered:
            signature = _shingles(item.content)
            match_index = None
            for index, existing in enumerate(signatures):
                if _jaccard(signature, existing) >= self._threshold:
                    match_index = index
                    break
            if match_index is None:
                survivors.append(item)
                signatures.append(signature)
                continue
            winner = survivors[match_index]
            winner.metadata.setdefault("absorbed", []).append(item.id)
            self.last_report.groups.setdefault(winner.id, []).append(item.id)
            dropped.append(item)

        self.last_report.kept = len(survivors)
        self.last_report.dropped = len(dropped)
        self.last_report.duplicate_rate = len(dropped) / len(items)
        return survivors, dropped
