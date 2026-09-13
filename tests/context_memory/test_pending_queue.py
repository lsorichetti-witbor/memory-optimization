"""The server-side embedding queue: states, claiming, and the circuit breaker.

Three properties, each because the alternative fails quietly:

**`pending` and `error` are different things.** Nobody has tried yet, versus the
provider refused. Only the second is worth re-arming when some *other* embedding
succeeds - that success is fresh evidence the outage is over, and it invalidates
every backoff computed while it was not.

**Two workers must never embed the same row.** Claiming is `FOR UPDATE SKIP
LOCKED` plus a lease, so a crashed worker releases its rows instead of stranding
them, and a second worker never sees rows the first holds.

**A queue of N rows must not discover one outage N times.** The Gemini free tier
is 1,000 embeddings a day; letting each queued row find out for itself that the
quota is gone spends the next day's allowance on failures, so the queue could
never drain. The breaker parks everything and probes once.

These run without Postgres on purpose. The policy - what gets queued, how long
to wait, when to stop - lives in `pending_policy.py` with no database attached,
so the decisions most likely to be quietly wrong are testable without a stack.
The SQL claim itself is exercised against the live server instead.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[2] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from pending_policy import (  # noqa: E402
    QUEUEABLE_CODES,
    CircuitBreaker,
    content_hash,
    source_created_at,
)


NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------ circuit breaker


def test_a_spent_daily_quota_parks_the_queue():
    breaker = CircuitBreaker()
    assert breaker.allows(now=NOW), "a fresh breaker must not block anything"

    breaker.trip("provider_quota_exhausted", "quota gone", now=NOW)

    assert breaker.is_open
    assert not breaker.allows(now=NOW + timedelta(minutes=5))


def test_a_daily_quota_waits_an_hour_not_seconds():
    # Google returns retryDelay: 2s even on a daily exhaustion. Believing it
    # would burn the allowance that lets the queue drain when it resets.
    breaker = CircuitBreaker()
    breaker.trip("provider_quota_exhausted", "quota gone", now=NOW)
    assert breaker.retry_at == NOW + timedelta(hours=1)


def test_a_rate_limit_waits_minutes_because_it_actually_clears():
    breaker = CircuitBreaker()
    breaker.trip("provider_rate_limited", "slow down", now=NOW)
    assert breaker.retry_at == NOW + timedelta(minutes=2)


def test_only_one_probe_is_let_through_when_the_window_opens():
    # If every worker pass probed, a parked queue would still hammer the
    # provider once the retry time passed - the parking would buy nothing.
    breaker = CircuitBreaker()
    breaker.trip("provider_quota_exhausted", "quota gone", now=NOW)
    later = NOW + timedelta(hours=2)

    assert breaker.allows(now=later) is True, "the first pass after the window probes"
    assert breaker.allows(now=later) is False, "a second concurrent pass must not"
    assert breaker.probe_in_flight


def test_a_success_closes_the_breaker():
    breaker = CircuitBreaker()
    breaker.trip("provider_unavailable", "down", now=NOW)
    breaker.close()
    assert not breaker.is_open
    assert breaker.allows(now=NOW)


def test_a_malformed_row_does_not_park_everyone_else():
    # A bad write is that row's problem. Tripping on it would let one unusable
    # memory stop every good one behind it.
    breaker = CircuitBreaker()
    breaker.trip("provider_bad_request", "your write is wrong", now=NOW)
    assert not breaker.is_open
    assert breaker.allows(now=NOW)


def test_an_unknown_code_does_not_park_the_queue():
    # Deliberately the opposite default from the client spool. There, an
    # unrecognised error must KEEP the write, because the cost of being wrong is
    # a lost memory. Here it must NOT park every other row, because the cost of
    # being wrong is a stalled queue - and the row is already safe on disk.
    breaker = CircuitBreaker()
    breaker.trip("unknown", "who knows", now=NOW)
    assert not breaker.is_open


def test_the_status_says_enough_to_act_on():
    breaker = CircuitBreaker()
    breaker.trip("provider_quota_exhausted", "quota gone until midnight", now=NOW)
    status = breaker.status()
    assert status["open"] is True
    assert status["code"] == "provider_quota_exhausted"
    assert "quota gone" in status["reason"]
    assert status["retry_at"]


# ------------------------------------------------------------------- hashing


def test_the_content_hash_matches_the_one_mem0_stores():
    # Embedding reuse joins on this. A different algorithm here would silently
    # never match, and the reuse would look implemented while doing nothing.
    import hashlib

    text = "Repo alpha pins its build to node 20."
    assert content_hash(text) == hashlib.md5(text.encode()).hexdigest()


def test_identical_text_hashes_identically_and_different_text_does_not():
    assert content_hash("same words") == content_hash("same words")
    assert content_hash("same words") != content_hash("other words")


# ------------------------------------------------------ queueable classification


def test_only_provider_failures_divert_into_the_queue():
    # A malformed write must fail loudly rather than sit in a queue retrying
    # forever; a provider failure must never be a client's problem.
    for code in (
        "provider_quota_exhausted",
        "provider_rate_limited",
        "provider_unavailable",
        "provider_timeout",
        "datastore_unavailable",
        "vector_store_unavailable",
        "unknown",
    ):
        assert code in QUEUEABLE_CODES, f"{code} should be queued, not returned as an error"

    assert "provider_bad_request" not in QUEUEABLE_CODES


def test_an_auth_failure_is_queued_here_too():
    # Same reasoning as the client spool: a rejected key is a configuration
    # problem, and the write itself is still good.
    assert "provider_auth_failed" in QUEUEABLE_CODES


# ------------------------------------------------------------------ ordering


def test_the_memory_s_own_date_is_read_from_the_metadata_envelope():
    # Ordering on arrival time re-dates everything a client replays after an
    # outage: memories made hours apart all arrive "now", so they drain in the
    # wrong order and recency ranking sees them as newly written.
    assert source_created_at({"metadata": {"created_at": "2026-09-10T09:00:00+00:00"}}) == datetime(
        2026, 9, 10, 9, 0, tzinfo=timezone.utc
    )


def test_a_naive_timestamp_is_read_as_utc_rather_than_dropped():
    # Dropping it would fall back to arrival time - the exact bug this field
    # exists to fix - while comparing it raw against aware values would raise.
    assert source_created_at({"metadata": {"created_at": "2026-09-10T09:00:00"}}) == datetime(
        2026, 9, 10, 9, 0, tzinfo=timezone.utc
    )


def test_a_missing_or_unparseable_date_falls_back_instead_of_raising():
    # enqueue runs inside a failure handler. Raising here would turn a queued
    # write into a lost one.
    for payload in ({}, {"metadata": {}}, {"metadata": {"created_at": "not a date"}},
                    {"metadata": {"created_at": ""}}, {"metadata": {"created_at": 12345}}):
        assert source_created_at(payload) is None


def test_ordering_puts_the_oldest_memory_first():
    # The property the queue depends on, independent of any SQL.
    made = [
        source_created_at({"metadata": {"created_at": s}})
        for s in ("2026-09-10T15:00:00+00:00", "2026-09-10T09:00:00+00:00", "2026-09-10T12:00:00+00:00")
    ]
    assert [d.hour for d in sorted(made)] == [9, 12, 15]
