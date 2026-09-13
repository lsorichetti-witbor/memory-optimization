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
import os
import sys

from datetime import datetime, timezone

from src.memory.scopes import Scope
from src.memory.mem0_provider import Mem0Provider
from src.memory.spool import Spool, SpooledWrite, is_unreachable
from src.scripts._common import add_common_arguments, add_scope_arguments, configure_stdout, emit

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

    scope = Scope.parse(args.scope)
    now = datetime.now(timezone.utc)

    def spool_it(reason: str) -> int:
        """Queue the write rather than losing it, and say so loudly.

        A memory is worth writing at the moment you notice it. If the stack
        happens to be down and the command merely errors, the observation is
        gone - so an unreachable server queues instead of failing.
        """
        spool = Spool()
        path = spool.enqueue(
            SpooledWrite(
                text=args.text,
                scope=scope,
                scope_key=args.key,
                user=os.environ.get("MEM0_USER", "unknown"),
                kind=args.kind,
                topic=args.topic,
                tags=tuple(args.tags),
                confidence=args.confidence,
                importance=args.importance,
                source=args.source,
                created_at=now,
                repository=os.environ.get("MEM0_REPOSITORY"),
            )
        )
        print("", file=sys.stderr)
        print("WARNING: the Mem0 server is unreachable. The memory was NOT stored.", file=sys.stderr)
        print(f"  reason:  {reason}", file=sys.stderr)
        print(f"  queued:  {path}", file=sys.stderr)
        print(f"  pending: {spool.pending()} write(s) waiting in {spool.root}", file=sys.stderr)
        print("  Start the stack, then replay with:", file=sys.stderr)
        print("    python -m src.scripts.memory_flush", file=sys.stderr)
        print("", file=sys.stderr)
        return 3

    try:
        provider = Mem0Provider.from_env()
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    try:
        records = provider.add(
            args.text,
            scope=scope,
            scope_key=args.key,
            kind=args.kind,
            topic=args.topic,
            tags=tuple(args.tags),
            confidence=args.confidence,
            importance=args.importance,
            source=args.source,
            infer=args.infer,
            created_at=now,
        )
    except Exception as error:  # noqa: BLE001 - the transport error type decides what happens
        if is_unreachable(error):
            return spool_it(str(error))
        # A 400 means the write itself is wrong. Queueing it would retry a
        # failure forever, so it fails here and now.
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
