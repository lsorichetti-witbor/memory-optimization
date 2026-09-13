"""The context source interface.

Sources produce candidates and nothing else. They never rank and never budget:
those are pipeline concerns, and a source that pre-filtered would hide the
reason an item did not make it into the final context.

Every source carries `last_error`. A source that failed must be distinguishable
from a source that genuinely had nothing to contribute - otherwise a dead Mem0
server looks exactly like an empty memory store.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from src.context.types import ContextItem, Layer, Task


class ContextSource(ABC):
    last_error: Optional[BaseException] = None

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def layer(self) -> Layer: ...

    @abstractmethod
    def collect(self, task: Task) -> list[ContextItem]:
        """Return candidate items. Never raises: failures land in `last_error`."""
