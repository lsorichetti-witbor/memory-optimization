"""Queue policy: the decisions, with no database attached.

Separated from `pending.py` so the rules that matter - what gets queued, how
long to wait, when to stop trying - can be read and tested without Postgres,
SQLAlchemy or a running server. They are the parts most likely to be wrong in a
way nothing notices.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

PENDING = "pending"
EMBEDDING = "embedding"
ERROR = "error"
DEAD = "dead"

# A row claimed by a worker that then dies would sit in `embedding` forever. The
# lease lets it be re-claimed: long enough that a slow embedding is not stolen
# mid-flight, short enough that a crash is not a long outage.
LEASE_SECONDS = 120

# After this many failures a row stops being retried and waits for a person. It
# is never deleted - it is still the only copy of something somebody kept.
MAX_ATTEMPTS = 12

BACKOFF_CAP_MINUTES = 60

# "The provider could not do it right now", as opposed to "this write is wrong".
# Only these divert into the queue; a malformed write fails loudly instead of
# retrying forever behind the rows that could actually succeed.
#
# `provider_auth_failed` is here on purpose: a rejected key is a configuration
# problem, and the same write succeeds once it is fixed.
QUEUEABLE_CODES = frozenset(
    {
        "provider_quota_exhausted",
        "provider_rate_limited",
        "provider_unavailable",
        "provider_timeout",
        "provider_auth_failed",
        "vector_store_unavailable",
        "datastore_unavailable",
        "unknown",
    }
)


def content_hash(text: str) -> str:
    """The same hash mem0 puts on every memory, so the two can be joined.

    Embedding reuse depends on this matching exactly. A different algorithm here
    would simply never find a match, and the reuse would look implemented while
    doing nothing at all.
    """
    return hashlib.md5(text.encode()).hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def backoff(attempts: int) -> timedelta:
    return timedelta(minutes=min(2**attempts, BACKOFF_CAP_MINUTES))


@dataclass
class CircuitBreaker:
    """Stops a queue of N rows from discovering the same outage N times.

    Measured: the Gemini free tier allows 1,000 embedding requests a day. With a
    few hundred rows queued, letting each one find out for itself that the quota
    is gone would spend the *next* day's allowance entirely on failures - the
    queue would guarantee it could never drain.

    So when the provider says it cannot serve anyone, the whole queue parks and
    one probe at a time is let through.

    Note the default here is the OPPOSITE of the client spool's, deliberately.
    There, an unrecognised error must keep the write, because being wrong costs
    a lost memory. Here, an unrecognised error must NOT park the queue, because
    being wrong costs every other row a stall - and those rows are already safe
    on disk. Same principle, different cheap direction.
    """

    TRIPPING_CODES = frozenset(
        {"provider_quota_exhausted", "provider_rate_limited", "provider_unavailable"}
    )

    opened_at: Optional[datetime] = None
    reason: str = ""
    code: str = ""
    retry_at: Optional[datetime] = None
    probe_in_flight: bool = False

    @property
    def is_open(self) -> bool:
        return self.opened_at is not None

    def trip(self, code: str, reason: str, *, now: Optional[datetime] = None) -> None:
        if code not in self.TRIPPING_CODES:
            return
        moment = now or utcnow()
        # A spent daily quota is not a "retry in 30 seconds" condition, whatever
        # the provider's retryDelay says - Google returns 2s on a daily
        # exhaustion too. Waiting an hour between probes costs one request;
        # believing the 2s costs the allowance that would let the queue drain.
        wait = timedelta(hours=1) if code == "provider_quota_exhausted" else timedelta(minutes=2)
        self.opened_at = moment
        self.code = code
        self.reason = reason
        self.retry_at = moment + wait
        self.probe_in_flight = False

    def close(self) -> None:
        self.opened_at = None
        self.reason = ""
        self.code = ""
        self.retry_at = None
        self.probe_in_flight = False

    def allows(self, *, now: Optional[datetime] = None) -> bool:
        """May a batch run right now?"""
        if not self.is_open:
            return True
        moment = now or utcnow()
        if self.retry_at and moment >= self.retry_at and not self.probe_in_flight:
            # Half open: exactly one batch gets through to find out. Without the
            # flag every worker pass would probe once the window passed, and the
            # parking would have bought nothing.
            self.probe_in_flight = True
            return True
        return False

    def status(self) -> dict[str, Any]:
        return {
            "open": self.is_open,
            "code": self.code or None,
            "reason": self.reason or None,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "retry_at": self.retry_at.isoformat() if self.retry_at else None,
            "probing": self.probe_in_flight,
        }
