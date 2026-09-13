"""Token budgeting.

Pinned items - instructions - are always included. If policy alone will not fit,
this raises instead of trimming: silently dropping a rule is precisely the
failure mode the whole design exists to prevent, and the operator has to see it.

Everything else competes by score, subject to per-layer floors so a flood of
high-scoring memories cannot starve the current-state layer.

Selection is greedy but does NOT stop at the first item that does not fit: a
smaller lower-scoring item may still earn its place in the remaining space.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.context.tokens import TokenCounter, get_token_counter
from src.context.types import ContextItem, Layer


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class LayerFloors:
    """Tokens reserved for each layer before the free pool is allocated.

    Zero means no reservation, not "no room".
    """

    instructions: int = 0
    current_state: int = 0
    repository: int = 0
    memory: int = 0

    def for_layer(self, layer: Layer) -> int:
        return {
            Layer.INSTRUCTIONS: self.instructions,
            Layer.CURRENT_STATE: self.current_state,
            Layer.REPOSITORY: self.repository,
            Layer.MEMORY: self.memory,
        }[layer]


@dataclass
class BudgetReport:
    total_tokens: int = 0
    tokens_by_layer: dict[Layer, int] = field(default_factory=dict)
    included_count: int = 0
    candidate_count: int = 0
    counter_name: str = ""

    def summary(self) -> str:
        return (
            f"{self.included_count} of {self.candidate_count} items included, "
            f"{self.total_tokens} tokens (counted with {self.counter_name})"
        )


@dataclass
class BudgetResult:
    included: list[ContextItem]
    excluded: list[ContextItem]
    report: BudgetReport


class ContextBudget:
    def __init__(
        self,
        max_tokens: int,
        counter: Optional[TokenCounter] = None,
        floors: Optional[LayerFloors] = None,
    ) -> None:
        self._max_tokens = max_tokens
        self._counter = counter or get_token_counter()
        self._floors = floors or LayerFloors()

    def apply(self, items: list[ContextItem]) -> BudgetResult:
        report = BudgetReport(
            candidate_count=len(items),
            counter_name=self._counter.name,
            tokens_by_layer={layer: 0 for layer in Layer},
        )
        for item in items:
            item.tokens = self._counter.count(item.content)

        included: list[ContextItem] = []
        excluded: list[ContextItem] = []
        spent = 0

        def take(item: ContextItem) -> None:
            nonlocal spent
            included.append(item)
            spent += item.tokens
            report.tokens_by_layer[item.layer] = report.tokens_by_layer.get(item.layer, 0) + item.tokens

        pinned = [i for i in items if i.pinned]
        pinned_cost = sum(i.tokens for i in pinned)
        if pinned_cost > self._max_tokens:
            raise BudgetExceeded(
                f"pinned items need {pinned_cost} tokens but the budget is {self._max_tokens}. "
                "Instructions are policy and must not be trimmed - raise the budget or "
                "reduce the instruction set."
            )
        for item in pinned:
            take(item)

        rest = sorted((i for i in items if not i.pinned), key=lambda i: (-i.score, i.id))

        # Floors first: reserve each layer's guaranteed share.
        floor_spent: dict[Layer, int] = {layer: 0 for layer in Layer}
        taken: set[int] = set()
        for index, item in enumerate(rest):
            floor = self._floors.for_layer(item.layer)
            if floor <= 0:
                continue
            if floor_spent[item.layer] + item.tokens > floor:
                continue
            if spent + item.tokens > self._max_tokens:
                continue
            take(item)
            floor_spent[item.layer] += item.tokens
            taken.add(index)

        # Free pool. Keep scanning after a miss: a smaller item may still fit.
        for index, item in enumerate(rest):
            if index in taken:
                continue
            if spent + item.tokens <= self._max_tokens:
                take(item)
                taken.add(index)
            else:
                excluded.append(item)

        report.total_tokens = spent
        report.included_count = len(included)
        return BudgetResult(included=included, excluded=excluded, report=report)
