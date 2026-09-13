"""Store one memory.

    python -m src.scripts.memory_store --scope repository --key memory-optimization \
        --kind discovery --topic server.default_provider \
        --text "The self-hosted stack ships pgvector only; there is no Neo4j service."

Memories enter at lifecycle=candidate. Promotion to durable is a separate,
deliberate step (see memory_promote.py) - handoff section 7 is explicit that not
every observation should become permanent memory.
"""

from __future__ import annotations

import argparse
import sys

from src.memory.scopes import Scope
from src.scripts._common import add_common_arguments, add_scope_arguments, configure_stdout, emit, provider_from_env

KINDS = ("decision", "discovery", "lesson", "convention", "failure", "incident", "note")


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_scope_arguments(parser)
    parser.add_argument("--text", required=True, help="the memory itself, stored verbatim")
    parser.add_argument("--kind", default="note", choices=KINDS)
    parser.add_argument("--topic", default=None, help="stable key used for conflict detection")
    parser.add_argument("--tag", action="append", default=[], dest="tags")
    parser.add_argument("--confidence", type=float, default=0.5)
    parser.add_argument("--importance", type=float, default=0.5)
    parser.add_argument("--source", default=None, help="where this came from, e.g. a session id")
    parser.add_argument(
        "--infer",
        action="store_true",
        help="let the server's LLM re-extract facts from the text instead of storing it verbatim",
    )
    add_common_arguments(parser)
    args = parser.parse_args(argv)

    provider = provider_from_env()
    try:
        records = provider.add(
            args.text,
            scope=Scope.parse(args.scope),
            scope_key=args.key,
            kind=args.kind,
            topic=args.topic,
            tags=tuple(args.tags),
            confidence=args.confidence,
            importance=args.importance,
            source=args.source,
            infer=args.infer,
        )
    except (RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        provider.close()

    emit(
        [{"id": r.id, "text": r.text} for r in records],
        args.json,
        "\n".join(f"stored {r.id}: {r.text}" for r in records),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
