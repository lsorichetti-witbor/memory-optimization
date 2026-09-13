"""Types shared by every context source and every pipeline stage.

`Signals` is deliberately a flat record of independent components rather than a
single number: handoff section 12 requires each component to be observable on
its own, so a retrieval failure can be told apart from a ranking failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class Layer(str, Enum):
    INSTRUCTIONS = "instructions"
    REPOSITORY = "repository"
    CURRENT_STATE = "current_state"
    MEMORY = "memory"

    @classmethod
    def precedence(cls) -> list["Layer"]:
        """Handoff section 13, highest authority first.

        Current state outranks repository truth because a file the task is
        actively editing is more current than the committed version of it.
        """
        return [cls.INSTRUCTIONS, cls.CURRENT_STATE, cls.REPOSITORY, cls.MEMORY]

    @classmethod
    def precedence_index(cls, layer: "Layer") -> int:
        return cls.precedence().index(layer)


@dataclass(frozen=True)
class Task:
    description: str
    files: tuple[str, ...] = ()
    repository: Optional[str] = None
    branch: Optional[str] = None
    keywords: tuple[str, ...] = ()

    def query_text(self) -> str:
        """What the lexical scorer matches against."""
        return " ".join([self.description, *self.keywords, *self.files])


@dataclass(frozen=True)
class Signals:
    """Every component of the score, each in [0, 1].

    Penalties (`redundancy`, `staleness`) are stored as magnitudes; the ranker
    applies their sign. Keeping them positive here means a signal can never be
    read as "less stale than nothing".
    """

    relevance: float = 0.0
    task_scope_match: float = 0.0
    project_scope_match: float = 0.0
    confidence: float = 0.0
    recency: float = 0.0
    importance: float = 0.0
    redundancy: float = 0.0
    staleness: float = 0.0

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1], got {value}")


@dataclass(frozen=True)
class ScoreBreakdown:
    total: float
    contributions: dict[str, float]


@dataclass
class ContextItem:
    id: str
    layer: Layer
    content: str
    source: str
    title: str = ""
    signals: Signals = field(default_factory=Signals)
    breakdown: Optional[ScoreBreakdown] = None
    tokens: int = 0
    created_at: Optional[datetime] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    pinned: bool = False

    @property
    def score(self) -> float:
        return self.breakdown.total if self.breakdown else 0.0

    def with_signals(self, **changes: float) -> "ContextItem":
        return replace(self, signals=replace(self.signals, **changes))
