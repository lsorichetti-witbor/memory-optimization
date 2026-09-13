"""Data types every MemoryProvider speaks.

Providers return these, never raw JSON, so a second implementation can be
benchmarked against Mem0 without the Context Manager noticing (handoff s10).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from src.memory.envelope import Envelope
from src.memory.scopes import Scope


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    text: str
    envelope: Envelope = field(default_factory=Envelope)
    score: Optional[float] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    raw: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class SearchQuery:
    query: str
    scope: Scope
    scope_key: str
    top_k: int = 10
    threshold: Optional[float] = None
    repository: Optional[str] = None
    project: Optional[str] = None


@dataclass(frozen=True)
class MemoryPage:
    """A page of memories plus the denominator needed to read it honestly.

    `truncated` is true when the server returned exactly as many rows as were
    asked for: there may be more, and a caller that reports `returned` as a
    total would be understating it.
    """

    records: tuple[MemoryRecord, ...]
    limit: int
    returned: int
    truncated: bool
