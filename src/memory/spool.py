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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

from src.memory.scopes import Scope

# Shared by every repository, so a queued write is never stranded in one
# checkout. Override with CONTEXT_MEMORY_SPOOL.
DEFAULT_SPOOL_ENV = "CONTEXT_MEMORY_SPOOL"


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

    def to_json(self) -> dict:
        data = asdict(self)
        data["scope"] = self.scope.value
        data["tags"] = list(self.tags)
        data["created_at"] = self.created_at.isoformat() if self.created_at else None
        return data

    @classmethod
    def from_json(cls, data: dict) -> "SpooledWrite":
        created = data.get("created_at")
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
        )


@dataclass
class FlushReport:
    total: int = 0
    replayed: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.replayed} of {self.total} spooled writes replayed, "
            f"{self.failed} still queued"
        )


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

    def pending(self) -> int:
        return len(self._files())

    def enqueue(self, write: SpooledWrite) -> Path:
        if not write.text or not write.text.strip():
            raise ValueError("refusing to spool a write with no text")

        self._root.mkdir(parents=True, exist_ok=True)
        stamp = (write.created_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        # The timestamp alone is not unique - two writes in the same second would
        # overwrite each other, and the loss would be silent.
        name = f"{stamp.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}.json"
        path = self._root / name
        path.write_text(json.dumps(write.to_json(), indent=2), encoding="utf-8")
        return path

    def entries(self) -> list[SpooledWrite]:
        out: list[SpooledWrite] = []
        for path in self._files():
            try:
                out.append(SpooledWrite.from_json(json.loads(path.read_text(encoding="utf-8"))))
            except (ValueError, KeyError):
                # Surfaced by flush(), which reports it rather than deleting it.
                continue
        return out

    def flush(self, provider_factory: Callable[[str], object]) -> FlushReport:
        """Replay every queued write. `provider_factory(user)` returns a provider.

        The factory takes the user so each entry is replayed as its own author:
        `user_id` is part of the search filter, and replaying someone else's
        memory under your identity would hide it from them.
        """
        files = self._files()
        report = FlushReport(total=len(files))
        providers: dict[str, object] = {}

        for path in files:
            try:
                write = SpooledWrite.from_json(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, KeyError, OSError) as error:
                report.failed += 1
                report.errors.append(f"{path.name}: unreadable ({error})")
                continue

            try:
                provider = providers.get(write.user)
                if provider is None:
                    provider = provider_factory(write.user)
                    providers[write.user] = provider
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
                )
            except Exception as error:  # noqa: BLE001 - any failure must keep the file
                report.failed += 1
                report.errors.append(f"{path.name}: {error}")
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


def is_unreachable(error: BaseException) -> bool:
    """Is this the server being down, rather than the write being wrong?

    A 400 means the write itself is bad and spooling it would just queue a
    failure forever. Only a transport-level failure is worth retrying later.
    """
    import httpx

    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.NetworkError)):
        return True
    text = str(error)
    return any(
        marker in text
        for marker in ("Connection refused", "10061", "Max retries", "Failed to establish", "[502]", "[503]", "[504]")
    )
