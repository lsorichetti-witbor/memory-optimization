"""The background worker that drains `pending_memories`.

Triggers, all four of them:

    on enqueue      a row just landed, try it straight away
    on startup      drain whatever a restart left behind
    on a timer      the backoff clock, for rows waiting their turn
    on success      re-arm every `error` row, because a working embedding is
                    fresh evidence that the outage they were backing off from
                    is over

The last one is the reason `pending` and `error` are separate states rather than
one "not done yet".

The worker replays through mem0's own `memory.add()` rather than rebuilding the
insert. Reproducing `_create_memory` here would mean a queued memory and a
directly-stored one could drift apart the next time upstream adds a payload
field, and the difference would be invisible - two memories that look the same
and rank differently. Embedding reuse is therefore done at the embedder (see
`install_embedding_reuse`) instead of by hand-assembling a row.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from db import SessionLocal
from errors import _classify
import pending

logger = logging.getLogger(__name__)

BATCH_SIZE = 5
IDLE_SECONDS = 60
BUSY_SECONDS = 2

_wake = asyncio.Event()
_task: Optional[asyncio.Task] = None


def wake() -> None:
    """Ask the worker to run now. Safe from any thread that shares the loop."""
    _wake.set()


# ------------------------------------------------------------ embedding reuse


class _ReusingEmbedder:
    """Serves an embedding from an identical one already stored, when there is one.

    Two people recording the same convention should cost one embedding, not two.
    On the Gemini free tier - 1,000 a day, measured - that is the difference
    between a queue that drains and one that cannot.

    Keyed by (text hash, memory_action) and never across actions: Gemini embeds
    with a task type, so the vector for "add" is not the vector for "search" and
    swapping them would quietly degrade every future ranking. Only `add` is
    served from the vector store, because those are the vectors stored there.
    """

    def __init__(self, inner: Any, memory: Any) -> None:
        self._inner = inner
        self._memory = memory
        self.reused = 0
        self.computed = 0

    def embed(self, text: str, memory_action: Optional[str] = None):
        if memory_action == "add":
            existing = pending.reuse_vector_for(self._memory, text)
            if existing is not None:
                self.reused += 1
                return existing
        self.computed += 1
        return self._inner.embed(text, memory_action=memory_action)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def install_embedding_reuse(memory: Any) -> Any:
    """Wrap the memory instance's embedder, once."""
    current = getattr(memory, "embedding_model", None)
    if current is None or isinstance(current, _ReusingEmbedder):
        return current
    wrapped = _ReusingEmbedder(current, memory)
    memory.embedding_model = wrapped
    return wrapped


# -------------------------------------------------------------------- draining


def drain_once(memory_factory, *, batch_size: int = BATCH_SIZE) -> dict[str, int]:
    """One pass: claim a batch, try each, record what happened.

    Returns counts so the caller can decide whether to come back immediately.
    """
    result = {"claimed": 0, "stored": 0, "failed": 0, "dead": 0, "rearmed": 0, "skipped_breaker": 0}

    if not pending.breaker.allows():
        # The queue is parked. Counting the skip rather than silently returning
        # zero keeps "nothing to do" distinguishable from "not allowed to".
        with SessionLocal() as db:
            result["skipped_breaker"] = int(pending.stats(db)["pending"])
        return result

    with SessionLocal() as db:
        rows = pending.claim_batch(db, limit=batch_size)
        result["claimed"] = len(rows)
        if not rows:
            return result

        memory = memory_factory()
        install_embedding_reuse(memory)

        for row in rows:
            params = dict(row.payload or {})
            messages = params.pop("messages", None) or [{"role": "user", "content": row.text}]
            try:
                memory.add(messages=messages, **params)
            except Exception as error:  # noqa: BLE001 - the code decides what happens next
                code, detail = _classify(error)
                state = pending.mark_failed(db, row, f"{detail} ({error})", code)
                result["dead" if state == pending.DEAD else "failed"] += 1
                pending.breaker.trip(code, detail)
                if pending.breaker.is_open:
                    # Everyone else in this batch would hit the same wall. Stop
                    # and let the remaining leases expire rather than spending
                    # the rest of the allowance discovering it once per row.
                    logger.warning("embedding queue parked: %s (%s)", detail, code)
                    break
                continue

            pending.mark_stored(db, row.id)
            result["stored"] += 1
            # A success proves the provider works, so the outage every `error`
            # row was backing off from is over. Re-arm them now instead of
            # waiting out delays that were guesses about a condition that has
            # since changed.
            pending.breaker.close()
            result["rearmed"] += pending.rearm_errors(db)

    return result


async def _loop(memory_factory) -> None:
    logger.info("pending-memory worker started (worker_id=%s)", pending.WORKER_ID)
    # Startup drain: a restart in the middle of a batch leaves rows claimed, and
    # their leases have to expire before anyone else takes them. Running now
    # picks up everything the previous process never got to.
    while True:
        try:
            outcome = await asyncio.to_thread(drain_once, memory_factory)
        except Exception:  # noqa: BLE001 - a worker that dies stops draining forever
            logger.exception("pending-memory worker pass failed")
            outcome = {"claimed": 0, "stored": 0}

        if outcome.get("stored") or outcome.get("claimed"):
            logger.info("pending-memory worker: %s", outcome)

        # Come straight back while work is moving; otherwise sleep until the
        # backoff clock or an enqueue wakes us.
        delay = BUSY_SECONDS if outcome.get("stored") else IDLE_SECONDS
        try:
            await asyncio.wait_for(_wake.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        finally:
            _wake.clear()


def start(memory_factory) -> None:
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop(memory_factory))


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutting down
        pass
    _task = None
