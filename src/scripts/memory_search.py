"""Search memories in one scope.

    python -m src.scripts.memory_search --scope repository --key memory-optimization \
        --query "which embedding provider"

Prints the denominator: `N of top_k` makes it obvious when the result set was
capped rather than exhaustive.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from src.memory.scopes import Scope
from src.memory.types import SearchQuery
from src.scripts._common import add_common_arguments, add_scope_arguments, configure_stdout, emit, provider_from_env


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_scope_arguments(parser)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--query")
    source.add_argument(
        "--query-file",
        dest="query_file",
        help="read the query from this UTF-8 file. A shell re-splits a quoted "
        "argument, and a truncated query returns confident results for a "
        "question nobody asked.",
    )
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--threshold", type=float, default=None)
    add_common_arguments(parser)
    args = parser.parse_args(argv)

    if args.query_file:
        try:
            query = Path(args.query_file).read_text(encoding="utf-8").strip()
        except OSError as error:
            print(f"error: cannot read --query-file: {error}", file=sys.stderr)
            return 2
    else:
        query = args.query

    provider = provider_from_env()
    try:
        results = provider.search(
            SearchQuery(
                query=query,
                scope=Scope.parse(args.scope),
                scope_key=args.key,
                top_k=args.top_k,
                threshold=args.threshold,
            )
        )
    except (RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        provider.close()

    payload = [
        {
            "id": r.id,
            "score": r.score,
            "lifecycle": r.envelope.lifecycle,
            "topic": r.envelope.topic,
            "text": r.text,
        }
        for r in results
    ]
    human_lines = [f"{len(results)} of top_k={args.top_k} returned"]
    for r in results:
        score = f"{r.score:.3f}" if r.score is not None else "n/a"
        human_lines.append(f"  [{score}] ({r.envelope.lifecycle}) {r.id}: {r.text}")
    emit(payload, args.json, "\n".join(human_lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
