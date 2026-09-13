"""The metadata envelope every stored memory carries.

Mem0 returns everything outside its reserved payload keys as `metadata`
(server/main.py, _serialize_memory), so the envelope has to be flat and
JSON-scalar only. Tags are stored one boolean key per member rather than as a
delimited string: a set flattened into `"a|b|c"` can only be matched by an
exact-combination query, never by membership.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from src.memory.lifecycle import Lifecycle, parse_lifecycle
from src.memory.scopes import Scope

_TAG_PREFIX = "tag_"

_KNOWN_KEYS = {
    "scope",
    "scope_key",
    "kind",
    "lifecycle",
    "confidence",
    "importance",
    "topic",
    "source",
    "created_at",
    "superseded_by",
    "lifecycle_changed_at",
}

_UNIT_FIELDS = ("confidence", "importance")


@dataclass(frozen=True)
class Envelope:
    scope: Scope = Scope.GLOBAL
    scope_key: str = "global"
    kind: str = "note"
    lifecycle: str = Lifecycle.CANDIDATE.value
    confidence: float = 0.5
    importance: float = 0.5
    topic: Optional[str] = None
    source: Optional[str] = None
    created_at: Optional[datetime] = None
    tags: tuple[str, ...] = ()
    superseded_by: Optional[str] = None
    lifecycle_changed_at: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        parse_lifecycle(self.lifecycle)
        for name in _UNIT_FIELDS:
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1], got {value}")
        # Tags are a set: order carries no meaning and it cannot survive the
        # one-key-per-tag encoding. Normalise so equality and round-trips hold.
        object.__setattr__(self, "tags", tuple(sorted(set(self.tags))))


def encode_envelope(envelope: Envelope) -> dict[str, Any]:
    """Flatten an envelope into Mem0 metadata."""
    encoded: dict[str, Any] = {
        "scope": envelope.scope.value,
        "scope_key": envelope.scope_key,
        "kind": envelope.kind,
        "lifecycle": envelope.lifecycle,
        "confidence": envelope.confidence,
        "importance": envelope.importance,
    }
    if envelope.topic:
        encoded["topic"] = envelope.topic
    if envelope.source:
        encoded["source"] = envelope.source
    if envelope.created_at:
        encoded["created_at"] = envelope.created_at.isoformat()
    if envelope.superseded_by:
        encoded["superseded_by"] = envelope.superseded_by
    if envelope.lifecycle_changed_at:
        encoded["lifecycle_changed_at"] = envelope.lifecycle_changed_at
    for tag in envelope.tags:
        encoded[f"{_TAG_PREFIX}{tag}"] = True
    for key, value in envelope.extra.items():
        encoded[key] = value
    return encoded


def decode_envelope(metadata: dict[str, Any] | None) -> Envelope:
    """Rebuild an envelope, keeping metadata written by anything else in `extra`."""
    metadata = dict(metadata or {})

    tags = tuple(
        sorted(key[len(_TAG_PREFIX) :] for key, value in metadata.items() if key.startswith(_TAG_PREFIX) and value)
    )
    extra = {
        key: value
        for key, value in metadata.items()
        if key not in _KNOWN_KEYS and not key.startswith(_TAG_PREFIX)
    }

    lifecycle = metadata.get("lifecycle", Lifecycle.CANDIDATE.value)
    parse_lifecycle(lifecycle)

    created_at = metadata.get("created_at")
    parsed_created_at = None
    if isinstance(created_at, str) and created_at:
        try:
            parsed_created_at = datetime.fromisoformat(created_at)
        except ValueError:
            # Keep the unparseable value visible instead of dropping it.
            extra["created_at_raw"] = created_at

    return Envelope(
        scope=Scope.parse(metadata.get("scope", Scope.GLOBAL.value)),
        scope_key=metadata.get("scope_key", "global"),
        kind=metadata.get("kind", "note"),
        lifecycle=lifecycle,
        confidence=float(metadata.get("confidence", 0.5)),
        importance=float(metadata.get("importance", 0.5)),
        topic=metadata.get("topic"),
        source=metadata.get("source"),
        created_at=parsed_created_at,
        tags=tags,
        superseded_by=metadata.get("superseded_by"),
        lifecycle_changed_at=metadata.get("lifecycle_changed_at"),
        extra=extra,
    )
