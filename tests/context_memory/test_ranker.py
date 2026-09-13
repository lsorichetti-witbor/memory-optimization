import pytest

from src.context.ranker import ContextRanker, ScoringWeights
from src.context.types import ContextItem, Layer, Signals, Task


def item(id_, layer, content, **signals):
    return ContextItem(id=id_, layer=layer, content=content, source=f"{id_}.md", signals=Signals(**signals))


def test_breakdown_names_every_component():
    ranked = ContextRanker().rank([item("a", Layer.REPOSITORY, "gemini provider")], Task(description="gemini"))
    assert set(ranked[0].breakdown.contributions) == {
        "relevance",
        "task_scope_match",
        "project_scope_match",
        "confidence",
        "recency",
        "importance",
        "redundancy",
        "staleness",
    }


def test_contributions_sum_to_the_total():
    ranked = ContextRanker().rank(
        [item("a", Layer.REPOSITORY, "gemini provider", confidence=0.5)], Task(description="gemini")
    )
    assert ranked[0].breakdown.total == pytest.approx(sum(ranked[0].breakdown.contributions.values()))


def test_penalties_are_negative_contributions():
    ranked = ContextRanker().rank(
        [item("a", Layer.MEMORY, "old", staleness=1.0, redundancy=1.0)], Task(description="old")
    )
    assert ranked[0].breakdown.contributions["staleness"] < 0
    assert ranked[0].breakdown.contributions["redundancy"] < 0


def test_relevance_is_filled_in_for_non_memory_layers_from_bm25():
    items = [
        item("a", Layer.REPOSITORY, "the embedder provider is gemini"),
        item("b", Layer.REPOSITORY, "the dashboard runs on port three thousand"),
    ]
    ranked = ContextRanker().rank(items, Task(description="which embedder provider"))
    assert ranked[0].id == "a"
    assert ranked[0].signals.relevance > ranked[1].signals.relevance


def test_memory_relevance_from_the_provider_is_not_overwritten():
    # The server already scored these by embedding similarity; recomputing with
    # bm25 would silently discard the better signal.
    items = [item("m", Layer.MEMORY, "unrelated words entirely", relevance=0.9)]
    ranked = ContextRanker().rank(items, Task(description="gemini provider"))
    assert ranked[0].signals.relevance == 0.9


def test_pinned_items_sort_above_everything_regardless_of_score():
    pinned = ContextItem(id="p", layer=Layer.INSTRUCTIONS, content="zzz", source="CLAUDE.md", pinned=True)
    scored = item("a", Layer.REPOSITORY, "gemini gemini gemini", confidence=1.0, importance=1.0)
    ranked = ContextRanker().rank([scored, pinned], Task(description="gemini"))
    assert ranked[0].id == "p"


def test_weights_are_injectable_and_change_the_order():
    a = item("a", Layer.MEMORY, "x", relevance=0.9, importance=0.0)
    b = item("b", Layer.MEMORY, "x", relevance=0.0, importance=0.9)
    relevance_first = ContextRanker(weights=ScoringWeights(relevance=2.0, importance=0.1)).rank(
        [a, b], Task(description="x")
    )
    importance_first = ContextRanker(weights=ScoringWeights(relevance=0.1, importance=2.0)).rank(
        [a, b], Task(description="x")
    )
    assert relevance_first[0].id == "a"
    assert importance_first[0].id == "b"


def test_default_weights_are_documented_as_unmeasured():
    # Handoff section 12: do not invent final weights before measurement.
    assert "unmeasured" in ScoringWeights.__doc__.lower()


def test_ranking_is_deterministic_for_tied_scores():
    a = item("a", Layer.MEMORY, "same", relevance=0.5)
    b = item("b", Layer.MEMORY, "same", relevance=0.5)
    first = [i.id for i in ContextRanker().rank([a, b], Task(description="same"))]
    second = [i.id for i in ContextRanker().rank([b, a], Task(description="same"))]
    assert first == second


def test_task_scope_match_rewards_an_item_scoped_to_the_task_repository():
    on_repo = ContextItem(
        id="a",
        layer=Layer.MEMORY,
        content="x",
        source="mem0",
        metadata={"scope": "repository", "scope_key": "memory-optimization"},
    )
    elsewhere = ContextItem(
        id="b",
        layer=Layer.MEMORY,
        content="x",
        source="mem0",
        metadata={"scope": "repository", "scope_key": "other-repo"},
    )
    ranked = ContextRanker().rank([elsewhere, on_repo], Task(description="x", repository="memory-optimization"))
    assert ranked[0].id == "a"


def test_a_global_memory_is_not_penalised_for_not_naming_a_repository():
    # Global knowledge is legitimately repository-agnostic.
    global_item = ContextItem(
        id="g", layer=Layer.MEMORY, content="x", source="mem0", metadata={"scope": "global", "scope_key": "global"}
    )
    ranked = ContextRanker().rank([global_item], Task(description="x", repository="memory-optimization"))
    assert ranked[0].breakdown.contributions["project_scope_match"] >= 0.0


def test_ranking_leaves_the_input_list_untouched():
    original = [item("a", Layer.REPOSITORY, "x")]
    ContextRanker().rank(original, Task(description="x"))
    assert original[0].breakdown is None


def test_every_item_comes_back_ranked():
    items = [item(f"i{n}", Layer.REPOSITORY, "x") for n in range(5)]
    ranked = ContextRanker().rank(items, Task(description="x"))
    assert len(ranked) == 5
    assert all(i.breakdown is not None for i in ranked)
