"""Memory layer: Mem0 results turned into context items.

Queried narrow-to-broad so the most specific memory is seen first; the same
memory surfacing from two scopes keeps the narrowest occurrence.

Superseded memories are dropped - something explicitly replaced them. Stale ones
are kept and flagged, because the compiler has to be able to show a reader that
a memory disagreed with current truth rather than pretending it never existed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from src.context.sources.base import ContextSource
from src.context.types import ContextItem, Layer, Signals, Task
from src.memory.base import MemoryProvider
from src.memory.lifecycle import Lifecycle
from src.memory.scopes import ScopeSelector
from src.memory.types import MemoryRecord, SearchQuery

# Half-life-ish decay: a 90 day old memory scores 0.5 on recency.
RECENCY_HALF_LIFE_DAYS = 90.0


class MemoryContextSource(ContextSource):
    def __init__(
        self,
        provider: MemoryProvider,
        selector: ScopeSelector,
        top_k: int = 10,
        threshold: Optional[float] = None,
    ) -> None:
        self._provider = provider
        self._selector = selector
        self._top_k = top_k
        self._threshold = threshold
        self.last_error: Optional[BaseException] = None

    @property
    def name(self) -> str:
        return "memory"

    @property
    def layer(self) -> Layer:
        return Layer.MEMORY

    @staticmethod
    def _age_days(record: MemoryRecord) -> Optional[int]:
        created = record.created_at or record.envelope.created_at
        if not created:
            return None
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - created).days)

    def _recency(self, record: MemoryRecord) -> float:
        age = self._age_days(record)
        if age is None:
            return 0.0
        return 1.0 / (1.0 + age / RECENCY_HALF_LIFE_DAYS)

    def _to_item(self, record: MemoryRecord) -> ContextItem:
        envelope = record.envelope
        age = self._age_days(record)
        metadata = {
            "scope": envelope.scope.value,
            "scope_key": envelope.scope_key,
            "kind": envelope.kind,
            "lifecycle": envelope.lifecycle,
            "topic": envelope.topic,
            "age_days": age,
        }
        if envelope.superseded_by:
            metadata["superseded_by"] = envelope.superseded_by
        return ContextItem(
            id=record.id,
            layer=Layer.MEMORY,
            content=record.text,
            source="mem0",
            title=envelope.topic or envelope.kind,
            created_at=record.created_at or envelope.created_at,
            signals=Signals(
                # The server already scored this by embedding similarity. A record
                # with no score is scored 0, not 1: absent evidence is not strong
                # evidence.
                relevance=float(record.score) if record.score is not None else 0.0,
                confidence=envelope.confidence,
                importance=envelope.importance,
                recency=self._recency(record),
                staleness=1.0 if envelope.lifecycle == Lifecycle.STALE.value else 0.0,
            ),
            metadata=metadata,
        )

    def collect(self, task: Task) -> list[ContextItem]:
        self.last_error = None
        items: list[ContextItem] = []
        seen: set[str] = set()

        try:
            for scope in self._selector.scopes_to_query():
                key = self._selector.key_for(scope)
                if not key:
                    continue
                query = SearchQuery(
                    query=task.query_text(),
                    scope=scope,
                    scope_key=key,
                    top_k=self._top_k,
                    threshold=self._threshold,
                    repository=self._selector.repository,
                    project=self._selector.project,
                )
                for record in self._provider.search(query):
                    if record.id in seen:
                        continue
                    if record.envelope.lifecycle == Lifecycle.SUPERSEDED.value:
                        continue
                    seen.add(record.id)
                    items.append(self._to_item(record))
        except Exception as error:  # noqa: BLE001 - the manager reports this, it must not crash the build
            self.last_error = error
            return []
        return items
