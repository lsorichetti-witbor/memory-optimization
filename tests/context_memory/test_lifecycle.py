import pytest

from src.memory.lifecycle import LIFECYCLES, Lifecycle, can_transition, transition


def test_lifecycle_vocabulary():
    assert set(LIFECYCLES) == {"candidate", "durable", "stale", "superseded"}


@pytest.mark.parametrize(
    "src,dst",
    [
        ("candidate", "durable"),
        ("candidate", "stale"),
        ("candidate", "superseded"),
        ("durable", "stale"),
        ("durable", "superseded"),
        ("stale", "durable"),
        ("stale", "superseded"),
    ],
)
def test_legal_transitions(src, dst):
    assert can_transition(Lifecycle(src), Lifecycle(dst)) is True


@pytest.mark.parametrize(
    "src,dst",
    [
        ("superseded", "durable"),
        ("superseded", "stale"),
        ("superseded", "candidate"),
        ("durable", "candidate"),
        ("stale", "candidate"),
    ],
)
def test_illegal_transitions(src, dst):
    assert can_transition(Lifecycle(src), Lifecycle(dst)) is False


def test_transition_returns_the_metadata_patch_to_write():
    patch = transition(
        Lifecycle.DURABLE, Lifecycle.SUPERSEDED, superseded_by="m9", at="2026-09-13T12:00:00+00:00"
    )
    assert patch == {
        "lifecycle": "superseded",
        "superseded_by": "m9",
        "lifecycle_changed_at": "2026-09-13T12:00:00+00:00",
    }


def test_superseding_without_a_successor_id_raises():
    # A superseded memory with no pointer to what replaced it is unrecoverable context.
    with pytest.raises(ValueError, match="superseded_by"):
        transition(Lifecycle.DURABLE, Lifecycle.SUPERSEDED, at="2026-09-13T12:00:00+00:00")


def test_illegal_transition_raises_rather_than_writing():
    with pytest.raises(ValueError, match="cannot transition"):
        transition(Lifecycle.SUPERSEDED, Lifecycle.DURABLE, at="2026-09-13T12:00:00+00:00")


def test_promoting_a_candidate_records_when_it_happened():
    patch = transition(Lifecycle.CANDIDATE, Lifecycle.DURABLE, at="2026-09-13T12:00:00+00:00")
    assert patch == {"lifecycle": "durable", "lifecycle_changed_at": "2026-09-13T12:00:00+00:00"}
