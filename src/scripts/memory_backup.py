"""Export every memory to a file, and restore it.

    python -m src.scripts.memory_backup export --out backup.json
    python -m src.scripts.memory_backup restore --in backup.json
    python -m src.scripts.memory_backup restore --in backup.json --dry-run

Export and restore are one command on purpose. An export nobody has restored is
not a backup, it is a file - and the only way to know the pair works is to run
both. `restore --dry-run` reports exactly what it would write without writing.

What survives a round trip: the text, the scope and scope key, the user, the
kind, topic, tags, confidence, importance, source, and the ORIGINAL created_at.
That last one matters: restoring with the restore-time as created_at would make
every memory look new, and recency is a ranking signal.

What does not survive: memory ids. Mem0 assigns them on write, so a restored
store has the same memories under different ids. Anything that referenced a
memory by id - a `superseded_by` pointer - is rewritten where it can be mapped
and reported where it cannot.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

import httpx

from src.memory.envelope import decode_envelope
from src.memory.mem0_provider import DEFAULT_TIMEOUT, Mem0Provider
from src.memory.scopes import Scope
from src.scripts._common import add_common_arguments, configure_stdout, emit


def _provider_for(user: str) -> Mem0Provider:
    """Raw, not durable - deliberately.

    Restore is the one writer whose input survives its own failure: the backup
    file is still on disk, so a row that does not land is re-run, not lost.
    Queueing it would copy hundreds of rows into the spool and bury the writes
    that genuinely have nowhere else to live.
    """
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


def _fetch_all() -> list[dict]:
    """Every memory the caller can see, via the admin listing."""
    base_url = os.environ.get("MEM0_API_URL")
    if not base_url:
        raise RuntimeError("MEM0_API_URL is not set")
    headers = {"X-API-Key": os.environ.get("MEM0_API_KEY", "")}
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=120) as client:
        response = client.get("/memories", params={"top_k": 1000}, headers=headers)
        if response.status_code >= 400:
            raise RuntimeError(f"listing failed [{response.status_code}]: {response.text[:200]}")
        rows = response.json().get("results") or []
    if len(rows) >= 1000:
        # The listing is capped. Exporting a truncated store and calling it a
        # backup is how a restore quietly loses the tail.
        raise RuntimeError(
            f"the listing returned {len(rows)} rows, which is the server cap - "
            "raise ALL_MEMORIES_LIMIT before trusting this as a full backup."
        )
    return rows


def do_export(path: Path) -> dict:
    rows = _fetch_all()
    entries = []
    for row in rows:
        envelope = decode_envelope(row.get("metadata"))
        entries.append(
            {
                "id": row.get("id"),
                "text": row.get("memory") or "",
                "user": row.get("user_id"),
                "scope": envelope.scope.value,
                "scope_key": envelope.scope_key,
                "kind": envelope.kind,
                "topic": envelope.topic,
                "tags": list(envelope.tags),
                "confidence": envelope.confidence,
                "importance": envelope.importance,
                "lifecycle": envelope.lifecycle,
                "superseded_by": envelope.superseded_by,
                "source": envelope.source,
                "created_at": row.get("created_at") or (
                    envelope.created_at.isoformat() if envelope.created_at else None
                ),
            }
        )
    payload = {"version": 1, "count": len(entries), "memories": entries}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def do_restore(path: Path, dry_run: bool) -> dict:
    from datetime import datetime

    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("memories") or []

    by_user = Counter(e.get("user") or "unknown" for e in entries)
    by_scope = Counter(f"{e.get('scope')}:{e.get('scope_key')}" for e in entries)
    report = {
        "file": str(path),
        "count": len(entries),
        "users": dict(by_user),
        "scopes": dict(by_scope),
        "written": 0,
        "failed": [],
        "id_map": {},
    }
    if dry_run:
        return report

    providers: dict[str, Mem0Provider] = {}
    try:
        for entry in entries:
            user = entry.get("user") or "unknown"
            provider = providers.get(user)
            if provider is None:
                provider = _provider_for(user)
                providers[user] = provider
            created = entry.get("created_at")
            try:
                records = provider.add(
                    entry["text"],
                    scope=Scope.parse(entry["scope"]),
                    scope_key=entry["scope_key"],
                    kind=entry.get("kind", "note"),
                    topic=entry.get("topic"),
                    tags=tuple(entry.get("tags") or ()),
                    confidence=float(entry.get("confidence", 0.5)),
                    importance=float(entry.get("importance", 0.5)),
                    source=entry.get("source"),
                    created_at=datetime.fromisoformat(created) if created else None,
                )
                report["written"] += 1
                if entry.get("id"):
                    report["id_map"][entry["id"]] = records[0].id
            except Exception as error:  # noqa: BLE001 - one bad row must not end the restore
                report["failed"].append(f"{entry.get('topic') or entry.get('text','')[:40]}: {error}")
    finally:
        for provider in providers.values():
            provider.close()

    # superseded_by pointed at an id that no longer exists after a restore.
    dangling = [
        e.get("topic") or e.get("text", "")[:40]
        for e in entries
        if e.get("superseded_by") and e["superseded_by"] not in report["id_map"]
    ]
    report["dangling_superseded_by"] = dangling
    return report


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    exporter = sub.add_parser("export", help="write every memory to a file")
    exporter.add_argument("--out", required=True)
    add_common_arguments(exporter)

    restorer = sub.add_parser("restore", help="write a backup file back into the store")
    restorer.add_argument("--in", required=True, dest="infile")
    restorer.add_argument("--dry-run", action="store_true", help="report what would be written, write nothing")
    add_common_arguments(restorer)

    args = parser.parse_args(argv)

    try:
        if args.command == "export":
            payload = do_export(Path(args.out))
            emit(
                {"out": args.out, "count": payload["count"]},
                args.json,
                f"exported {payload['count']} memories -> {args.out}",
            )
            return 0

        report = do_restore(Path(args.infile), args.dry_run)
    except (RuntimeError, ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    lines = [
        f"{'would restore' if args.dry_run else 'restored'} {report['count']} memories from {report['file']}",
        f"  users : {', '.join(f'{u}={n}' for u, n in sorted(report['users'].items()))}",
        f"  scopes: {', '.join(f'{s}={n}' for s, n in sorted(report['scopes'].items()))}",
    ]
    if not args.dry_run:
        lines.append(f"  written: {report['written']} of {report['count']}")
        for failure in report["failed"]:
            lines.append(f"  FAILED {failure}")
        if report.get("dangling_superseded_by"):
            lines.append(
                "  note: superseded_by pointers could not be remapped for: "
                + ", ".join(report["dangling_superseded_by"])
            )
    emit(report, args.json, "\n".join(lines))
    return 1 if report.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
