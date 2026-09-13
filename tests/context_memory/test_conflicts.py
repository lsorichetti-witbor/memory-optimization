from datetime import datetime, timezone

from src.context.conflicts import ConflictResolver
from src.context.types import ContextItem, Layer


def item(id_, layer, content, topic=None, created_at=None, **metadata):
    md = dict(metadata)
    if topic:
        md["topic"] = topic
    return ContextItem(id=id_, layer=layer, content=content, source=id_, metadata=md, created_at=created_at)


def test_a_memory_contradicting_repository_truth_on_the_same_topic_is_marked_stale():
    repo = item("r", Layer.REPOSITORY, "PostgreSQL is the vector store.", topic="vector_store")
    memory = item("m", Layer.MEMORY, "Project uses Redis.", topic="vector_store")
    resolved, report = ConflictResolver().run([memory, repo])
    by_id = {i.id: i for i in resolved}
    assert by_id["m"].signals.staleness == 1.0
    assert by_id["r"].signals.staleness == 0.0
    assert report.conflicts[0].loser == "m"
    assert report.conflicts[0].winner == "r"


def test_the_stale_memory_is_kept_not_deleted():
    repo = item("r", Layer.REPOSITORY, "PostgreSQL.", topic="vector_store")
    memory = item("m", Layer.MEMORY, "Redis.", topic="vector_store")
    resolved, _ = ConflictResolver().run([memory, repo])
    assert {i.id for i in resolved} == {"m", "r"}


def test_the_loser_records_what_overruled_it():
    repo = item("r", Layer.REPOSITORY, "PostgreSQL.", topic="vector_store")
    memory = item("m", Layer.MEMORY, "Redis.", topic="vector_store")
    resolved, _ = ConflictResolver().run([memory, repo])
    loser = next(i for i in resolved if i.id == "m")
    assert loser.metadata["overruled_by"] == "r"


def test_two_memories_on_the_same_topic_leave_the_newer_one_authoritative():
    old = item(
        "old", Layer.MEMORY, "a", topic="t", created_at=datetime(2025, 1, 1, tzinfo=timezone.utc)
    )
    new = item(
        "new", Layer.MEMORY, "b", topic="t", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    resolved, _ = ConflictResolver().run([old, new])
    by_id = {i.id: i for i in resolved}
    assert by_id["old"].signals.staleness == 1.0
    assert by_id["new"].signals.staleness == 0.0


def test_an_explicitly_superseded_memory_is_marked_regardless_of_topic():
    memory = item("m", Layer.MEMORY, "old approach", superseded_by="m9")
    resolved, report = ConflictResolver().run([memory])
    assert resolved[0].signals.staleness == 1.0
    assert report.conflicts[0].rule == "explicit_supersession"


def test_instructions_are_never_marked_stale_by_anything():
    rule = item("i", Layer.INSTRUCTIONS, "Always use pnpm.", topic="package_manager")
    memory = item("m", Layer.MEMORY, "npm was used here once.", topic="package_manager")
    resolved, _ = ConflictResolver().run([rule, memory])
    assert next(i for i in resolved if i.id == "i").signals.staleness == 0.0
    assert next(i for i in resolved if i.id == "m").signals.staleness == 1.0


def test_repository_items_are_never_marked_stale_by_a_memory():
    repo = item("r", Layer.REPOSITORY, "x", topic="t")
    memory = item("m", Layer.MEMORY, "y", topic="t")
    resolved, _ = ConflictResolver().run([repo, memory])
    assert next(i for i in resolved if i.id == "r").signals.staleness == 0.0


def test_memories_no_rule_could_check_are_counted_not_ignored():
    # The silent direction: an undetected contradiction looks exactly like agreement.
    memory = item("m", Layer.MEMORY, "Something with no topic key at all.")
    repo = item("r", Layer.REPOSITORY, "Unrelated repository fact.")
    _, report = ConflictResolver().run([memory, repo])
    assert report.unchecked == 1
    assert report.checked == 0


def test_report_states_the_denominator():
    memory = item("m", Layer.MEMORY, "x", topic="t")
    repo = item("r", Layer.REPOSITORY, "y", topic="t")
    other = item("o", Layer.MEMORY, "z")
    _, report = ConflictResolver().run([memory, repo, other])
    assert report.summary() == "1 of 2 memories checked for conflicts, 1 conflict found, 1 unchecked"


def test_a_memory_alone_on_its_topic_is_checked_and_left_alone():
    memory = item("m", Layer.MEMORY, "x", topic="t")
    _, report = ConflictResolver().run([memory])
    assert report.checked == 1
    assert report.conflicts == []
