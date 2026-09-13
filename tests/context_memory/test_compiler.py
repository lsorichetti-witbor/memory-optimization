from src.context.compiler import ContextCompiler
from src.context.types import ContextItem, Layer, Signals


def item(id_, layer, content, source=None, **kw):
    return ContextItem(id=id_, layer=layer, content=content, source=source or f"{id_}.md", **kw)


def test_sections_appear_in_precedence_order():
    text = ContextCompiler().compile(
        [
            item("m", Layer.MEMORY, "a memory"),
            item("r", Layer.REPOSITORY, "repo truth"),
            item("s", Layer.CURRENT_STATE, "git state"),
            item("i", Layer.INSTRUCTIONS, "a rule"),
        ]
    ).text
    assert text.index("a rule") < text.index("git state") < text.index("repo truth") < text.index("a memory")


def test_every_item_carries_its_source():
    text = ContextCompiler().compile([item("r", Layer.REPOSITORY, "x", source="docs/a.md")]).text
    assert "docs/a.md" in text


def test_stale_memories_are_rendered_with_an_explicit_warning():
    text = ContextCompiler().compile(
        [
            item(
                "m",
                Layer.MEMORY,
                "Project uses Redis.",
                signals=Signals(staleness=1.0),
                metadata={"overruled_by": "r"},
            )
        ]
    ).text
    assert "STALE" in text
    assert "overruled by r" in text


def test_memories_are_rendered_with_confidence_and_age():
    text = ContextCompiler().compile(
        [item("m", Layer.MEMORY, "a lesson", signals=Signals(confidence=0.4), metadata={"age_days": 120})]
    ).text
    assert "confidence 0.4" in text
    assert "120d" in text


def test_an_empty_layer_produces_no_heading():
    text = ContextCompiler().compile([item("i", Layer.INSTRUCTIONS, "a rule")]).text
    assert "Memory" not in text


def test_compiling_nothing_yields_an_explicit_empty_marker_not_a_blank_string():
    # A blank context is indistinguishable from a failed build otherwise.
    result = ContextCompiler().compile([])
    assert "no context selected" in result.text.lower()


def test_the_compiled_result_keeps_the_items_it_rendered():
    items = [item("i", Layer.INSTRUCTIONS, "a rule"), item("m", Layer.MEMORY, "a memory")]
    result = ContextCompiler().compile(items)
    assert [i.id for i in result.items] == ["i", "m"]


def test_instruction_items_are_not_annotated_with_confidence():
    # Policy is not probabilistic; a confidence score next to a rule invites
    # treating it as optional.
    text = ContextCompiler().compile(
        [item("i", Layer.INSTRUCTIONS, "a rule", signals=Signals(confidence=1.0))]
    ).text
    assert "confidence" not in text


def test_item_titles_are_rendered_when_present():
    text = ContextCompiler().compile([item("r", Layer.REPOSITORY, "body", title="README > Setup")]).text
    assert "README > Setup" in text
