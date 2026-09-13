"""End-to-end checks for the server-side embedding queue, against the live stack.

    docker compose -f server/docker-compose.yaml exec -T mem0 \
        python /app/../scripts/test-pending-queue.py
    # or, from the repo root:
    docker compose --project-directory server exec -T mem0 python - < scripts/test-pending-queue.py

These cannot be unit tests. `FOR UPDATE SKIP LOCKED`, lease expiry and ordering
are Postgres behaviours, and a fake would only prove the fake works. The pure
policy - what gets queued, how long to wait, when to stop - is unit-tested in
tests/context_memory/test_pending_queue.py instead.

Nothing here touches the real queue: every row is prefixed and deleted, and no
embedding is ever requested, so it is safe to run with the provider down or the
quota spent.
"""

from __future__ import annotations

import sys
import threading
import uuid
from datetime import timedelta

sys.path.insert(0, "/app")

import pending  # noqa: E402
from db import SessionLocal  # noqa: E402
from models import PendingMemory  # noqa: E402

PREFIX = "QUEUECHECK"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  - ' + detail) if detail else ''}")


def clean(db) -> None:
    db.query(PendingMemory).filter(PendingMemory.text.like(f"{PREFIX}%")).delete(synchronize_session=False)
    db.commit()


def add(db, text: str, made: str | None = None) -> PendingMemory:
    payload = {"user_id": "queuecheck"}
    if made:
        payload["metadata"] = {"created_at": made}
    return pending.enqueue(
        db, text=f"{PREFIX} {text}", payload=payload, idempotency_key=f"qc-{uuid.uuid4().hex[:10]}"
    )


def mine(rows) -> list:
    return [r for r in rows if r.text.startswith(PREFIX)]


def main() -> int:
    print("Server-side embedding queue, against the live database\n")

    # -- two workers never take the same row --------------------------------
    print("claiming")
    with SessionLocal() as db:
        clean(db)
        for i in range(4):
            add(db, f"concurrent {i}")

    grabbed: dict[str, set[str]] = {}
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        with SessionLocal() as db:
            barrier.wait()
            grabbed[name] = {str(r.id) for r in mine(pending.claim_batch(db, limit=3))}

    a = threading.Thread(target=worker, args=("A",))
    b = threading.Thread(target=worker, args=("B",))
    a.start(), b.start(), a.join(), b.join()
    overlap = grabbed["A"] & grabbed["B"]
    check("two concurrent workers claim disjoint rows", not overlap, f"overlap={len(overlap)}")
    check("every claimable row was taken", len(grabbed["A"] | grabbed["B"]) == 4)

    with SessionLocal() as db:
        clean(db)

    # -- a dead worker releases its rows ------------------------------------
    print("\nleases")
    with SessionLocal() as db:
        clean(db)
        add(db, "held by a worker that dies")
        claimed = mine(pending.claim_batch(db, limit=5))
        check("a claim marks the row embedding", len(claimed) == 1 and claimed[0].state == "embedding")
        check("a live lease is not re-claimable", not mine(pending.claim_batch(db, limit=5)))

        later = pending._utcnow() + timedelta(seconds=pending.LEASE_SECONDS + 5)
        check("an expired lease is re-claimable", len(mine(pending.claim_batch(db, limit=5, now=later))) == 1)
        clean(db)

        # A lease no honest claim could produce - a clock jump, or a caller
        # passing the wrong `now`. Observed for real: a row stuck in `embedding`
        # for two days, held by a process that no longer existed.
        row = add(db, "pinned by an impossible lease")
        db.query(PendingMemory).filter(PendingMemory.id == row.id).update(
            {"state": "embedding", "claimed_by": "ghost",
             "lease_until": pending._utcnow() + timedelta(days=2)}
        )
        db.commit()
        check("an implausible lease does not pin a row", len(mine(pending.claim_batch(db, limit=5))) == 1)
        clean(db)

    # -- states -------------------------------------------------------------
    print("\nstates")
    with SessionLocal() as db:
        clean(db)
        row = add(db, "fails once then waits")
        claimed = mine(pending.claim_batch(db, limit=5))
        pending.mark_failed(db, claimed[0], "quota gone", "provider_quota_exhausted")
        check("a failure lands in error, not dead", db.get(PendingMemory, row.id).state == "error")
        check("an error inside its backoff is not retried", not mine(pending.claim_batch(db, limit=5)))

        after = pending._utcnow() + timedelta(hours=6)
        check(
            "an error retries on its OWN timer, with no other success",
            len(mine(pending.claim_batch(db, limit=5, now=after))) == 1,
        )
        clean(db)

        row = add(db, "swept back by someone else's success")
        claimed = mine(pending.claim_batch(db, limit=5))
        pending.mark_failed(db, claimed[0], "quota gone", "provider_quota_exhausted")
        pending.rearm_errors(db)
        check("a success re-arms error rows immediately", db.get(PendingMemory, row.id).state == "pending")
        clean(db)

        row = add(db, "gives up eventually")
        for _ in range(3):
            got = mine(pending.claim_batch(db, limit=5, now=pending._utcnow() + timedelta(hours=99)))
            if got:
                pending.mark_failed(db, got[0], "still gone", "provider_quota_exhausted")
        state = db.get(PendingMemory, row.id).state
        check("attempts accumulate rather than resetting", db.get(PendingMemory, row.id).attempts >= 3, state)
        clean(db)

    # -- ordering -----------------------------------------------------------
    print("\nordering")
    with SessionLocal() as db:
        clean(db)
        # Arrival order C, A, B; written order A, B, C. This is the client-spool
        # replay case: they all arrive at once, hours after they were written.
        add(db, "C", "2026-09-10T15:00:00+00:00")
        add(db, "A", "2026-09-10T09:00:00+00:00")
        add(db, "B", "2026-09-10T12:00:00+00:00")
        order = [r.text.split()[-1] for r in mine(pending.claim_batch(db, limit=10))]
        check("drains by when the memory was WRITTEN, not when it arrived",
              order == ["A", "B", "C"], f"got {order}")
        clean(db)

        for i in (1, 2, 3):
            add(db, f"plain{i}")
        order = [r.text.split()[-1] for r in mine(pending.claim_batch(db, limit=10))]
        check("falls back to arrival order when no date was sent",
              order == ["plain1", "plain2", "plain3"], f"got {order}")
        clean(db)

    # -- an aborted batch must not leave rows looking busy ------------------
    print("\naborted batches")
    with SessionLocal() as db:
        clean(db)
        rows = [add(db, f"batch member {i}") for i in range(3)]
        claimed = mine(pending.claim_batch(db, limit=5))
        check("all three were claimed", len(claimed) == 3)

        # The provider dies after the first row: the rest were never tried.
        freed = pending.release(db, claimed[1:])
        check("untried rows are handed back immediately", freed == 2)

        states = {r.id: r.state for r in db.query(PendingMemory).all()}
        untried = [states[r.id] for r in claimed[1:]]
        check("they return to pending, not left in embedding", set(untried) == {"pending"},
              f"got {untried}")
        attempts = [db.get(PendingMemory, r.id).attempts for r in claimed[1:]]
        check("and are not charged an attempt they never had", set(attempts) == {0}, f"got {attempts}")
        clean(db)

    # -- "send all" has to mean all -----------------------------------------
    print("\nsend all")
    with SessionLocal() as db:
        clean(db)
        row = add(db, "already pending but inside a backoff window")
        db.query(PendingMemory).filter(PendingMemory.id == row.id).update(
            {"next_attempt_at": pending._utcnow() + timedelta(hours=1)}
        )
        db.commit()
        check("a pending row inside its window is not claimable",
              not mine(pending.claim_batch(db, limit=5)))

        # What the endpoint does with force=true. A row already in `pending`
        # keeps whatever next_attempt_at it was given, so pressing the button
        # looked like it did nothing and the rows stayed put with no reason.
        from sqlalchemy import update as sa_update

        db.execute(
            sa_update(PendingMemory)
            .where(PendingMemory.state == "pending",
                   PendingMemory.next_attempt_at > pending._utcnow())
            .values(next_attempt_at=pending._utcnow())
        )
        db.commit()
        check("send-all clears the window so the row can actually go",
              len(mine(pending.claim_batch(db, limit=5))) == 1)
        clean(db)

    # -- idempotency --------------------------------------------------------
    print("\nidempotency")
    with SessionLocal() as db:
        clean(db)
        key = f"qc-dup-{uuid.uuid4().hex[:8]}"
        first = pending.enqueue(db, text=f"{PREFIX} duplicate", payload={"user_id": "queuecheck"},
                                idempotency_key=key)
        second = pending.enqueue(db, text=f"{PREFIX} duplicate", payload={"user_id": "queuecheck"},
                                 idempotency_key=key)
        check("the same key returns the same row, not a second one", first.id == second.id)
        check("only one row exists for that key",
              db.query(PendingMemory).filter(PendingMemory.idempotency_key == key).count() == 1)
        clean(db)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed} of {len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
