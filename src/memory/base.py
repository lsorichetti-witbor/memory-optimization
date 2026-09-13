"""The memory provider interface.

Kept deliberately small and separate from the Context Manager so an alternative
memory implementation can be evaluated against Mem0 (handoff section 10).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional, Sequence

from src.memory.scopes import Scope
from src.memory.types import MemoryPage, MemoryRecord, SearchQuery


class MemoryProvider(ABC):
    @abstractmethod
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
    ) -> list[MemoryRecord]:
        """Store a memory. Raises if the backend accepted the call but stored nothing."""

    @abstractmethod
    def search(self, query: SearchQuery) -> list[MemoryRecord]:
        """Retrieve memories relevant to a query within one scope."""

    @abstractmethod
    def get(self, memory_id: str) -> MemoryRecord:
        """Fetch one memory by id."""

    @abstractmethod
    def get_all(self, *, scope: Scope, scope_key: str, top_k: int = 100) -> MemoryPage:
        """List memories in a scope, reporting the limit so truncation is visible."""

    @abstractmethod
    def update(
        self,
        memory_id: str,
        *,
        text: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Patch a memory. Only the fields passed are sent."""

    @abstractmethod
    def delete(self, memory_id: str) -> None:
        """Delete one memory."""

    @abstractmethod
    def delete_all(self, *, scope: Scope, scope_key: str) -> int:
        """Delete every memory in a scope, returning how many were removed.

        Implementations must not over-reach: a scope the backend cannot filter
        on has to be enumerated and deleted by id, never wiped with a broader
        bulk call.
        """

    @abstractmethod
    def history(self, memory_id: str) -> list[dict[str, Any]]:
        """Return the backend's change history for a memory."""

    @abstractmethod
    def reset(self) -> None:
        """Drop everything. Destructive."""
