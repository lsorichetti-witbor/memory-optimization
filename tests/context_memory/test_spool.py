from datetime import datetime, timedelta, timezone

import pytest

from src.memory.scopes import Scope
from src.memory.spool import Spool, SpooledWrite


def entry(**kw) -> SpooledWrite:
    base = dict(
        text="A memory that could not be written because the server was down.",
        scope=Scope.REPOSITORY,
        scope_key="repo-a",
        user="lautaro",
        kind="discovery",
        topic="server.ports",
        created_at=datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc),
    )
    base.update(kw)
    return SpooledWrite(**base)


# ------------------------------------------------------------------ enqueue


def test_enqueue_writes_one_file_per_entry(tmp_path):
    spool = Spool(root=tmp_path)
    spool.enqueue(entry())
    spool.enqueue(entry(text="Another one entirely different from the first."))
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_the_filename_starts_with_a_sortable_timestamp(tmp_path):
    spool = Spool(root=tmp_path)
    path = spool.enqueue(entry())
    assert path.name.startswith("20260913T120000")


def test_two_entries_at_the_same_instant_do_not_collide(tmp_path):
    # A timestamp alone is not unique: two writes in the same second would
    # overwrite each other and the loss would be silent.
    spool = Spool(root=tmp_path)
    at = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    a = spool.enqueue(entry(created_at=at, text="First memory, long enough to store."))
    b = spool.enqueue(entry(created_at=at, text="Second memory, also long enough."))
    assert a != b
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_the_spool_is_shared_across_repositories_and_scopes(tmp_path):
    spool = Spool(root=tmp_path)
    spool.enqueue(entry(scope=Scope.REPOSITORY, scope_key="repo-a"))
    spool.enqueue(entry(scope=Scope.REPOSITORY, scope_key="repo-b"))
    spool.enqueue(entry(scope=Scope.GLOBAL, scope_key="global"))
    keys = {(e.scope, e.scope_key) for e in spool.entries()}
    assert keys == {
        (Scope.REPOSITORY, "repo-a"),
        (Scope.REPOSITORY, "repo-b"),
        (Scope.GLOBAL, "global"),
    }


def test_entries_come_back_in_the_order_they_were_queued(tmp_path):
    spool = Spool(root=tmp_path)
    base = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    for i in range(3):
        spool.enqueue(entry(created_at=base + timedelta(minutes=i), text=f"Memory number {i} of three."))
    assert [e.text for e in spool.entries()] == [
        "Memory number 0 of three.",
        "Memory number 1 of three.",
        "Memory number 2 of three.",
    ]


def test_a_round_trip_preserves_every_field(tmp_path):
    from dataclasses import replace

    spool = Spool(root=tmp_path)
    original = entry(tags=("infra", "ports"), confidence=0.9, importance=0.8, source="session:x")
    spool.enqueue(original)
    stored = spool.entries()[0]
    # enqueue assigns an idempotency key, so the read-back is the original plus
    # that one field. Compare with it filled in rather than loosening the
    # comparison field by field, which would stop catching a dropped field.
    assert stored == replace(original, idempotency_key=stored.idempotency_key)


def test_enqueue_assigns_an_idempotency_key(tmp_path):
    # Without a key the replay cannot tell "never stored" from "stored, but the
    # confirmation never arrived", and a crash between the two duplicates.
    spool = Spool(root=tmp_path)
    spool.enqueue(entry())
    assert spool.entries()[0].idempotency_key


def test_each_queued_write_gets_its_own_key(tmp_path):
    spool = Spool(root=tmp_path)
    spool.enqueue(entry(text="One memory worth keeping for later."))
    spool.enqueue(entry(text="A different memory worth keeping too."))
    keys = {e.idempotency_key for e in spool.entries()}
    assert len(keys) == 2, "a shared key would make the second write look already stored"


def test_a_caller_supplied_key_is_kept(tmp_path):
    # DurableProvider passes the key it will send to the server; overwriting it
    # here would mean the stored key and the queued key never match.
    spool = Spool(root=tmp_path)
    spool.enqueue(entry(idempotency_key="chosen-by-the-caller"))
    assert spool.entries()[0].idempotency_key == "chosen-by-the-caller"


def test_the_original_timestamp_survives_and_is_not_the_replay_time(tmp_path):
    # Replaying with the replay-time as created_at would make an old memory look
    # brand new, and recency is a ranking signal.
    spool = Spool(root=tmp_path)
    spool.enqueue(entry(created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    assert spool.entries()[0].created_at == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_different_users_are_kept_apart(tmp_path):
    spool = Spool(root=tmp_path)
    spool.enqueue(entry(user="lautaro"))
    spool.enqueue(entry(user="someone-else"))
    assert {e.user for e in spool.entries()} == {"lautaro", "someone-else"}


# -------------------------------------------------------------------- flush


class FakeProvider:
    def __init__(self, fail_on=()):
        self.added = []
        self.fail_on = set(fail_on)
        self.user = None

    def add(self, text, **kwargs):
        if text in self.fail_on:
            raise RuntimeError("upstream 503")
        self.added.append((text, kwargs))
        return [type("R", (), {"id": f"id-{len(self.added)}"})()]


def test_flush_replays_every_entry_and_removes_the_files(tmp_path):
    spool = Spool(root=tmp_path)
    spool.enqueue(entry(text="First memory, long enough to store."))
    spool.enqueue(entry(text="Second memory, also long enough."))
    provider = FakeProvider()

    report = spool.flush(lambda user: provider)

    assert report.replayed == 2
    assert report.failed == 0
    assert list(tmp_path.glob("*.json")) == []


def test_flush_preserves_the_original_created_at(tmp_path):
    spool = Spool(root=tmp_path)
    spool.enqueue(entry(created_at=datetime(2025, 6, 1, tzinfo=timezone.utc)))
    provider = FakeProvider()
    spool.flush(lambda user: provider)
    assert provider.added[0][1]["created_at"] == datetime(2025, 6, 1, tzinfo=timezone.utc)


def test_a_failed_replay_keeps_its_file(tmp_path):
    # Deleting on failure would lose the memory permanently, and the loss would
    # be invisible: the spool would simply be empty next time.
    spool = Spool(root=tmp_path)
    spool.enqueue(entry(text="This one fails on replay entirely."))
    spool.enqueue(entry(text="This one succeeds on replay entirely."))
    provider = FakeProvider(fail_on={"This one fails on replay entirely."})

    report = spool.flush(lambda user: provider)

    assert report.replayed == 1
    assert report.failed == 1
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert "upstream 503" in report.errors[0]


def test_flush_groups_by_user_so_each_entry_replays_as_its_author(tmp_path):
    spool = Spool(root=tmp_path)
    spool.enqueue(entry(user="lautaro", text="Memory belonging to the first user."))
    spool.enqueue(entry(user="someone-else", text="Memory belonging to the second user."))
    seen = []

    def factory(user):
        seen.append(user)
        return FakeProvider()

    spool.flush(factory)
    assert sorted(seen) == ["lautaro", "someone-else"]


def test_flushing_an_empty_spool_is_not_an_error(tmp_path):
    report = Spool(root=tmp_path).flush(lambda user: FakeProvider())
    assert report.replayed == 0
    assert report.failed == 0
    assert "0 of 0" in report.summary()


def test_the_report_states_the_denominator(tmp_path):
    spool = Spool(root=tmp_path)
    spool.enqueue(entry(text="This one fails on replay entirely."))
    spool.enqueue(entry(text="This one succeeds on replay entirely."))
    report = spool.flush(lambda user: FakeProvider(fail_on={"This one fails on replay entirely."}))
    # The property is that the denominator is present and the leftover is
    # counted, not the exact wording - pinning the phrase would break on any
    # rewording that kept the report correct.
    assert report.summary().startswith("1 of 2 spooled writes replayed")
    assert report.outstanding == 1
    assert "1" in report.summary() and "queued" in report.summary()


def test_a_corrupt_spool_file_is_reported_and_left_alone(tmp_path):
    # Deleting what could not be parsed would destroy the only copy of a memory
    # whose file merely got truncated.
    (tmp_path / "20260913T120000-broken.json").write_text("{not json", encoding="utf-8")
    spool = Spool(root=tmp_path)
    report = spool.flush(lambda user: FakeProvider())
    assert report.failed == 1
    assert (tmp_path / "20260913T120000-broken.json").exists()
    assert any("broken" in e for e in report.errors)


def test_pending_count_is_available_without_flushing(tmp_path):
    spool = Spool(root=tmp_path)
    assert spool.pending() == 0
    spool.enqueue(entry())
    assert spool.pending() == 1


def test_the_spool_root_is_created_on_demand(tmp_path):
    root = tmp_path / "does" / "not" / "exist"
    Spool(root=root).enqueue(entry())
    assert root.is_dir()


def test_enqueue_refuses_an_entry_with_no_text(tmp_path):
    with pytest.raises(ValueError, match="text"):
        Spool(root=tmp_path).enqueue(entry(text="   "))
