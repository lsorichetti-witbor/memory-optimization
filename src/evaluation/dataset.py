"""Benchmark cases.

This ships a small, repo-local dataset, NOT LongMemEval. That is a deliberate
limit and it is stated everywhere the results are printed: a weight tuned on
four questions about one repository has not been measured on the same basis as
a published benchmark number, and treating it as though it had is exactly the
mistake of inheriting a target from a different measurement basis.

`EvalDataset` is the seam: a LongMemEval / BEAM loader produces the same type,
and the runner does not change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class EvalCase:
    id: str
    question: str
    relevant: frozenset[str]
    answer_contains: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    repository: str | None = None

    def __post_init__(self) -> None:
        if not self.relevant:
            # A case with no ground truth scores every arm identically and so
            # measures nothing, while still counting towards the total.
            raise ValueError(f"case {self.id!r} names no relevant items")


@dataclass(frozen=True)
class EvalDataset:
    name: str
    description: str
    cases: tuple[EvalCase, ...]
    source: str = "repo-local"

    def __post_init__(self) -> None:
        ids = [c.id for c in self.cases]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"duplicate case ids: {', '.join(sorted(duplicates))}")

    @property
    def size(self) -> int:
        return len(self.cases)

    def provenance(self) -> str:
        plural = "" if self.size == 1 else "s"
        return f"{self.name} ({self.source}): {self.size} case{plural} - {self.description}"

    @classmethod
    def from_json(cls, path: Path) -> "EvalDataset":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        cases = tuple(
            EvalCase(
                id=c["id"],
                question=c["question"],
                relevant=frozenset(c["relevant"]),
                answer_contains=tuple(c.get("answer_contains", ())),
                files=tuple(c.get("files", ())),
                repository=c.get("repository"),
            )
            for c in data["cases"]
        )
        return cls(
            name=data["name"],
            description=data["description"],
            cases=cases,
            source=data.get("source", "repo-local"),
        )
