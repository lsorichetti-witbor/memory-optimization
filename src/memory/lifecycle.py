"""Memory lifecycle: candidate -> durable -> stale -> superseded.

The transition table is explicit. An implicit one lets any state reach any
other, and the illegal transition then surfaces months later as a data bug with
no diff to explain it.
"""

from __future__ import annotations

from enum import Enum


class Lifecycle(str, Enum):
    CANDIDATE = "candidate"
    DURABLE = "durable"
    STALE = "stale"
    SUPERSEDED = "superseded"


LIFECYCLES: tuple[str, ...] = tuple(state.value for state in Lifecycle)

_ALLOWED: dict[Lifecycle, frozenset[Lifecycle]] = {
    Lifecycle.CANDIDATE: frozenset({Lifecycle.DURABLE, Lifecycle.STALE, Lifecycle.SUPERSEDED}),
    Lifecycle.DURABLE: frozenset({Lifecycle.STALE, Lifecycle.SUPERSEDED}),
    # stale -> durable is revalidation: the memory was checked again and still holds.
    Lifecycle.STALE: frozenset({Lifecycle.DURABLE, Lifecycle.SUPERSEDED}),
    Lifecycle.SUPERSEDED: frozenset(),
}


def parse_lifecycle(value: str) -> Lifecycle:
    try:
        return Lifecycle(value)
    except ValueError:
        raise ValueError(f"unknown lifecycle: {value!r}. Known: {', '.join(LIFECYCLES)}") from None


def can_transition(source: Lifecycle, target: Lifecycle) -> bool:
    return target in _ALLOWED[source]


def transition(
    source: Lifecycle,
    target: Lifecycle,
    at: str,
    superseded_by: str | None = None,
) -> dict[str, object]:
    """Return the metadata patch to write, or raise before anything is written."""
    if not can_transition(source, target):
        raise ValueError(f"cannot transition {source.value} -> {target.value}")
    if target is Lifecycle.SUPERSEDED and not superseded_by:
        # Without a pointer to the replacement, the reason for the supersession
        # is lost and the memory cannot be traced forward.
        raise ValueError("superseded_by is required when moving a memory to superseded")

    patch: dict[str, object] = {"lifecycle": target.value, "lifecycle_changed_at": at}
    if superseded_by:
        patch["superseded_by"] = superseded_by
    return patch
