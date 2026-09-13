"""Move a memory through its lifecycle.

    python -m src.scripts.memory_promote --id <memory id> --to durable
    python -m src.scripts.memory_promote --id <memory id> --to superseded --replacement <id>

Illegal transitions are refused before anything is written (see
src/memory/lifecycle.py for the table).
"""

from __future__ import annotations

import argparse
import sys

from src.memory.lifecycle import Lifecycle
from src.scripts._common import add_common_arguments, configure_stdout, emit, provider_from_env


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--id", required=True, dest="memory_id")
    parser.add_argument("--to", required=True, choices=["durable", "stale", "superseded"])
    parser.add_argument("--replacement", default=None, help="required when --to superseded")
    add_common_arguments(parser)
    args = parser.parse_args(argv)

    provider = provider_from_env()
    try:
        before = provider.get(args.memory_id)
        target = Lifecycle(args.to)
        if target is Lifecycle.DURABLE:
            provider.promote(args.memory_id)
        elif target is Lifecycle.STALE:
            provider.mark_stale(args.memory_id)
        else:
            provider.supersede(args.memory_id, replacement_id=args.replacement or "")
    except (RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        provider.close()

    emit(
        {"id": args.memory_id, "from": before.envelope.lifecycle, "to": args.to},
        args.json,
        f"{args.memory_id}: {before.envelope.lifecycle} -> {args.to}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
