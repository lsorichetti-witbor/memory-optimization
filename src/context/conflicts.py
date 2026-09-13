"""Precedence and conflict resolution.

Handoff section 13's ladder lives in `Layer.precedence()`. This module adds the
rule that matters: historical memory must not silently override current truth.

Detecting free-text contradiction reliably is not something this module can do,
and pretending otherwise would be exactly the failure this codebase exists to
catch. So the rules are deterministic and their coverage is measured:

1. `topic_supersession` - items sharing a `topic` key. The higher-precedence
   layer wins; between two memories, the newer one wins.
2. `explicit_supersession` - a memory whose envelope carries `superseded_by`.

NAMED FAILURE DIRECTION: a memory that contradicts repository truth while
sharing no `topic` key is NOT detected. Both items are injected, ordered by
precedence, and the memory is rendered with its age and confidence so a reader
can weigh it. `ConflictReport.unchecked` counts exactly those memories, so the
gap is a printed number rather than an assumption. This is a known limitation
with a visible failure mode - it is not silent.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Optional

from src.context.types import ContextItem, Layer


@dataclass(frozen=True)
class Conflict:
    winner: str
    loser: str
    rule: str
    topic: Optional[str] = None


@dataclass
class ConflictReport:
    conflicts: list[Conflict] = field(default_factory=list)
    checked: int = 0
    unchecked: int = 0

    @property
    def memories(self) -> int:
        return self.checked + self.unchecked

    def summary(self) -> str:
        return (
            f"{self.checked} of {self.memories} memories checked for conflicts, "
            f"{len(self.conflicts)} conflict{'' if len(self.conflicts) == 1 else 's'} found, "
            f"{self.unchecked} unchecked"
        )


def _sort_key(item: ContextItem) -> tuple[int, float]:
    """Lower is more authoritative: precedence first, then newer."""
    created = item.created_at
    if created and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    timestamp = created.timestamp() if created else 0.0
    return (Layer.precedence_index(item.layer), -timestamp)


class ConflictResolver:
    def run(self, items: list[ContextItem]) -> tuple[list[ContextItem], ConflictReport]:
        report = ConflictReport()
        resolved = {item.id: item for item in items}

        memories = [i for i in items if i.layer is Layer.MEMORY]

        # Rule 2 first: an explicit supersession does not depend on a topic.
        explicitly_superseded: set[str] = set()
        for memory in memories:
            replacement = memory.metadata.get("superseded_by")
            if replacement:
                explicitly_superseded.add(memory.id)
                resolved[memory.id] = resolved[memory.id].with_signals(staleness=1.0)
                resolved[memory.id].metadata["overruled_by"] = replacement
                report.conflicts.append(
                    Conflict(winner=replacement, loser=memory.id, rule="explicit_supersession")
                )

        # Rule 1: topic groups.
        by_topic: dict[str, list[ContextItem]] = {}
        for item in items:
            topic = item.metadata.get("topic")
            if topic:
                by_topic.setdefault(topic, []).append(item)

        for topic, group in by_topic.items():
            ordered = sorted(group, key=_sort_key)
            winner = ordered[0]
            for loser in ordered[1:]:
                if loser.layer is not Layer.MEMORY:
                    # Only memory can lose. Instructions and repository truth are
                    # never marked stale by anything downstream of them.
                    continue
                resolved[loser.id] = resolved[loser.id].with_signals(staleness=1.0)
                resolved[loser.id].metadata["overruled_by"] = winner.id
                report.conflicts.append(
                    Conflict(winner=winner.id, loser=loser.id, rule="topic_supersession", topic=topic)
                )

        for memory in memories:
            if memory.metadata.get("topic") or memory.id in explicitly_superseded:
                report.checked += 1
            else:
                report.unchecked += 1

        return [resolved[item.id] for item in items], report
