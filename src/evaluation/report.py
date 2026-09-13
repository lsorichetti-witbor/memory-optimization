"""Run reporting.

Every number printed here carries the methodology block from handoff section 16,
because a retrieval number without its configuration is not comparable to
anything.

The report will not print `answer_accuracy`. This harness measures retrieval and
selection only. Presenting a Recall@K as an end-to-end memory QA score is the
specific misreporting the handoff calls out, and the easiest way to avoid it is
to make the field impossible to populate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


def _fmt(value: Optional[float], places: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{places}f}"


@dataclass
class ArmResult:
    name: str
    recall_at_k: Optional[float]
    precision_at_k: Optional[float]
    mrr: float
    ndcg: float
    context_tokens: int
    memories_injected: int
    latency_ms: float
    candidates: int = 0
    scoreable_cases: int = 0
    total_cases: int = 0
    duplicate_rate: float = 0.0

    def render_row(self) -> str:
        scored = (
            f"{self.scoreable_cases}/{self.total_cases}"
            if self.total_cases
            else "-"
        )
        return (
            f"| {self.name:<22} | {_fmt(self.recall_at_k):>7} | {_fmt(self.precision_at_k):>7} | "
            f"{self.mrr:>6.3f} | {self.ndcg:>6.3f} | {self.context_tokens:>7} | "
            f"{self.memories_injected:>4} | {self.latency_ms:>8.1f} | {scored:>7} |"
        )


@dataclass
class RunReport:
    dataset: str
    dataset_size: int
    model: str
    top_k: int
    reranker: str
    token_budget: int
    runs: int
    self_reported: bool
    retrieval_only: bool = True
    notes: tuple[str, ...] = ()
    arms: list[ArmResult] = field(default_factory=list)

    def add(self, arm: ArmResult) -> None:
        self.arms.append(arm)

    def render(self) -> str:
        plural = "" if self.dataset_size == 1 else "s"
        lines = [
            "# Context Manager evaluation",
            "",
            "## Methodology",
            "",
            f"- dataset: {self.dataset} ({self.dataset_size} case{plural})",
            f"- model: {self.model}",
            f"- retrieval configuration: layers as named per arm",
            f"- top_k: {self.top_k}",
            f"- reranker: {self.reranker}",
            f"- token_budget: {self.token_budget}",
            f"- runs: {self.runs}",
            f"- self_reported: {self.self_reported}",
            "",
        ]
        if self.retrieval_only:
            lines += [
                "**This is a retrieval-only benchmark.** It measures which items the",
                "Context Manager selects, not whether an agent given that context answers",
                "correctly. Recall@K here is not an end-to-end memory QA score and must",
                "not be reported as one.",
                "",
            ]
        lines += [
            f"## Results over {self.dataset_size} case{plural}",
            "",
            "| arm                    | recall@k | prec@k |    mrr |   ndcg |  tokens | mem |  ms | scored |",
            "|------------------------|---------:|-------:|-------:|-------:|--------:|----:|----:|-------:|",
        ]
        lines += [arm.render_row() for arm in self.arms]
        lines.append("")
        lines.append(
            "`scored` is the number of cases that could be scored at all; a case whose "
            "arm returned nothing is undefined rather than zero, and is excluded from "
            "the mean rather than dragging it down."
        )
        if self.notes:
            lines += ["", "## Notes", ""]
            lines += [f"- {n}" for n in self.notes]
        return "\n".join(lines) + "\n"
