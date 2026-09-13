"""A provider wrapper that will not lose a write.

The spool existed before this, but it was wired into one CLI entry point
(`memory_store.py`). Everything else that writes - `memory_extract --store`,
which stores a whole batch of candidates extracted from a transcript,
`memory_backup restore`, the seeders, and any agent holding a `Mem0Provider` -
printed an error and returned 1. The batch case is the worst of them: many
memories at once, from a transcript that may already be gone.

Putting the guarantee here instead means every caller inherits it without
changing a line, because they all pass through `add`.

Two properties, both chosen for how they fail:

**Write-ahead, not write-behind.** The spool entry is written *before* the
request, so a crash, a Ctrl+C or a killed terminal mid-POST leaves the memory
recoverable. Queueing from inside the exception handler - which is what happened
before - cannot help with a process that never reaches the handler.

**Confirmed delete only.** The entry is removed after the server accepts, and on
one other occasion: when the server says the write itself is malformed, because
retrying that forever helps nobody. Every other failure keeps the file.

The cost is a duplicate window: stored server-side, then killed before the
delete. `idempotency_key` closes it - the key travels with the memory, and
`Spool.flush` skips an entry whose key is already in the scope. Duplicating a
memory is recoverable; losing one is not.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from src.memory.base import MemoryProvider
from src.memory.errors import Mem0Error
from src.memory.scopes import Scope
from src.memory.spool import Spool, SpooledWrite, should_queue
from src.memory.types import MemoryPage, MemoryRecord, SearchQuery


class WriteQueued(RuntimeError):
    """The write was not stored, and was kept on disk to be replayed.

    Raised rather than returned. A caller that ignores the difference between
    "stored" and "queued" would report success for a memory that is not
    searchable yet, so the failure has to be loud - but it carries the queue
    path, so the caller can say where the write went instead of just that it
    failed.
    """

    def __init__(self, path: Path, cause: BaseException, pending: int) -> None:
        self.path = Path(path)
        self.cause = cause
        self.pending = pending
        code = getattr(cause, "code", None)
        super().__init__(
            f"not stored, queued at {self.path} ({pending} pending): {cause}"
            + (f" [{code}]" if code and code != "unknown" else "")
        )

    @property
    def code(self) -> str:
        return str(getattr(self.cause, "code", "unknown"))

    def advice(self) -> str:
        """What the user should actually do, which depends on why it failed.

        The old message said "the Mem0 server is unreachable - start the stack"
        for every queued write. With the quota exhausted that was wrong twice
        over: the server was up, and starting it again would change nothing.
        """
        return {
            "provider_quota_exhausted": (
                "The provider's quota is spent. Replay after it resets, or raise the limit."
            ),
            "provider_rate_limited": "Rate limited. Replay shortly.",
            "provider_auth_failed": (
                "The provider rejected the credentials. Fix the API key, then replay - "
                "the write itself is fine."
            ),
            "provider_unavailable": "The provider is down. Replay when it is back.",
            "datastore_unavailable": "The memory database is unreachable. Start the stack, then replay.",
            "vector_store_unavailable": "The vector store is unreachable. Start the stack, then replay.",
        }.get(self.code, "Fix the cause, then replay.")


class DurableProvider(MemoryProvider):
    """Wraps a provider so a failed write is queued instead of lost.

    Only `add` is changed. Reads pass straight through: a search that fails is
    retried by whoever ran it, one second later, at no cost. The asymmetry is
    the point - reads may fail fast, writes may not fail at all.
    """

    # How many queued writes one successful write will try to drain. Bounded so
    # a single `store` cannot stall for minutes because a large backlog was
    # waiting; the remainder drains on the next write, or on an explicit flush.
    DEFAULT_MAX_DRAIN = 25

    def __init__(
        self,
        inner: MemoryProvider,
        *,
        user: str,
        spool: Optional[Spool] = None,
        repository: Optional[str] = None,
        auto_drain: bool = True,
        max_drain: int = DEFAULT_MAX_DRAIN,
    ) -> None:
        self._inner = inner
        self._user = user
        self._spool = spool if spool is not None else Spool()
        self._repository = repository
        self._auto_drain = auto_drain
        self._max_drain = max_drain
        # The report from the most recent opportunistic drain, so a CLI can say
        # what happened. None means no drain ran on this call.
        self.last_drain = None

    @property
    def inner(self) -> MemoryProvider:
        return self._inner

    @property
    def spool(self) -> Spool:
        return self._spool

    def add(
        self,
        text: str,
        *,
        scope: Scope,
        scope_key: str,
        kind: str = "note",
        topic: Optional[str] = None,
        tags: Sequence[str] = (),
        confidence: float = 0.5,
        importance: float = 0.5,
        source: Optional[str] = None,
        infer: bool = False,
        created_at: Optional[Any] = None,
        idempotency_key: Optional[str] = None,
    ) -> list[MemoryRecord]:
        moment = created_at or datetime.now(timezone.utc)
        path = self._spool.enqueue(
            SpooledWrite(
                text=text,
                scope=scope,
                scope_key=scope_key,
                user=self._user,
                kind=kind,
                topic=topic,
                tags=tuple(tags),
                confidence=confidence,
                importance=importance,
                source=source,
                created_at=moment,
                repository=self._repository,
                idempotency_key=idempotency_key,
            )
        )
        # enqueue() generates the key when the caller did not supply one, and it
        # has to be the same key that reaches the server - otherwise the replay
        # check compares a key that was never stored and duplicates every time.
        queued = self._read_back(path, idempotency_key)

        try:
            records = self._inner.add(
                text,
                scope=scope,
                scope_key=scope_key,
                kind=kind,
                topic=topic,
                tags=tags,
                confidence=confidence,
                importance=importance,
                source=source,
                infer=infer,
                created_at=moment,
                idempotency_key=queued,
            )
        except BaseException as error:
            # BaseException on purpose: KeyboardInterrupt is exactly the case
            # write-ahead exists for, and letting it through here without a
            # decision would leave the entry behind with no explanation. It is
            # kept (should_queue defaults to keeping) and re-raised untouched.
            if not should_queue(error):
                self._spool.discard(path)
                raise
            if isinstance(error, Exception):
                raise WriteQueued(path, error, self._spool.pending()) from error
            raise

        self._spool.discard(path)
        self._drain_backlog()
        return records

    def _drain_backlog(self) -> None:
        """Replay what is queued, now that a write has just succeeded.

        The trigger is the right one on purpose: a successful write is direct
        evidence that whatever was blocking the queue has stopped blocking it.
        While an embedder is down every write fails, so nothing fires; the first
        write after recovery clears the backlog. No timer, no daemon, and no
        window where the queue is drainable but nobody is draining it.

        Scoped to this user's entries because that is the only identity this
        provider can write as - `user_id` is part of the search filter, so
        replaying a teammate's memory as us would hide it from them. Their
        entries wait for their own next write, or for an explicit flush.

        Never raises. A write that reached the store has succeeded, and turning
        that into an error because the *backlog* misbehaved would report the
        wrong outcome for the thing the caller actually asked for.
        """
        self.last_drain = None
        if not self._auto_drain or self._spool.pending() == 0:
            return
        try:
            self.last_drain = self._spool.flush(
                # The raw inner provider, not self: routing the replay back
                # through this wrapper would write a fresh spool entry for every
                # entry it is trying to drain.
                lambda _user: self._inner,
                user=self._user,
                limit=self._max_drain,
            )
        except Exception:  # noqa: BLE001 - see docstring
            self.last_drain = None

    def _read_back(self, path: Path, supplied: Optional[str]) -> Optional[str]:
        if supplied:
            return supplied
        try:
            import json

            return json.loads(path.read_text(encoding="utf-8")).get("idempotency_key")
        except (OSError, ValueError):
            # Without a key the replay cannot dedupe, which risks a duplicate -
            # never a loss. Carry on.
            return None

    # -- everything else is the inner provider, unchanged ------------------

    def search(self, query: SearchQuery) -> list[MemoryRecord]:
        return self._inner.search(query)

    def get(self, memory_id: str) -> MemoryRecord:
        return self._inner.get(memory_id)

    def get_all(self, *, scope: Scope, scope_key: str, top_k: int = 100) -> MemoryPage:
        return self._inner.get_all(scope=scope, scope_key=scope_key, top_k=top_k)

    def update(
        self,
        memory_id: str,
        *,
        text: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        self._inner.update(memory_id, text=text, metadata=metadata)

    def delete(self, memory_id: str) -> None:
        self._inner.delete(memory_id)

    def delete_all(self, *, scope: Scope, scope_key: str) -> int:
        return self._inner.delete_all(scope=scope, scope_key=scope_key)

    def history(self, memory_id: str) -> list[dict[str, Any]]:
        return self._inner.history(memory_id)

    def reset(self) -> None:
        self._inner.reset()

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()


def durable_from_env(inner: MemoryProvider, *, user: Optional[str] = None) -> DurableProvider:
    """Wrap a provider using the same environment the transport reads."""
    return DurableProvider(
        inner,
        user=user or os.environ.get("MEM0_USER", "unknown"),
        repository=os.environ.get("MEM0_REPOSITORY"),
    )
