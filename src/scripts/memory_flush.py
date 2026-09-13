"""Replay memories that were queued while the server was down.

    python -m src.scripts.memory_flush            # replay everything queued
    python -m src.scripts.memory_flush --list     # show what is queued, write nothing
    python -m src.scripts.memory_flush --json

The spool is shared across every repository, every scope and every user on the
machine, so this replays all of them in one pass, oldest first. Each entry is
replayed as its own author - `user_id` is part of the search filter, so writing
someone else's memory under your identity would hide it from them.

Nothing is deleted until the server confirms it stored. A failure leaves the
file queued and is counted; the exit code is non-zero so a caller notices.
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx

from src.memory.mem0_provider import DEFAULT_TIMEOUT, Mem0Provider
from src.memory.spool import Spool
from src.scripts._common import add_common_arguments, configure_stdout, emit


def _provider_for(user: str) -> Mem0Provider:
    """A provider bound to the entry's own author, not to the current shell."""
    base_url = os.environ.get("MEM0_API_URL")
    if not base_url:
        raise RuntimeError("MEM0_API_URL is not set")
    return Mem0Provider(
        client=httpx.Client(base_url=base_url.rstrip("/"), timeout=DEFAULT_TIMEOUT),
        api_key=os.environ.get("MEM0_API_KEY", ""),
        user=user,
        repository=os.environ.get("MEM0_REPOSITORY"),
        project=os.environ.get("MEM0_PROJECT"),
    )


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", dest="list_only", help="show the queue, replay nothing")
    parser.add_argument("--spool", default=None, help="spool directory (default: the shared one)")
    add_common_arguments(parser)
    args = parser.parse_args(argv)

    spool = Spool(root=args.spool) if args.spool else Spool()

    if args.list_only:
        entries = spool.entries()
        payload = [
            {
                "queued_at": e.created_at.isoformat() if e.created_at else None,
                "user": e.user,
                "scope": e.scope.value,
                "scope_key": e.scope_key,
                "kind": e.kind,
                "topic": e.topic,
                "text": e.text,
            }
            for e in entries
        ]
        lines = [f"{spool.pending()} write(s) queued in {spool.root}"]
        for e in entries:
            stamp = e.created_at.strftime("%Y-%m-%d %H:%M:%SZ") if e.created_at else "unknown"
            lines.append(f"  [{stamp}] {e.user} -> {e.scope.value}:{e.scope_key} ({e.kind}) {e.text[:80]}")
        emit(payload, args.json, "\n".join(lines))
        return 0

    if spool.pending() == 0:
        emit({"replayed": 0, "failed": 0, "total": 0}, args.json, f"nothing queued in {spool.root}")
        return 0

    report = spool.flush(_provider_for)

    payload = {
        "total": report.total,
        "replayed": report.replayed,
        "failed": report.failed,
        "errors": report.errors,
        "spool": str(spool.root),
    }
    lines = [report.summary()]
    for error in report.errors:
        lines.append(f"  {error}")
    if report.failed:
        lines.append("")
        lines.append(f"{report.failed} write(s) are still queued in {spool.root} and were NOT lost.")
        lines.append("Re-run this command once the cause is fixed.")
    emit(payload, args.json, "\n".join(lines))

    # Non-zero when anything is still queued: a caller that only checks the exit
    # code must not read a partial replay as a complete one.
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
