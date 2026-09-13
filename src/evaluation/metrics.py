"""Retrieval metrics.

Two choices here matter more than the formulas.

**Undefined is not zero.** A case with no relevant items, or an arm that returned
nothing, cannot be scored. Returning 0.0 would drag a mean down with a case that
was never scoreable and make an arm look worse than it is. These return `None`,
and the aggregator excludes them and reports how many it excluded.

**Precision divides by what was actually returned, not by k.** Dividing by k
punishes a system for returning a short but perfect list, which is exactly the
behaviour a context manager is supposed to have.
"""

from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence


def recall_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> Optional[float]:
    if not relevant:
        return None
    top = list(retrieved)[:k]
    return len(set(top) & relevant) / len(relevant)


def precision_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> Optional[float]:
    top = list(retrieved)[:k]
    if not top:
        return None
    return len(set(top) & relevant) / len(top)


def reciprocal_rank(retrieved: Sequence[str], relevant: set[str]) -> float:
    for index, item in enumerate(retrieved, start=1):
        if item in relevant:
            return 1.0 / index
    return 0.0


def ndcg_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> float:
    """Binary-relevance NDCG."""
    top = list(retrieved)[:k]
    dcg = sum(1.0 / math.log2(i + 1) for i, item in enumerate(top, start=1) if item in relevant)
    ideal_hits = min(len(relevant), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    if idcg == 0:
        return 0.0
    return dcg / idcg


def mean_defined(values: Iterable[Optional[float]]) -> tuple[Optional[float], int, int]:
    """Mean of the values that are defined.

    Returns (mean or None, defined_count, total_count) so a caller can print the
    denominator instead of silently averaging over a subset.
    """
    values = list(values)
    defined = [v for v in values if v is not None]
    if not defined:
        return None, 0, len(values)
    return sum(defined) / len(defined), len(defined), len(values)
