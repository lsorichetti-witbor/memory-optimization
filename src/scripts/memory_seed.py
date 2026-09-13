"""Seed the memories that are meant to be reproducible from version control.

    python -m src.scripts.memory_seed --global                 # engineering lessons + rule incidents
    python -m src.scripts.memory_seed --repo memory-optimization
    python -m src.scripts.memory_seed --global --repo memory-optimization
    python -m src.scripts.memory_seed --list

Two sets, both idempotent by topic:

  global  src/evaluation/global_seed.py  - rule incidents extracted from CLAUDE.md
                                           by the section 19 refactor, plus what
                                           has been learned about retrieval
  repo    src/evaluation/seed.py         - the fixtures the repo-local benchmark
                                           dataset scores against

The rule incidents matter most. After section 19 the rules stayed in CLAUDE.md
and the evidence moved to Mem0, so these memories are the only record of why
several rules exist. Keeping them in code rather than only in a database is what
makes a wipe recoverable.
"""

from __future__ import annotations

import argparse
import sys

from src.evaluation.global_seed import GLOBAL_MEMORIES, seed_global
from src.evaluation.seed import SEED_MEMORIES, seed as seed_repo
from src.memory.scopes import Scope
from src.scripts._common import add_common_arguments, configure_stdout, emit, provider_from_env


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--global",
        action="store_true",
        dest="global_scope",  # `global` is a keyword, so argparse cannot name the attribute after it
        help="seed the global scope",
    )
    parser.add_argument("--repo", default=None, metavar="KEY", help="seed this repository scope key")
    parser.add_argument("--list", action="store_true", dest="list_only", help="show what would be written")
    add_common_arguments(parser)
    args = parser.parse_args(argv)

    if args.list_only:
        lines = [f"global scope ({len(GLOBAL_MEMORIES)} memories):"]
        lines += [f"  {m.kind:<9} {m.topic}" for m in GLOBAL_MEMORIES]
        lines.append(f"repository scope ({len(SEED_MEMORIES)} memories):")
        lines += [f"  {m.kind:<9} {m.topic}" for m in SEED_MEMORIES]
        emit(
            {
                "global": [{"topic": m.topic, "kind": m.kind} for m in GLOBAL_MEMORIES],
                "repository": [{"topic": m.topic, "kind": m.kind} for m in SEED_MEMORIES],
            },
            args.json,
            "\n".join(lines),
        )
        return 0

    if not args.global_scope and not args.repo:
        print("error: nothing to do. Pass --global, --repo <key>, or --list.", file=sys.stderr)
        return 2

    provider = provider_from_env()
    written: dict[str, dict[str, str]] = {}
    try:
        if args.global_scope:
            written["global"] = seed_global(provider)
        if args.repo:
            written["repository"] = seed_repo(provider, scope_key=args.repo, scope=Scope.REPOSITORY)
    except (RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        provider.close()

    lines = []
    for group, topics in written.items():
        lines.append(f"{group}: seeded {len(topics)} memories")
        lines += [f"  {mid[:8]}  {topic}" for topic, mid in topics.items()]
    emit(written, args.json, "\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
