from src.context.chunking import chunk_markdown

DOC = """# Title

Intro line.

## Section A

Alpha body.

### Section A.1

Nested body.

## Section B

Beta body.
"""


def test_chunks_split_on_headings():
    chunks = chunk_markdown(DOC, source="CLAUDE.md")
    assert [c.title for c in chunks] == [
        "Title",
        "Title > Section A",
        "Title > Section A > Section A.1",
        "Title > Section B",
    ]


def test_chunk_content_excludes_deeper_sections():
    chunks = {c.title: c.content for c in chunk_markdown(DOC, source="CLAUDE.md")}
    assert "Nested body" not in chunks["Title > Section A"]
    assert "Alpha body" in chunks["Title > Section A"]


def test_chunk_ids_are_stable_and_unique_per_heading_path():
    ids = [c.id for c in chunk_markdown(DOC, source="CLAUDE.md")]
    assert len(set(ids)) == len(ids)
    assert ids == [c.id for c in chunk_markdown(DOC, source="CLAUDE.md")]


def test_two_files_with_identical_headings_get_distinct_ids():
    # Ids that share a generated prefix and differ only at the tail collapse into
    # one label when truncated; keep the distinguishing part in the id itself.
    a = chunk_markdown(DOC, source="a/CLAUDE.md")
    b = chunk_markdown(DOC, source="b/CLAUDE.md")
    assert set(c.id for c in a).isdisjoint(c.id for c in b)


def test_every_id_resolves_back_to_exactly_one_chunk():
    chunks = chunk_markdown(DOC, source="a/CLAUDE.md") + chunk_markdown(DOC, source="b/CLAUDE.md")
    by_id = {}
    for chunk in chunks:
        by_id.setdefault(chunk.id, []).append(chunk)
    assert all(len(v) == 1 for v in by_id.values())


def test_code_fences_do_not_start_a_new_chunk():
    doc = "## S\n\n```python\n# not a heading\n```\n\ntail\n"
    chunks = chunk_markdown(doc, source="x.md")
    assert len(chunks) == 1
    assert "# not a heading" in chunks[0].content


def test_a_document_with_no_headings_yields_one_chunk():
    chunks = chunk_markdown("just prose\nmore prose\n", source="x.md")
    assert len(chunks) == 1
    assert chunks[0].title == "x.md"


def test_an_empty_document_yields_no_chunks():
    assert chunk_markdown("   \n\n", source="x.md") == []


def test_oversized_sections_are_split_with_an_ordinal_suffix():
    doc = "## Big\n\n" + "\n\n".join(f"paragraph {i}" for i in range(200))
    chunks = chunk_markdown(doc, source="x.md", max_chars=400)
    assert len(chunks) > 1
    assert chunks[0].title.endswith(f"(1/{len(chunks)})")
    assert chunks[-1].title.endswith(f"({len(chunks)}/{len(chunks)})")


def test_splitting_an_oversized_section_loses_no_text():
    doc = "## Big\n\n" + "\n\n".join(f"paragraph {i}" for i in range(50))
    chunks = chunk_markdown(doc, source="x.md", max_chars=300)
    joined = " ".join(c.content for c in chunks)
    for i in range(50):
        assert f"paragraph {i}" in joined
