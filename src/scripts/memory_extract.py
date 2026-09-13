"""Propose candidate memories from a session, and store them only on request.

    # review what would be stored - no writes
    python -m src.scripts.memory_extract --repository memory-optimization --since HEAD~5

    # store the survivors, all at lifecycle=candidate
    python -m src.scripts.memory_extract --repository memory-optimization --since HEAD~5 --store

Without `--store` nothing is written. That is the review gate from handoff
section 7: extraction proposes, a human or a reviewing agent disposes. Even with
`--store`, everything lands at lifecycle `candidate` and needs a separate
`memory_promote --to durable` before it is treated as settled knowledge.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from src.memory.extraction import (
    HeuristicExtractor,
    SessionArtifacts,
    dedupe_candidates,
    filter_storable,
)
from src.memory.durable import WriteQueued
from src.memory.errors import InvalidWrite, Mem0Error
from src.memory.scopes import Scope
from src.scripts._common import add_common_arguments, configure_stdout, emit, provider_from_env, repo_root

# Vars that silently redirect git away from the directory we asked for.
_GIT_REDIRECT = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR")


def _git(root: Path, *args: str) -> str:
    import os

    env = {k: v for k, v in os.environ.items() if k not in _GIT_REDIRECT}
    result = subprocess.run(["git", *args], cwd=root, env=env, capture_output=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed [{result.returncode}]: "
            f"{result.stderr.decode('utf-8', 'replace').strip()[:200]}"
        )
    return result.stdout.decode("utf-8", "replace")


def _commits(root: Path, since: str) -> tuple[str, ...]:
    # %B is the raw body, and the NUL separator keeps multi-paragraph messages
    # intact - splitting on newlines would shred every commit with a body.
    raw = _git(root, "log", f"{since}..HEAD", "--format=%B%x00")
    return tuple(c.strip() for c in raw.split("\0") if c.strip())


def _repository_text(root: Path, limit: int = 200_000) -> str:
    """Enough repository prose to detect a candidate that merely restates it."""
    parts: list[str] = []
    total = 0
    for name in ("README.md", "AGENTS.md"):
        path = root / name
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            parts.append(text)
            total += len(text)
    docs = root / "docs"
    if docs.is_dir():
        for path in sorted(docs.rglob("*.md")):
            if total >= limit:
                break
            text = path.read_text(encoding="utf-8", errors="replace")
            parts.append(text)
            total += len(text)
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repository", required=True, help="repository scope key")
    parser.add_argument("--since", default="HEAD~10", help="git revision to collect commits after")
    parser.add_argument("--decision", action="append", default=[], dest="decisions",
                        help="a decision to record, verbatim; repeatable")
    parser.add_argument("--tool-error", action="append", default=[], dest="tool_errors",
                        help="a tool or environment error observed; repeatable")
    parser.add_argument("--test-failure", action="append", default=[], dest="test_failures",
                        help="a test failure worth remembering; repeatable")
    parser.add_argument("--root", default=None)
    parser.add_argument("--store", action="store_true",
                        help="actually write the survivors as lifecycle=candidate memories")
    add_common_arguments(parser)
    args = parser.parse_args(argv)

    root = Path(args.root or repo_root())

    try:
        commits = _commits(root, args.since)
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"warning: could not read git history: {error}", file=sys.stderr)
        commits = ()

    artifacts = SessionArtifacts(
        commits=commits,
        decisions=tuple(args.decisions),
        tool_errors=tuple(args.tool_errors),
        test_failures=tuple(args.test_failures),
        repository=args.repository,
        scope=Scope.REPOSITORY,
    )

    raw = HeuristicExtractor().extract(artifacts)
    deduped = dedupe_candidates(raw)
    kept, report = filter_storable(deduped, repository_text=_repository_text(root))

    payload = [
        {
            "text": c.text,
            "kind": c.kind,
            "topic": c.topic,
            "scope": c.scope.value,
            "scope_key": c.scope_key,
            "lifecycle": c.lifecycle,
            "evidence": c.evidence[:400],
        }
        for c in kept
    ]

    stored: list[str] = []
    queued: list[str] = []
    rejected: list[str] = []
    if args.store and kept:
        provider = provider_from_env()
        try:
            # Per candidate, not per batch. One bad or queued write used to
            # abandon every remaining candidate in the batch - the largest
            # single loss in the system, because these come from a transcript
            # that may already be gone.
            for candidate in kept:
                try:
                    records = provider.add(
                        candidate.text,
                        scope=candidate.scope,
                        scope_key=candidate.scope_key,
                        kind=candidate.kind,
                        topic=candidate.topic,
                        tags=candidate.tags,
                        confidence=candidate.confidence,
                        importance=candidate.importance,
                        source=f"extraction:{args.since}..HEAD",
                    )
                    stored.extend(r.id for r in records)
                except WriteQueued as q:
                    queued.append(str(q.path))
                except (InvalidWrite, Mem0Error) as error:
                    rejected.append(f"{candidate.topic or candidate.text[:40]}: {error}")
        finally:
            provider.close()

        if queued:
            print("", file=sys.stderr)
            print(
                f"WARNING: {len(queued)} of {len(kept)} extracted memories were NOT stored. "
                f"They are queued and will not be lost.",
                file=sys.stderr,
            )
            print("  replay with: python -m src.scripts.memory_flush", file=sys.stderr)
        for line in rejected:
            print(f"rejected: {line}", file=sys.stderr)

    # Non-zero whenever anything did not reach the store, so a script that only
    # checks the exit code cannot read a partly-queued run as a complete one.
    exit_code = 1 if rejected else (3 if queued else 0)

    if args.json:
        emit(
            {
                "candidates": payload,
                "stored": stored,
                "queued": queued,
                "rejected": rejected,
                "summary": report.summary(),
            },
            True,
            "",
        )
        return exit_code

    lines = [
        f"{len(raw)} raw -> {len(deduped)} after dedupe -> {report.summary()}",
        "",
    ]
    for c in kept:
        lines.append(f"[{c.kind}] topic={c.topic or '-'}")
        lines.append(f"  {c.text}")
    if not kept:
        lines.append("nothing worth storing from these artifacts")
    lines.append("")
    if args.store:
        # With their denominator: "stored 4" beside 6 candidates hides the two
        # that did not make it.
        lines.append(f"stored {len(stored)} of {len(kept)} memories at lifecycle=candidate")
        if queued:
            lines.append(f"queued {len(queued)} of {len(kept)} for replay (not lost)")
        if rejected:
            lines.append(f"rejected {len(rejected)} of {len(kept)} as malformed (not queued)")
        lines.append("promote with: python -m src.scripts.memory_promote --id <id> --to durable")
    else:
        lines.append("nothing written. re-run with --store to write these as candidates.")
    print("\n".join(lines))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
