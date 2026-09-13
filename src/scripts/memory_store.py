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
from pathlib import Path

from datetime import datetime, timezone

from src.memory.durable import WriteQueued
from src.memory.errors import InvalidWrite, Mem0Error
from src.memory.scopes import Scope
from src.scripts._common import (
    add_common_arguments,
    add_scope_arguments,
    configure_stdout,
    emit,
    provider_from_env,
)

KINDS = ("decision", "discovery", "lesson", "convention", "failure", "incident", "note")


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_scope_arguments(parser)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", help="the memory itself, stored verbatim")
    source.add_argument(
        "--text-file",
        dest="text_file",
        help="read the memory from this UTF-8 file instead of the command line. "
        "Use it for anything containing quotes: a shell re-splits a quoted "
        "argument and the memory arrives truncated or rejected.",
    )
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

    if args.text_file:
        try:
            text = Path(args.text_file).read_text(encoding="utf-8")
        except OSError as error:
            print(f"error: cannot read --text-file: {error}", file=sys.stderr)
            return 2
    else:
        text = args.text
    if not text.strip():
        print("error: the memory is empty", file=sys.stderr)
        return 2

    # The queueing lives in DurableProvider now, so every writer gets it rather
    # than only this one. All that is left here is reporting it.
    provider = provider_from_env()

    try:
        records = provider.add(
            text,
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
    except WriteQueued as queued:
        print("", file=sys.stderr)
        print("WARNING: the memory was NOT stored. It is queued and will not be lost.", file=sys.stderr)
        print(f"  reason:  {queued.cause}", file=sys.stderr)
        print(f"  queued:  {queued.path}", file=sys.stderr)
        print(f"  pending: {queued.pending} write(s) waiting in {provider.spool.root}", file=sys.stderr)
        # What to do depends on why it failed. "Start the stack" was printed for
        # every failure and was wrong for a spent quota: the server was up.
        print(f"  next:    {queued.advice()}", file=sys.stderr)
        print("    python -m src.scripts.memory_flush", file=sys.stderr)
        print("", file=sys.stderr)
        return 3
    except (InvalidWrite, Mem0Error) as error:
        # The server judged the write itself malformed, so it was not queued -
        # retrying it would fail identically forever.
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        provider.close()

    # A write the server queued is accepted and safe, but it is NOT searchable
    # yet. Printing "stored" for it would be the one lie this whole layer exists
    # to avoid - the caller would move on believing they can find it.
    queued_remotely = [r for r in records if r.envelope.extra.get("queued_server_side")]
    if queued_remotely:
        reason = queued_remotely[0].envelope.extra.get("queued_reason") or "the embedder was unavailable"
        lines = [
            f"accepted {r.id}: {r.text}" for r in queued_remotely
        ] + [
            "",
            "NOT searchable yet: the server queued it for embedding.",
            f"  reason: {reason}",
            "  watch:  ctx health   |   <dashboard>/dashboard/queue",
        ]
        payload: dict | list = {
            "accepted": [{"pending_id": r.id, "text": r.text} for r in queued_remotely],
            "searchable": False,
            "reason": reason,
        }
    else:
        lines = [f"stored {r.id}: {r.text}" for r in records]
        payload = [{"id": r.id, "text": r.text} for r in records]

    # A successful write drains whatever was queued. Say so: a command that
    # quietly did ten times the work it was asked to do is a mystery pause, and
    # an unreported partial drain looks exactly like a full one.
    drain = provider.last_drain
    if drain is not None and drain.total:
        lines.append(f"backlog: {drain.summary()}")
        if drain.outstanding:
            lines.append(f"  {drain.outstanding} still queued - python -m src.scripts.memory_flush")
        payload = {"result": payload, "backlog": {
            "total": drain.total,
            "replayed": drain.replayed,
            "already_stored": drain.already_stored,
            "outstanding": drain.outstanding,
        }}

    emit(payload, args.json, "\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
