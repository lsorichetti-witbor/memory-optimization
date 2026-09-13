"""A write must survive every way the store can refuse it.

The rule these tests encode: **reads may fail fast, writes may not fail at all.**
A search that fails is retried by whoever ran it, one second later, at no cost.
A write that fails takes an observation with it, and nobody notices, because
what was lost never existed anywhere else.

The previous design queued from inside the exception handler of one CLI, and
decided what to queue by matching the error message against a list of strings.
Both halves are tested here: the layer (every caller, not one) and the default
(keep, not drop).
"""

from __future__ import annotations

import json

import pytest

from src.memory.durable import DurableProvider, WriteQueued
from src.memory.errors import InvalidWrite, Mem0Error
from src.memory.scopes import Scope
from src.memory.spool import Spool, SpooledWrite, should_queue
from src.memory.types import MemoryPage, MemoryRecord


class Recorder:
    """An inner provider that fails however the test asks it to."""

    def __init__(self, error=None, existing=()):
        self.error = error
        self.added = []
        self.existing = list(existing)
        self.closed = False

    def add(self, text, **kwargs):
        if self.error is not None:
            raise self.error
        self.added.append((text, kwargs))
        return [MemoryRecord(id=f"id-{len(self.added)}", text=text)]

    def get_all(self, *, scope, scope_key, top_k=100):
        return MemoryPage(
            records=tuple(self.existing), limit=top_k, returned=len(self.existing), truncated=False
        )

    def close(self):
        self.closed = True


def durable(tmp_path, inner):
    return DurableProvider(inner, user="lautaro", spool=Spool(root=tmp_path))


def only_entry(tmp_path):
    files = sorted(p for p in tmp_path.glob("*.json"))
    assert len(files) == 1, f"expected exactly one queued entry, found {[f.name for f in files]}"
    return json.loads(files[0].read_text(encoding="utf-8"))


# ------------------------------------------------------- the default is keep


@pytest.mark.parametrize(
    "error",
    [
        Mem0Error("quota", code="provider_quota_exhausted", status=502),
        Mem0Error("slow down", code="provider_rate_limited", status=502),
        Mem0Error("down", code="provider_unavailable", status=502),
        Mem0Error("bad key", code="provider_auth_failed", status=401),
        Mem0Error("boom", code="unknown", status=500),
        Mem0Error("proxy said no", status=503),
        # No Mem0Error at all: a transport blow-up, or a bug in our own code.
        # Neither means the write is bad, so neither may discard it.
        RuntimeError("connection reset by peer"),
        TimeoutError("read timed out"),
        OSError("socket closed"),
    ],
)
def test_every_failure_that_is_not_a_malformed_write_is_queued(tmp_path, error):
    # The old predicate matched message text and dropped anything unrecognised:
    # a 500, a RemoteProtocolError, a proxy's HTML error page and a bare 429
    # were all lost. An error nobody has seen before must land on the safe side.
    provider = durable(tmp_path, Recorder(error=error))
    with pytest.raises(WriteQueued):
        provider.add("A memory worth keeping.", scope=Scope.REPOSITORY, scope_key="r")
    assert only_entry(tmp_path)["text"] == "A memory worth keeping."


def test_an_auth_failure_is_queued_because_the_write_is_still_good(tmp_path):
    # A rejected API key is a configuration problem. Fix the key and this exact
    # write succeeds, so discarding it would throw away a recoverable memory.
    provider = durable(tmp_path, Recorder(error=Mem0Error("nope", code="provider_auth_failed", status=403)))
    with pytest.raises(WriteQueued):
        provider.add("Still a good memory.", scope=Scope.GLOBAL, scope_key="global")
    assert len(list(tmp_path.glob("*.json"))) == 1


# --------------------------------------------------- and the exception to it


@pytest.mark.parametrize(
    "error",
    [
        Mem0Error("malformed", code="provider_bad_request", status=400),
        Mem0Error("unprocessable", status=422),
        InvalidWrite("scope_key is required"),
    ],
)
def test_a_write_the_server_calls_malformed_is_not_queued(tmp_path, error):
    # Queueing this would retry a guaranteed failure until someone investigates,
    # and bury the real queue under entries that can never drain.
    provider = durable(tmp_path, Recorder(error=error))
    with pytest.raises((Mem0Error, InvalidWrite)):
        provider.add("A malformed write.", scope=Scope.REPOSITORY, scope_key="r")
    assert list(tmp_path.glob("*.json")) == []


def test_should_queue_defaults_to_true_for_a_type_it_has_never_seen(tmp_path):
    class NeverSeenBefore(Exception):
        pass

    assert should_queue(NeverSeenBefore()) is True


# ------------------------------------------------------------- write-ahead


def test_the_entry_exists_before_the_request_is_made(tmp_path):
    # The reason for write-ahead: a process killed mid-POST never reaches an
    # exception handler, so queueing from one cannot save the write.
    seen = {}

    class Killed:
        def add(self, text, **kwargs):
            seen["queued_at_request_time"] = len(list(tmp_path.glob("*.json")))
            raise KeyboardInterrupt()

        def close(self):
            pass

    provider = durable(tmp_path, Killed())
    with pytest.raises(KeyboardInterrupt):
        provider.add("Interrupted halfway.", scope=Scope.REPOSITORY, scope_key="r")

    assert seen["queued_at_request_time"] == 1, "the entry must be on disk before the POST"
    assert only_entry(tmp_path)["text"] == "Interrupted halfway."


def test_a_successful_write_leaves_nothing_queued(tmp_path):
    provider = durable(tmp_path, Recorder())
    records = provider.add("Stored cleanly.", scope=Scope.REPOSITORY, scope_key="r")
    assert [r.text for r in records] == ["Stored cleanly."]
    assert list(tmp_path.glob("*.json")) == [], "a stored write must not stay in the queue"


def test_no_temp_file_is_left_behind(tmp_path):
    # The atomic write uses a .tmp sidecar; leaking one would accumulate forever
    # and, being invisible to pending(), would never be reported.
    provider = durable(tmp_path, Recorder())
    provider.add("Stored cleanly.", scope=Scope.REPOSITORY, scope_key="r")
    assert list(tmp_path.glob("*.tmp")) == []


# ------------------------------------------------------------- idempotency


def test_the_key_sent_to_the_server_is_the_key_left_in_the_queue(tmp_path):
    # If these differ, the replay compares a key that was never stored and
    # duplicates the memory on every flush.
    inner = Recorder()
    provider = durable(tmp_path, inner)

    # Fail once to leave an entry, then read what the retry would send.
    failing = durable(tmp_path, Recorder(error=Mem0Error("down", code="provider_unavailable")))
    with pytest.raises(WriteQueued):
        failing.add("Queued once.", scope=Scope.REPOSITORY, scope_key="r")
    queued_key = only_entry(tmp_path)["idempotency_key"]
    assert queued_key

    provider.add("Sent now.", scope=Scope.REPOSITORY, scope_key="r", idempotency_key=queued_key)
    assert inner.added[0][1]["idempotency_key"] == queued_key


def test_a_replay_skips_a_write_the_server_already_has(tmp_path):
    # The duplicate window: stored server-side, then killed before the queue
    # entry was removed. Replaying blind would store it a second time.
    spool = Spool(root=tmp_path)
    path = spool.enqueue(
        SpooledWrite(text="Stored, but never confirmed.", scope=Scope.REPOSITORY, scope_key="r", user="lautaro")
    )
    key = json.loads(path.read_text(encoding="utf-8"))["idempotency_key"]

    from src.memory.envelope import Envelope

    already = MemoryRecord(
        id="server-side-id",
        text="Stored, but never confirmed.",
        envelope=Envelope(extra={"idempotency_key": key}),
    )
    inner = Recorder(existing=[already])
    report = spool.flush(lambda user: inner)

    assert inner.added == [], "the write was already stored; replaying it duplicates the memory"
    assert report.already_stored == 1
    assert report.replayed == 0, "a skipped duplicate is not a write stored on this run"
    assert list(tmp_path.glob("*.json")) == [], "a confirmed-present write should leave the queue"


def test_an_unreadable_listing_re_stores_rather_than_assuming_it_is_there(tmp_path):
    # Failing toward a duplicate, never toward a loss: if the check itself
    # cannot answer, the write is attempted.
    spool = Spool(root=tmp_path)
    spool.enqueue(SpooledWrite(text="Uncertain.", scope=Scope.REPOSITORY, scope_key="r", user="lautaro"))

    class ListingBroken(Recorder):
        def get_all(self, **kwargs):
            raise RuntimeError("cannot list right now")

    inner = ListingBroken()
    spool.flush(lambda user: inner)
    assert len(inner.added) == 1


# ------------------------------------------------ attempts, backoff, dead/


def test_a_failed_replay_counts_the_attempt_and_keeps_the_file(tmp_path):
    spool = Spool(root=tmp_path)
    spool.enqueue(SpooledWrite(text="Keeps failing.", scope=Scope.REPOSITORY, scope_key="r", user="lautaro"))
    spool.flush(lambda user: Recorder(error=Mem0Error("down", code="provider_unavailable")), force=True)
    entries = spool.entries()
    assert len(entries) == 1
    assert entries[0].attempts == 1
    assert "down" in (entries[0].last_error or "")


def test_an_entry_inside_its_backoff_window_is_deferred_not_failed(tmp_path):
    # "Not tried yet" and "tried and failed" call for different actions, so they
    # are counted apart.
    spool = Spool(root=tmp_path)
    spool.enqueue(SpooledWrite(text="Waiting.", scope=Scope.REPOSITORY, scope_key="r", user="lautaro"))
    inner = Recorder(error=Mem0Error("down", code="provider_unavailable"))
    spool.flush(lambda user: inner, force=True)

    second = spool.flush(lambda user: inner)
    assert second.deferred == 1
    assert second.failed == 0
    assert spool.entries()[0].attempts == 1, "a deferred entry must not burn an attempt"


def test_force_retries_inside_the_backoff_window(tmp_path):
    spool = Spool(root=tmp_path)
    spool.enqueue(SpooledWrite(text="Retry me now.", scope=Scope.REPOSITORY, scope_key="r", user="lautaro"))
    inner = Recorder(error=Mem0Error("down", code="provider_unavailable"))
    spool.flush(lambda user: inner, force=True)
    assert spool.flush(lambda user: inner, force=True).failed == 1
    assert spool.entries()[0].attempts == 2


def test_an_exhausted_entry_moves_to_dead_and_is_not_deleted(tmp_path):
    spool = Spool(root=tmp_path)
    spool.enqueue(SpooledWrite(text="Never works.", scope=Scope.REPOSITORY, scope_key="r", user="lautaro"))
    inner = Recorder(error=Mem0Error("down", code="provider_unavailable"))

    for _ in range(3):
        report = spool.flush(lambda user: inner, force=True, max_attempts=3)

    assert report.dead_lettered == 1
    assert spool.pending() == 0
    assert spool.dead() == 1, "an exhausted write is still the only copy; it must survive"
    dead_file = next(spool.dead_root.glob("*.json"))
    assert json.loads(dead_file.read_text(encoding="utf-8"))["text"] == "Never works."


def test_dead_entries_are_not_replayed_again(tmp_path):
    # dead/ lives under the spool root; if the glob picked it up the entry would
    # loop forever instead of waiting for a human.
    spool = Spool(root=tmp_path)
    spool.enqueue(SpooledWrite(text="Never works.", scope=Scope.REPOSITORY, scope_key="r", user="lautaro"))
    inner = Recorder(error=Mem0Error("down", code="provider_unavailable"))
    for _ in range(2):
        spool.flush(lambda user: inner, force=True, max_attempts=2)
    assert spool.flush(lambda user: inner, force=True).total == 0


def test_outstanding_counts_every_reason_a_write_is_not_stored(tmp_path):
    # The exit code keys off this. Leaving any category out would let a partial
    # replay exit 0.
    from src.memory.spool import FlushReport

    report = FlushReport(total=6, replayed=2, failed=1, deferred=2, dead_lettered=1)
    assert report.outstanding == 4


# ----------------------------------------------------------------- reporting


def test_the_advice_depends_on_why_the_write_failed(tmp_path):
    # One message for every failure was wrong for a spent quota: it said the
    # server was unreachable and told the user to start a stack that was up.
    quota = WriteQueued(tmp_path / "x.json", Mem0Error("q", code="provider_quota_exhausted"), 1)
    down = WriteQueued(tmp_path / "x.json", Mem0Error("d", code="datastore_unavailable"), 1)

    assert "quota" in quota.advice().lower()
    assert "unreachable" not in quota.advice().lower()
    assert "start the stack" in down.advice().lower()


def test_reads_are_not_wrapped_and_not_queued(tmp_path):
    # Deliberate asymmetry: a failed search costs a retry, a failed write costs
    # the memory. Only writes get the machinery.
    class ReadFails(Recorder):
        def get_all(self, **kwargs):
            raise Mem0Error("search down", code="provider_unavailable")

    provider = durable(tmp_path, ReadFails())
    with pytest.raises(Mem0Error):
        provider.get_all(scope=Scope.REPOSITORY, scope_key="r")
    assert list(tmp_path.glob("*.json")) == []
