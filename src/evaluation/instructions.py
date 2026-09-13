"""Rule-coverage benchmark for an instruction set.

Handoff section 19.6 asks, after refactoring `CLAUDE.md`, whether any important
invariant was lost and whether task success regressed. This measures the first
question and is explicit about not measuring the second.

**What it measures.** For each scenario: is the governing rule still *present*
anywhere in the instruction set, and does the Context Manager still *select* it
into the budget for that task.

**What it does not measure.** Whether an agent given the new instruction set
behaves better or worse. That needs an agent-task benchmark with graded
outcomes, which this is not. A refactor can pass every check here and still
change behaviour, because wording matters and this only checks that the words
are reachable.

The distinction between `present` and `selected` is the point. A rule can
survive a refactor intact and still never reach the agent - moved into a file
that ranks below the budget cut, or buried in a chunk too large to fit. Checking
presence alone would call that a success.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from src.context.budget import BudgetExceeded
from src.context.manager import ContextManager, ContextRequest
from src.context.sources.instructions import InstructionsSource
from src.context.tokens import TokenCounter, get_token_counter
from src.context.types import Layer, Task

DEFAULT_MAX_TOKENS = 8000


@dataclass(frozen=True)
class RuleCase:
    """One invariant, and the task that should surface it."""

    id: str
    task: str
    invariant: str
    must_match: tuple[str, ...]
    files: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.must_match:
            raise ValueError(
                f"case {self.id!r} has an empty must_match: it would score every "
                "instruction set identically and measure nothing"
            )

    def matches(self, text: str) -> bool:
        """Every marker must appear in ONE chunk.

        Split across two unrelated rules the invariant is not intact anywhere,
        and counting it as present would hide exactly the kind of loss a
        refactor causes.
        """
        lowered = text.lower()
        return all(marker.lower() in lowered for marker in self.must_match)


@dataclass
class CoverageResult:
    case_id: str
    present: bool
    selected: bool
    matched_source: Optional[str] = None
    # True when the pinned instruction set alone exceeded the budget. The build
    # raises rather than trimming policy, which is correct product behaviour and
    # is itself a regression worth reporting: an instruction set that cannot fit
    # its own budget reaches the agent as an error, not as policy.
    budget_exceeded: bool = False


@dataclass
class InstructionSetReport:
    name: str
    tokens: int
    results: list[CoverageResult] = field(default_factory=list)

    @property
    def present_count(self) -> int:
        return sum(1 for r in self.results if r.present)

    @property
    def selected_count(self) -> int:
        return sum(1 for r in self.results if r.selected)

    def summary(self) -> str:
        total = len(self.results)
        return (
            f"{self.name}: {self.present_count} of {total} rules present, "
            f"{self.selected_count} of {total} selected, "
            f"{self.tokens} instruction tokens"
        )


@dataclass
class ComparisonReport:
    before: InstructionSetReport
    after: InstructionSetReport
    lost: list[str] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)
    gained: list[str] = field(default_factory=list)

    @property
    def regressed(self) -> bool:
        return bool(self.lost or self.unreachable)

    @property
    def token_delta(self) -> int:
        return self.after.tokens - self.before.tokens

    @property
    def token_delta_pct(self) -> float:
        if not self.before.tokens:
            return 0.0
        return 100.0 * self.token_delta / self.before.tokens

    def summary(self) -> str:
        lines = [
            self.before.summary(),
            self.after.summary(),
            "",
            f"instruction tokens: {self.before.tokens} -> {self.after.tokens} "
            f"({self.token_delta:+d}, {self.token_delta_pct:+.1f}%)",
        ]
        if self.lost:
            lines.append(f"REGRESSION - rules lost entirely: {', '.join(self.lost)}")
        if self.unreachable:
            lines.append(
                f"REGRESSION - rules still present but no longer selected: {', '.join(self.unreachable)}"
            )
        if self.gained:
            lines.append(f"newly selected: {', '.join(self.gained)}")
        if not self.regressed:
            # Saying "no regression" is only honest about what was checked.
            lines.append(
                "No rule was lost or made unreachable. This does NOT establish that "
                "agent behaviour is unchanged - see the module docstring."
            )
        return "\n".join(lines)


def evaluate_instruction_set(
    root: Path,
    cases: Sequence[RuleCase],
    name: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    counter: Optional[TokenCounter] = None,
) -> InstructionSetReport:
    """Score one instruction set. `root` is a directory laid out like a repo."""
    root = Path(root)
    counter = counter or get_token_counter()

    source = InstructionsSource(root=root)
    # Presence is judged against everything the source can see, for a task that
    # names nothing in particular, so it does not depend on ranking.
    all_items = source.collect(Task(description=" ".join(c.task for c in cases)))
    tokens = sum(counter.count(item.content) for item in all_items)

    report = InstructionSetReport(name=name, tokens=tokens)

    for case in cases:
        present_in = next((i for i in all_items if case.matches(f"{i.title}\n{i.content}")), None)

        selected = False
        budget_exceeded = False
        if present_in is not None:
            manager = ContextManager(
                sources=[InstructionsSource(root=root)],
                max_tokens=max_tokens,
                counter=counter,
            )
            try:
                built = manager.build(
                    ContextRequest(
                        task=Task(description=case.task, files=case.files),
                        max_tokens=max_tokens,
                    )
                )
            except BudgetExceeded:
                # The build raises rather than trimming policy, which is correct.
                # For this benchmark it means the rule did not reach the agent,
                # and an instruction set that cannot fit its own budget is itself
                # a regression worth reporting rather than a crash.
                built = None
                budget_exceeded = True

            if built is not None:
                selected = any(
                    item.layer is Layer.INSTRUCTIONS and case.matches(f"{item.title}\n{item.content}")
                    for item in built.items
                )

        report.results.append(
            CoverageResult(
                case_id=case.id,
                present=present_in is not None,
                selected=selected,
                matched_source=present_in.source if present_in else None,
                budget_exceeded=budget_exceeded,
            )
        )

    return report


def compare(before: InstructionSetReport, after: InstructionSetReport) -> ComparisonReport:
    by_id_before = {r.case_id: r for r in before.results}
    by_id_after = {r.case_id: r for r in after.results}

    lost = [cid for cid, r in by_id_before.items() if r.present and not by_id_after.get(cid, r).present]
    unreachable = [
        cid
        for cid, r in by_id_before.items()
        if r.selected and by_id_after.get(cid, r).present and not by_id_after[cid].selected
    ]
    gained = [
        cid
        for cid, r in by_id_after.items()
        if r.selected and cid in by_id_before and not by_id_before[cid].selected
    ]
    return ComparisonReport(
        before=before, after=after, lost=sorted(lost), unreachable=sorted(unreachable), gained=sorted(gained)
    )
