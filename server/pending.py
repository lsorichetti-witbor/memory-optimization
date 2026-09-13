"""The server-side embedding queue.

A write reaches the vector store only after it is embedded, and the embedder is
the part that fails - a daily quota, a rate limit, an outage. Before this, that
failure was pushed back to whoever happened to be calling: their client queued
the memory on their own laptop, drained it only when that person ran a command,
and nobody else could see it. The failure is server-wide, so the queue should be
too.

Shape of it:

    POST /memories -> try to embed inline (unchanged happy path)
                   -> on embedder failure, land the row here and return 202
    worker         -> claim -> embed -> insert into the vector store -> delete

Four states. `pending` and `error` are deliberately distinct: `pending` means
nobody has tried yet, `error` means the provider refused, and only the second
one is worth re-arming when some *other* embedding succeeds.

Two workers must never embed the same row, and two users must never pay for the
same embedding twice. Those are different problems and both are handled here -
see `claim_batch` and `reuse_vector_for`.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select, text as sql_text, update
from sqlalchemy.orm import Session

from models import PendingMemory
from pending_policy import (  # re-exported so callers have one import
    BACKOFF_CAP_MINUTES,
    DEAD,
    EMBEDDING,
    ERROR,
    LEASE_SECONDS,
    MAX_ATTEMPTS,
    PENDING,
    QUEUEABLE_CODES,
    CircuitBreaker,
    backoff as _backoff,
    content_hash,
    source_created_at,
    utcnow as _utcnow,
)

logger = logging.getLogger(__name__)

# One worker identity per process, so `claimed_by` says which process holds a
# row when more than one is running.
WORKER_ID = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"

breaker = CircuitBreaker()


# ------------------------------------------------------------------ enqueue


def find_existing(db: Session, idempotency_key: Optional[str]) -> Optional[PendingMemory]:
    if not idempotency_key:
        return None
    return db.execute(
        select(PendingMemory).where(PendingMemory.idempotency_key == idempotency_key)
    ).scalar_one_or_none()


def enqueue(
    db: Session,
    *,
    text: str,
    payload: dict,
    idempotency_key: Optional[str] = None,
    error: str = "",
    error_code: str = "",
) -> PendingMemory:
    """Land a memory the embedder refused, so the client can stop holding it.

    Returns the existing row when the idempotency key is already queued, rather
    than raising: a client retrying a request whose response it never saw is the
    normal case, not an error.
    """
    existing = find_existing(db, idempotency_key)
    if existing is not None:
        return existing

    row = PendingMemory(
        state=PENDING,
        text=text,
        payload=payload,
        content_hash=content_hash(text),
        idempotency_key=idempotency_key,
        last_error=error[:2000] or None,
        last_error_code=error_code or None,
        next_attempt_at=_utcnow(),
        source_created_at=source_created_at(payload) or _utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# -------------------------------------------------------------------- claim


def claim_batch(db: Session, limit: int = 10, *, now: Optional[datetime] = None) -> list[PendingMemory]:
    """Take up to `limit` rows, exclusively.

    `FOR UPDATE SKIP LOCKED` is what makes two workers safe: each transaction
    locks the rows it selects and the other simply does not see them, so the
    same memory is never embedded twice. Without it, two workers reading the
    same `state='pending'` rows would both embed and both insert.

    A row whose lease has expired is claimable again - that is the crash case,
    where the worker holding it went away without finishing.

    `error` rows are claimable once their backoff has elapsed. They were left out
    at first, which made `next_attempt_at` a field that was written and never
    read: with one row in the queue and nothing else succeeding, there was no
    later success to trigger the sweep, so it sat in `error` forever waiting for
    a human. Measured - a single failed row was still unclaimable two days after
    its backoff expired.

    `dead` is deliberately still excluded. That one really is waiting for a
    person.

    A lease further out than one lease-length is treated as already expired. No
    honest claim can produce one: it means a clock jumped, or something wrote it
    with the wrong `now`. Without this the row is unreachable until that date
    passes and no worker will touch it - observed as a row sitting in
    `embedding`, held by a process that no longer exists, with a lease two days
    in the future. `embedding` with nobody working is the worst state to be
    stuck in, because it looks like progress.
    """
    moment = now or _utcnow()
    claimable = sql_text(
        """
        SELECT id FROM pending_memories
        WHERE (
            (state IN (:pending, :error) AND (next_attempt_at IS NULL OR next_attempt_at <= :now))
            OR (state = :embedding AND (
                    lease_until IS NULL
                    OR lease_until < :now
                    OR lease_until > :max_lease
            ))
        )
        ORDER BY COALESCE(source_created_at, created_at)
        FOR UPDATE SKIP LOCKED
        LIMIT :limit
        """
    )
    ids = [
        r[0]
        for r in db.execute(
            claimable,
            {
                "pending": PENDING,
                "error": ERROR,
                "embedding": EMBEDDING,
                "now": moment,
                "max_lease": moment + timedelta(seconds=LEASE_SECONDS * 2),
                "limit": limit,
            },
        ).fetchall()
    ]
    if not ids:
        db.commit()
        return []

    db.execute(
        update(PendingMemory)
        .where(PendingMemory.id.in_(ids))
        .values(
            state=EMBEDDING,
            claimed_by=WORKER_ID,
            lease_until=moment + timedelta(seconds=LEASE_SECONDS),
            updated_at=moment,
        )
    )
    db.commit()
    # Ordered again, deliberately. The claim above picks the oldest rows, but a
    # bare SELECT ... WHERE id IN (...) returns them in whatever order Postgres
    # finds convenient - so the batch was chosen oldest-first and then embedded
    # in an arbitrary one. Measured: memories made at 15:00, 09:00 and 12:00
    # drained in exactly that order.
    return list(
        db.execute(
            select(PendingMemory)
            .where(PendingMemory.id.in_(ids))
            .order_by(func.coalesce(PendingMemory.source_created_at, PendingMemory.created_at))
        ).scalars()
    )


# ------------------------------------------------------------------ outcomes


def release(db: Session, rows, *, now: Optional[datetime] = None) -> int:
    """Hand claimed rows back without counting an attempt against them.

    For a batch that stops early - the provider went down partway through, so
    the rest were never tried. Leaving them to their lease would park them in
    `embedding` for the lease's duration, which reads as "a worker is on it"
    when no worker is. That is the most misleading state in the system, and it
    is avoidable: they were never attempted, so say so.
    """
    ids = [r.id for r in rows]
    if not ids:
        return 0
    moment = now or _utcnow()
    result = db.execute(
        update(PendingMemory)
        .where(PendingMemory.id.in_(ids), PendingMemory.state == EMBEDDING)
        # next_attempt_at is reset too: the row was never tried, so it has not
        # earned a backoff and must not sit out one it never caused.
        .values(
            state=PENDING, claimed_by=None, lease_until=None,
            next_attempt_at=moment, updated_at=moment,
        )
    )
    db.commit()
    return int(result.rowcount or 0)


def mark_stored(db: Session, row_id: uuid.UUID) -> None:
    """The memory is in the vector store; the queue row has done its job."""
    db.execute(sql_text("DELETE FROM pending_memories WHERE id = :id"), {"id": row_id})
    db.commit()


def mark_failed(db: Session, row: PendingMemory, error: str, code: str, *, now: Optional[datetime] = None) -> str:
    """Record a failed attempt and decide whether it is worth another.

    Returns the new state, so the caller can report `error` and `dead` apart -
    one is waiting, the other is waiting for a person.
    """
    moment = now or _utcnow()
    attempts = row.attempts + 1
    state = DEAD if attempts >= MAX_ATTEMPTS else ERROR
    db.execute(
        update(PendingMemory)
        .where(PendingMemory.id == row.id)
        .values(
            state=state,
            attempts=attempts,
            last_error=error[:2000],
            last_error_code=code,
            next_attempt_at=moment + _backoff(attempts),
            claimed_by=None,
            lease_until=None,
            updated_at=moment,
        )
    )
    db.commit()
    return state


def rearm_errors(db: Session, *, now: Optional[datetime] = None) -> int:
    """Move `error` rows back to `pending` after some embedding has succeeded.

    This is why the two states are separate. A success is fresh evidence that
    the provider works, and that evidence invalidates every backoff computed
    while it did not - those delays were guesses about an outage that is now
    over. Rows that already reached `dead` are left alone: they failed for a
    reason a working provider does not explain.
    """
    moment = now or _utcnow()
    result = db.execute(
        update(PendingMemory)
        .where(PendingMemory.state == ERROR)
        .values(state=PENDING, next_attempt_at=moment, updated_at=moment)
    )
    db.commit()
    return int(result.rowcount or 0)


# ---------------------------------------------------------------- reuse & stats


def already_stored(memory, idempotency_key: Optional[str]) -> bool:
    """Is this queued row's memory already in the vector store?

    The crash window: a worker embeds, inserts, and dies before deleting its
    queue row. The lease expires, someone re-claims it, and the memory is stored
    a second time - a duplicate, and a wasted embedding out of an allowance that
    was already the reason the row was queued.

    Checked by the idempotency key rather than the text, because the same
    sentence written to two scopes is two legitimate memories while the same key
    is by definition one write.

    Fails toward re-storing: if the lookup itself errors this returns False and
    the row is attempted. A duplicate is recoverable, a lost memory is not, so
    an unreadable answer must never be read as "already there".
    """
    if not idempotency_key:
        return False
    try:
        store = memory.vector_store
        with store._get_cursor() as cur:  # noqa: SLF001 - no public API exposes payload filters
            cur.execute(
                f"SELECT 1 FROM {store.collection_name} "  # noqa: S608 - identifier from config, not input
                "WHERE payload->>'idempotency_key' = %s LIMIT 1",
                (idempotency_key,),
            )
            return cur.fetchone() is not None
    except Exception as error:  # noqa: BLE001 - see docstring
        logger.debug("already-stored lookup failed, will re-attempt the write: %s", error)
        return False


def reuse_vector_for(memory, text: str) -> Optional[list[float]]:
    """An embedding already computed for identical text, if there is one.

    The other half of "never process the same embedding twice": not two workers
    on one row, but two people writing the same sentence. On a 1,000-a-day free
    tier that is the difference between draining and not.

    Reaches into the vector store's connection because mem0's public `list()`
    returns payloads without vectors. Failing soft on purpose - if anything about
    that shape changes, the caller just embeds normally and pays for it.
    """
    digest = content_hash(text)
    try:
        store = memory.vector_store
        with store._get_cursor() as cur:  # noqa: SLF001 - see docstring
            cur.execute(
                f"SELECT vector FROM {store.collection_name} "  # noqa: S608 - identifier from config, not input
                "WHERE payload->>'hash' = %s AND vector IS NOT NULL LIMIT 1",
                (digest,),
            )
            row = cur.fetchone()
        if not row or row[0] is None:
            return None
        raw = row[0]
        if isinstance(raw, str):
            return [float(x) for x in raw.strip("[]").split(",") if x.strip()]
        return list(raw)
    except Exception as error:  # noqa: BLE001 - see docstring
        logger.debug("embedding reuse lookup failed, will embed normally: %s", error)
        return None


def stats(db: Session) -> dict[str, Any]:
    """Counts by state plus the oldest waiting row, for the dashboard and health."""
    rows = db.execute(
        select(PendingMemory.state, func.count(), func.min(PendingMemory.created_at)).group_by(
            PendingMemory.state
        )
    ).all()
    by_state = {state: {"count": int(count), "oldest": oldest} for state, count, oldest in rows}
    total = sum(v["count"] for v in by_state.values())
    oldest = min((v["oldest"] for v in by_state.values() if v["oldest"]), default=None)
    return {
        "total": total,
        # Anything not yet in the vector store is not searchable, whatever state
        # it is in. Reporting only `pending` would understate the gap.
        "not_searchable": total,
        "pending": by_state.get(PENDING, {}).get("count", 0),
        "embedding": by_state.get(EMBEDDING, {}).get("count", 0),
        "error": by_state.get(ERROR, {}).get("count", 0),
        "dead": by_state.get(DEAD, {}).get("count", 0),
        "oldest_queued_at": oldest.isoformat() if oldest else None,
        "circuit_breaker": breaker.status(),
    }
