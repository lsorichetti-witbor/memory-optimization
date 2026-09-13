import pytest

from src.context.budget import BudgetExceeded, ContextBudget, LayerFloors
from src.context.tokens import HeuristicTokenCounter
from src.context.types import ContextItem, Layer, ScoreBreakdown


def item(id_, layer, chars, total=1.0, pinned=False):
    return ContextItem(
        id=id_,
        layer=layer,
        content="x" * chars,
        source=id_,
        pinned=pinned,
        breakdown=ScoreBreakdown(total=total, contributions={}),
    )


def budget(max_tokens, floors=None):
    return ContextBudget(max_tokens=max_tokens, counter=HeuristicTokenCounter(), floors=floors or LayerFloors())


def test_items_are_included_in_score_order_until_the_cap():
    # 200 chars = 50 tokens each, so a 60 token cap has room for exactly one.
    result = budget(60).apply(
        [item("low", Layer.MEMORY, 200, total=0.1), item("high", Layer.MEMORY, 200, total=0.9)]
    )
    assert [i.id for i in result.included] == ["high"]
    assert [i.id for i in result.excluded] == ["low"]


def test_pinned_items_are_always_included():
    result = budget(60).apply(
        [
            item("pin", Layer.INSTRUCTIONS, 200, total=0.0, pinned=True),
            item("mem", Layer.MEMORY, 200, total=1.0),
        ]
    )
    assert "pin" in [i.id for i in result.included]
    assert "mem" in [i.id for i in result.excluded]


def test_pinned_content_over_the_cap_raises_rather_than_trimming_policy():
    with pytest.raises(BudgetExceeded, match="pinned"):
        budget(10).apply([item("pin", Layer.INSTRUCTIONS, 400, pinned=True)])


def test_per_layer_floor_reserves_room_for_current_state():
    floors = LayerFloors(current_state=30)
    result = ContextBudget(max_tokens=60, counter=HeuristicTokenCounter(), floors=floors).apply(
        [
            item("m1", Layer.MEMORY, 200, total=0.99),
            item("state", Layer.CURRENT_STATE, 80, total=0.01),
        ]
    )
    assert "state" in [i.id for i in result.included]


def test_report_prints_the_denominator():
    result = budget(40).apply([item(f"m{i}", Layer.MEMORY, 100, total=1 - i / 10) for i in range(10)])
    assert result.report.summary().startswith(f"{len(result.included)} of 10 items included")


def test_report_breaks_down_tokens_by_layer():
    result = budget(1000).apply([item("a", Layer.MEMORY, 40), item("b", Layer.REPOSITORY, 80)])
    assert result.report.tokens_by_layer[Layer.MEMORY] == 10
    assert result.report.tokens_by_layer[Layer.REPOSITORY] == 20


def test_report_names_how_it_counted_tokens():
    result = budget(1000).apply([item("a", Layer.MEMORY, 40)])
    assert result.report.counter_name == "heuristic"


def test_excluded_items_are_returned_not_discarded():
    result = budget(20).apply(
        [item("a", Layer.MEMORY, 400, total=0.9), item("b", Layer.MEMORY, 400, total=0.1)]
    )
    assert {i.id for i in result.included} | {i.id for i in result.excluded} == {"a", "b"}


def test_an_empty_input_produces_an_empty_but_valid_result():
    result = budget(100).apply([])
    assert result.included == []
    assert result.report.total_tokens == 0
    assert result.report.candidate_count == 0


def test_included_items_carry_their_measured_token_cost():
    result = budget(1000).apply([item("a", Layer.MEMORY, 40)])
    assert result.included[0].tokens == 10


def test_a_smaller_item_can_still_fit_after_a_larger_one_was_skipped():
    # Greedy-by-score must not stop at the first item that does not fit.
    result = budget(30).apply(
        [item("big", Layer.MEMORY, 400, total=0.9), item("small", Layer.MEMORY, 40, total=0.5)]
    )
    assert [i.id for i in result.included] == ["small"]
