from src.context.dedupe import Deduplicator
from src.context.types import ContextItem, Layer, ScoreBreakdown


def item(id_, content, total=1.0, layer=Layer.MEMORY):
    return ContextItem(
        id=id_,
        layer=layer,
        content=content,
        source=f"{id_}.md",
        breakdown=ScoreBreakdown(total=total, contributions={}),
    )


def test_near_identical_items_collapse_to_one():
    kept, dropped = Deduplicator(threshold=0.8).run(
        [
            item("a", "The embedder provider is gemini for this deployment.", total=1.0),
            item("b", "The embedder provider is gemini for this deployment", total=0.4),
        ]
    )
    assert [i.id for i in kept] == ["a"]
    assert [i.id for i in dropped] == ["b"]


def test_the_higher_scoring_duplicate_survives():
    kept, _ = Deduplicator(threshold=0.8).run(
        [
            item("low", "same text here entirely and then some more", total=0.2),
            item("high", "same text here entirely and then some more", total=0.9),
        ]
    )
    assert [i.id for i in kept] == ["high"]


def test_distinct_items_both_survive():
    kept, dropped = Deduplicator(threshold=0.8).run(
        [
            item("a", "the embedder provider is gemini"),
            item("b", "the dashboard listens on port three thousand"),
        ]
    )
    assert len(kept) == 2
    assert dropped == []


def test_a_duplicate_across_layers_keeps_the_higher_precedence_layer():
    # Repository truth outranks a memory that merely restates it.
    kept, dropped = Deduplicator(threshold=0.8).run(
        [
            item("m", "postgres is the vector store for this stack", total=0.9, layer=Layer.MEMORY),
            item("r", "postgres is the vector store for this stack", total=0.1, layer=Layer.REPOSITORY),
        ]
    )
    assert [i.id for i in kept] == ["r"]
    assert [i.id for i in dropped] == ["m"]


def test_the_survivor_records_what_it_absorbed():
    kept, _ = Deduplicator(threshold=0.8).run(
        [
            item("a", "same text here entirely and then some more", total=0.9),
            item("b", "same text here entirely and then some more", total=0.2),
        ]
    )
    assert kept[0].metadata["absorbed"] == ["b"]


def test_duplicate_rate_is_reported_with_its_denominator():
    d = Deduplicator(threshold=0.8)
    d.run(
        [
            item("a", "one two three four five six"),
            item("b", "one two three four five six"),
            item("c", "totally different content in every respect"),
        ]
    )
    assert d.last_report.total == 3
    assert d.last_report.dropped == 1
    assert d.last_report.duplicate_rate == 1 / 3
    assert "1 of 3" in d.last_report.summary()


def test_very_short_items_are_not_collapsed_by_accident():
    kept, _ = Deduplicator(threshold=0.8).run([item("a", "ok"), item("b", "no")])
    assert len(kept) == 2


def test_identical_very_short_items_still_collapse():
    kept, _ = Deduplicator(threshold=0.8).run([item("a", "ok", total=0.9), item("b", "ok", total=0.1)])
    assert [i.id for i in kept] == ["a"]


def test_an_empty_input_is_handled():
    kept, dropped = Deduplicator().run([])
    assert kept == []
    assert dropped == []


def test_deduplication_is_deterministic_for_tied_scores():
    first, _ = Deduplicator(threshold=0.8).run(
        [item("a", "one two three four five six", total=0.5), item("b", "one two three four five six", total=0.5)]
    )
    second, _ = Deduplicator(threshold=0.8).run(
        [item("b", "one two three four five six", total=0.5), item("a", "one two three four five six", total=0.5)]
    )
    assert [i.id for i in first] == [i.id for i in second]
