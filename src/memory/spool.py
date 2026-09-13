"""Offline spool for memory writes.

When the Mem0 server is unreachable, a write would otherwise be lost at the
moment it was worth keeping - you notice something, the stack happens to be
down, and the observation evaporates. The spool catches those writes on disk and
replays them once the server is back.

Three properties it has to have, each because the alternative fails silently:

**One shared location for every repository and scope.** A per-repo spool would
strand a memory in whichever checkout you happened to be in, and you would never
think to look there. Entries carry their own scope, scope key and user, so one
directory serves every repo, the global scope, and every user on the machine.

**The original timestamp survives.** Replaying with the replay time as
`created_at` would make a month-old memory look brand new, and recency is a
ranking signal. The queued time is what gets written.

**Nothing is deleted until it is confirmed stored.** A file is removed only
after the provider accepts it. A failed replay - or a file that will not parse -
is left where it is and counted, because deleting it would destroy the only copy
and the next run would simply show an empty spool.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

from src.memory.errors import InvalidWrite, Mem0Error
from src.memory.scopes import Scope

# Shared by every repository, so a queued write is never stranded in one
# checkout. Override with CONTEXT_MEMORY_SPOOL.
DEFAULT_SPOOL_ENV = "CONTEXT_MEMORY_SPOOL"

# After this many failed replays an entry moves to `dead/`. It is never deleted:
# a write that cannot be stored is still the only copy of something somebody
# thought was worth keeping, so the end of the retry road is a human looking at
# it, not a silent drop.
DEFAULT_MAX_ATTEMPTS = 10

# Retry spacing, so a flush on a cron does not hammer a provider that is out of
# quota for the day. Manual flushes can ignore it with force=True.
BACKOFF_CAP_MINUTES = 60


def default_spool_root() -> Path:
    configured = os.environ.get(DEFAULT_SPOOL_ENV)
    if configured:
        return Path(configured)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / "context-memory" / "spool"
    return Path.home() / ".context-memory" / "spool"


@dataclass(frozen=True)
class SpooledWrite:
    text: str
    scope: Scope
    scope_key: str
    user: str
    kind: str = "note"
    topic: Optional[str] = None
    tags: tuple[str, ...] = ()
    confidence: float = 0.5
    importance: float = 0.5
    source: Optional[str] = None
    created_at: Optional[datetime] = None
    repository: Optional[str] = None

    # Stored with the memory so a replay can tell "never stored" from "stored,
    # but the confirmation never got back to me". Without it, a process killed
    # between the server committing and the client deleting the spool file
    # produces a duplicate on the next flush.
    idempotency_key: Optional[str] = None

    # Replay bookkeeping. Kept in the same file as the payload so an entry is
    # one self-describing artifact; a sidecar could be lost separately and the
    # attempt count would silently reset.
    attempts: int = 0
    last_error: Optional[str] = None
    last_attempt_at: Optional[datetime] = None

    def to_json(self) -> dict:
        data = asdict(self)
        data["scope"] = self.scope.value
        data["tags"] = list(self.tags)
        data["created_at"] = self.created_at.isoformat() if self.created_at else None
        data["last_attempt_at"] = self.last_attempt_at.isoformat() if self.last_attempt_at else None
        return data

    @classmethod
    def from_json(cls, data: dict) -> "SpooledWrite":
        created = data.get("created_at")
        attempted = data.get("last_attempt_at")
        return cls(
            text=data["text"],
            scope=Scope.parse(data["scope"]),
            scope_key=data["scope_key"],
            user=data["user"],
            kind=data.get("kind", "note"),
            topic=data.get("topic"),
            tags=tuple(data.get("tags") or ()),
            confidence=float(data.get("confidence", 0.5)),
            importance=float(data.get("importance", 0.5)),
            source=data.get("source"),
            created_at=datetime.fromisoformat(created) if created else None,
            repository=data.get("repository"),
            # Absent in files queued before these fields existed. Defaulting to
            # "never tried" is right: an entry from the old format has not been
            # counted against the attempt limit.
            idempotency_key=data.get("idempotency_key"),
            attempts=int(data.get("attempts", 0)),
            last_error=data.get("last_error"),
            last_attempt_at=datetime.fromisoformat(attempted) if attempted else None,
        )

    def next_attempt_at(self) -> Optional[datetime]:
        """When this entry becomes eligible again, or None if it is eligible now."""
        if not self.attempts or self.last_attempt_at is None:
            return None
        minutes = min(2**self.attempts, BACKOFF_CAP_MINUTES)
        return self.last_attempt_at + timedelta(minutes=minutes)


@dataclass
class FlushReport:
    total: int = 0
    replayed: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    # Already present server-side, recognised by idempotency key. Counted apart
    # from `replayed` so a duplicate that was correctly skipped cannot be read
    # as a write that was stored on this run.
    already_stored: int = 0
    # Not attempted because backoff has not elapsed. Counted and named, never
    # folded into `failed`: "not tried yet" and "tried and failed" call for
    # different actions.
    deferred: int = 0
    # Moved to `dead/` after exhausting the attempt limit. Still on disk.
    dead_lettered: int = 0

    @property
    def outstanding(self) -> int:
        """Everything still waiting, for any reason."""
        return self.failed + self.deferred + self.dead_lettered

    def summary(self) -> str:
        parts = [f"{self.replayed} of {self.total} spooled writes replayed"]
        if self.already_stored:
            parts.append(f"{self.already_stored} already stored")
        if self.failed:
            parts.append(f"{self.failed} failed and still queued")
        if self.deferred:
            parts.append(f"{self.deferred} waiting on backoff")
        if self.dead_lettered:
            parts.append(f"{self.dead_lettered} moved to dead/")
        if len(parts) == 1:
            parts.append(f"{self.outstanding} still queued")
        return ", ".join(parts)


class Spool:
    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = Path(root) if root is not None else default_spool_root()

    @property
    def root(self) -> Path:
        return self._root

    def _files(self) -> list[Path]:
        if not self._root.is_dir():
            return []
        # Filenames lead with a sortable UTC timestamp, so lexical order is
        # chronological order and replay happens oldest-first.
        return sorted(self._root.glob("*.json"))

    @property
    def dead_root(self) -> Path:
        """Where entries land after exhausting their attempts. Never emptied here."""
        return self._root / "dead"

    def pending(self) -> int:
        return len(self._files())

    def dead(self) -> int:
        if not self.dead_root.is_dir():
            return 0
        return len(list(self.dead_root.glob("*.json")))

    def enqueue(self, write: SpooledWrite) -> Path:
        if not write.text or not write.text.strip():
            raise InvalidWrite("refusing to spool a write with no text")

        self._root.mkdir(parents=True, exist_ok=True)
        if write.idempotency_key is None:
            write = replace(write, idempotency_key=uuid.uuid4().hex)
        stamp = (write.created_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        # The timestamp alone is not unique - two writes in the same second would
        # overwrite each other, and the loss would be silent.
        name = f"{stamp.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}.json"
        path = self._root / name
        self._write_atomic(path, write)
        return path

    @staticmethod
    def _write_atomic(path: Path, write: SpooledWrite) -> None:
        """Write via a temp file and replace, so a crash cannot leave a half file.

        The whole point of the write-ahead entry is to survive a process dying
        at a bad moment. A truncated JSON file would survive as an unreadable
        entry, which flush reports but cannot replay - the write would be
        visible and still lost.
        """
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(write.to_json(), indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def discard(self, path: Path) -> None:
        """Remove an entry. Only for a write that is confirmed stored, or unstorable."""
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass

    def entries(self) -> list[SpooledWrite]:
        out: list[SpooledWrite] = []
        for path in self._files():
            try:
                out.append(SpooledWrite.from_json(json.loads(path.read_text(encoding="utf-8"))))
            except (ValueError, KeyError):
                # Surfaced by flush(), which reports it rather than deleting it.
                continue
        return out

    def flush(
        self,
        provider_factory: Callable[[str], object],
        *,
        force: bool = False,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        now: Optional[datetime] = None,
    ) -> FlushReport:
        """Replay every queued write. `provider_factory(user)` returns a provider.

        The factory takes the user so each entry is replayed as its own author:
        `user_id` is part of the search filter, and replaying someone else's
        memory under your identity would hide it from them.

        `force` ignores backoff, for a human who has just fixed the cause and
        does not want to wait. `max_attempts` bounds the retry loop; an entry
        past it moves to `dead/` rather than being retried forever or deleted.
        """
        files = self._files()
        report = FlushReport(total=len(files))
        providers: dict[str, object] = {}
        moment = now or datetime.now(timezone.utc)
        seen_keys: dict[tuple[str, str, str], set[str]] = {}

        for path in files:
            try:
                write = SpooledWrite.from_json(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, KeyError, OSError) as error:
                report.failed += 1
                report.errors.append(f"{path.name}: unreadable ({error})")
                continue

            due = write.next_attempt_at()
            if not force and due is not None and due > moment:
                report.deferred += 1
                report.errors.append(
                    f"{path.name}: not retried yet, eligible at {due.strftime('%H:%M:%SZ')} "
                    f"(attempt {write.attempts}); --force overrides"
                )
                continue

            try:
                provider = providers.get(write.user)
                if provider is None:
                    provider = provider_factory(write.user)
                    providers[write.user] = provider

                # Was this stored by an earlier run whose confirmation never got
                # back? Checking costs one listing per scope, and skipping it
                # would mean every crash between commit and unlink silently
                # duplicates a memory.
                if write.idempotency_key and self._already_stored(provider, write, seen_keys):
                    report.already_stored += 1
                    self.discard(path)
                    continue

                provider.add(
                    write.text,
                    scope=write.scope,
                    scope_key=write.scope_key,
                    kind=write.kind,
                    topic=write.topic,
                    tags=write.tags,
                    confidence=write.confidence,
                    importance=write.importance,
                    source=write.source,
                    created_at=write.created_at,
                    idempotency_key=write.idempotency_key,
                )
            except Exception as error:  # noqa: BLE001 - any failure must keep the file
                attempted = replace(
                    write,
                    attempts=write.attempts + 1,
                    last_error=str(error)[:500],
                    last_attempt_at=moment,
                )
                if attempted.attempts >= max_attempts:
                    destination = self._to_dead(path, attempted)
                    report.dead_lettered += 1
                    report.errors.append(
                        f"{path.name}: failed {attempted.attempts} times, moved to "
                        f"{destination} and NOT deleted - {error}"
                    )
                else:
                    self._write_atomic(path, attempted)
                    report.failed += 1
                    report.errors.append(f"{path.name}: attempt {attempted.attempts} - {error}")
                continue

            # Only now: the write is confirmed stored.
            try:
                path.unlink()
            except OSError as error:
                report.errors.append(f"{path.name}: replayed but could not be removed ({error})")
            report.replayed += 1

        for provider in providers.values():
            close = getattr(provider, "close", None)
            if callable(close):
                close()

        return report

    @staticmethod
    def _already_stored(
        provider: object,
        write: SpooledWrite,
        cache: dict[tuple[str, str, str], set[str]],
    ) -> bool:
        """Is this exact write already in the store, by idempotency key?

        Uses `get_all`, which lists a scope and needs no embedding - so this
        still works during the very outage that queued the write, which is when
        it matters. One listing per (user, scope, key), cached across entries.

        Fails toward re-storing: if the listing itself errors, this returns
        False and the write is attempted. A duplicate is recoverable and a lost
        write is not, so an unreadable answer must not be read as "already
        there".
        """
        cache_key = (write.user, write.scope.value, write.scope_key)
        keys = cache.get(cache_key)
        if keys is None:
            try:
                page = provider.get_all(scope=write.scope, scope_key=write.scope_key, top_k=1000)
                keys = {
                    str(record.envelope.extra["idempotency_key"])
                    for record in page.records
                    if record.envelope.extra.get("idempotency_key")
                }
            except Exception:  # noqa: BLE001 - see docstring: unknown means "not stored"
                keys = set()
            cache[cache_key] = keys
        return write.idempotency_key in keys

    def _to_dead(self, path: Path, write: SpooledWrite) -> Path:
        """Move an exhausted entry to `dead/`, keeping its failure history."""
        self.dead_root.mkdir(parents=True, exist_ok=True)
        destination = self.dead_root / path.name
        self._write_atomic(destination, write)
        self.discard(path)
        return destination


def should_queue(error: BaseException) -> bool:
    """Keep this write for later, or drop it?

    **The default is keep**, and that inversion is the whole point. This used to
    be `is_unreachable`, which matched the message text against a list of
    strings - `"Connection refused"`, `"[502]"`, `"[503]"`, `"[504]"` - and
    dropped the write on anything else. Measured against that list, a plain
    `500`, an `httpx.RemoteProtocolError`, a `PoolTimeout`, a proxy's HTML error
    page and a bare `429` were all silently discarded, and each one is an
    ordinary embedding failure. The quota case only survived because the server
    happens to wrap a 429 as a 502.

    So the question is no longer "did I recognise this failure?" but "did the
    server tell me this write is malformed?" - because that is the only answer
    that makes keeping it pointless. The two mistakes cost very different
    amounts: wrongly keeping a write leaves a file somebody deletes, wrongly
    dropping one destroys the only copy of an observation. An error nobody has
    seen before has to land on the cheap side.
    """
    if isinstance(error, InvalidWrite):
        return False
    if isinstance(error, Mem0Error):
        return not error.permanent
    return True
