"""Phase 5: turn what happened in a session into candidate memories.

Two rules shape this module.

**Nothing is stored automatically.** Handoff section 7 is explicit that not every
observation should become permanent memory. Everything produced here is a
`Candidate` at lifecycle `candidate`; a `Candidate` cannot even be constructed
at `durable`, so the review gate cannot be bypassed by accident.

**Nothing the repository already says is worth storing.** Repository truth is
retrieved live by `RepositorySource`. A copy in Mem0 only creates a second
version to go stale, so `filter_storable` rejects candidates that merely restate
it - and counts the rejection rather than dropping it quietly.

The heuristic extractor here is deliberately narrow and deterministic. It reads
structured session artifacts, not prose. An LLM-backed extractor can implement
the same `Extractor` protocol later; keeping the interface small is what makes
that swap cheap, and keeping this one deterministic is what makes it testable
without a model in the loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional, Protocol, Sequence

from src.context.lexical import tokenize
from src.memory.lifecycle import Lifecycle
from src.memory.scopes import Scope

KINDS = ("decision", "discovery", "lesson", "convention", "failure", "incident", "note")

# Shorter than this is not a fact, it is a fragment: "it broke" tells a future
# reader nothing they can act on.
MIN_TEXT_CHARS = 25

# Jaccard threshold for "this candidate restates something".
RESTATEMENT_THRESHOLD = 0.6
DEDUPE_THRESHOLD = 0.8

_BREAKING_COMMIT = re.compile(r"^(?P<type>\w+)(?:\([^)]*\))?!:\s*(?P<subject>.+)$", re.M)
_FIX_COMMIT = re.compile(r"^fix(?:\([^)]*\))?:\s*(?P<subject>.+)$", re.M)
# No DOTALL: the cause is the rest of ITS paragraph, not the rest of the commit.
# With DOTALL this swallowed every later paragraph, measured on a real commit.
_CAUSE = re.compile(r"\b(?:because|caused by|due to|root cause)\b\s*(?P<cause>[^\n]+(?:\n(?!\s*\n)[^\n]+)*)", re.I)

# Git trailers are metadata about the commit, not knowledge from it.
_TRAILER = re.compile(
    r"^(?:Co-Authored-By|Signed-off-by|Claude-Session|Reviewed-by|Acked-by|Cc|Refs|Closes|Fixes)\s*:.*$",
    re.I | re.M,
)
# A dotted or slashed identifier is a usable conflict topic: server/main.py,
# server.default_provider, pgvector.embedding_dims.
_TOPIC_TOKEN = re.compile(r"\b([a-z_][a-z0-9_]*(?:[./][a-z0-9_]+)+)\b", re.I)


@dataclass(frozen=True)
class SessionArtifacts:
    """Structured record of a session. Deliberately not a transcript."""

    summary: str = ""
    commits: tuple[str, ...] = ()
    test_failures: tuple[str, ...] = ()
    tool_errors: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    repository: Optional[str] = None
    scope: Scope = Scope.REPOSITORY


@dataclass
class Candidate:
    text: str
    kind: str
    scope: Scope
    scope_key: str
    evidence: str
    topic: Optional[str] = None
    confidence: float = 0.5
    importance: float = 0.5
    lifecycle: str = Lifecycle.CANDIDATE.value
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown kind: {self.kind!r}. Known: {', '.join(KINDS)}")
        if self.lifecycle != Lifecycle.CANDIDATE.value:
            raise ValueError(
                "extraction may only produce lifecycle 'candidate'. "
                "Promotion to durable is a separate, deliberate step."
            )


@dataclass
class ExtractionReport:
    considered: int = 0
    storable: int = 0
    rejected: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        reasons = ", ".join(f"{k} {v}" for k, v in sorted(self.rejected.items()))
        total_rejected = sum(self.rejected.values())
        tail = f" ({reasons})" if reasons else ""
        return (
            f"{self.storable} of {self.considered} candidates storable, "
            f"{total_rejected} rejected{tail}"
        )


class Extractor(Protocol):
    def extract(self, artifacts: SessionArtifacts) -> list[Candidate]: ...


def _topic_for(text: str) -> Optional[str]:
    match = _TOPIC_TOKEN.search(text)
    if not match:
        return None
    return match.group(1).replace("/", ".")


def _shingles(text: str, size: int = 3) -> frozenset[tuple[str, ...]]:
    tokens = tokenize(text)
    if len(tokens) < size:
        return frozenset({tuple(tokens)})
    return frozenset(tuple(tokens[i : i + size]) for i in range(len(tokens) - size + 1))


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


class HeuristicExtractor:
    """Deterministic extraction from structured artifacts.

    What it will NOT do: turn ordinary commit history into memories. Commits are
    already in git, and the repository layer reads them live. Only commits that
    carry something git does not - a breaking change, or a fix that states its
    cause - become candidates.
    """

    def extract(self, artifacts: SessionArtifacts) -> list[Candidate]:
        scope = artifacts.scope
        key = artifacts.repository or "global"
        out: list[Candidate] = []

        def add(text: str, kind: str, evidence: str, importance: float = 0.5) -> None:
            out.append(
                Candidate(
                    text=text.strip(),
                    kind=kind,
                    scope=scope,
                    scope_key=key,
                    evidence=evidence,
                    topic=_topic_for(text),
                    importance=importance,
                )
            )

        for raw_commit in artifacts.commits:
            commit = _TRAILER.sub("", raw_commit).strip()
            breaking = _BREAKING_COMMIT.search(commit)
            if breaking:
                add(
                    f"Breaking change: {breaking.group('subject').strip()}",
                    "decision",
                    raw_commit,
                    importance=0.8,
                )
                continue
            fix = _FIX_COMMIT.search(commit)
            if fix:
                cause = _CAUSE.search(commit)
                if cause:
                    add(
                        f"{fix.group('subject').strip()} - cause: {cause.group('cause').strip()}",
                        "lesson",
                        raw_commit,
                        importance=0.7,
                    )

        for error in artifacts.tool_errors:
            add(error, "discovery", error, importance=0.7)

        for failure in artifacts.test_failures:
            add(failure, "failure", failure, importance=0.6)

        for decision in artifacts.decisions:
            add(decision, "decision", decision, importance=0.8)

        return out


def dedupe_candidates(candidates: Sequence[Candidate], threshold: float = DEDUPE_THRESHOLD) -> list[Candidate]:
    """Collapse candidates that say the same thing, keeping every piece of evidence.

    Evidence is merged rather than discarded: a reviewer judging a candidate
    needs to see everything that produced it, not just the first occurrence.
    """
    kept: list[Candidate] = []
    signatures: list[frozenset] = []
    for candidate in candidates:
        signature = _shingles(candidate.text)
        merged = False
        for index, existing in enumerate(signatures):
            if _jaccard(signature, existing) >= threshold:
                survivor = kept[index]
                if candidate.evidence not in survivor.evidence:
                    survivor.evidence = f"{survivor.evidence}\n---\n{candidate.evidence}"
                merged = True
                break
        if not merged:
            kept.append(candidate)
            signatures.append(signature)
    return kept


def filter_storable(
    candidates: Iterable[Candidate],
    repository_text: str,
    min_chars: int = MIN_TEXT_CHARS,
    restatement_threshold: float = RESTATEMENT_THRESHOLD,
) -> tuple[list[Candidate], ExtractionReport]:
    """Drop candidates not worth storing, and say why each was dropped."""
    candidates = list(candidates)
    report = ExtractionReport(considered=len(candidates))
    repo_signature = _shingles(repository_text) if repository_text.strip() else frozenset()

    kept: list[Candidate] = []
    for candidate in candidates:
        if len(candidate.text) < min_chars:
            report.rejected["too_short"] = report.rejected.get("too_short", 0) + 1
            continue
        if repo_signature and _jaccard(_shingles(candidate.text), repo_signature) >= restatement_threshold:
            report.rejected["restates_repository"] = report.rejected.get("restates_repository", 0) + 1
            continue
        kept.append(candidate)

    report.storable = len(kept)
    return kept, report
