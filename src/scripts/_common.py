"""Shared CLI plumbing.

Connection settings come from the environment so a key never lands in shell
history or a process listing:

    MEM0_API_URL     http://localhost:8888   (port from MEM0_API_PORT in server/.env)
    MEM0_API_KEY     the ADMIN_API_KEY from server/.env
    MEM0_USER        the human this memory belongs to
    MEM0_REPOSITORY  optional, the repository scope key
    MEM0_PROJECT     optional, the project scope key
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from src.memory.mem0_provider import Mem0Provider
from src.memory.scopes import Scope, ScopeSelector


def configure_stdout() -> None:
    """Force UTF-8 on stdout/stderr.

    The Windows console defaults to cp1252, and repository files routinely carry
    characters it cannot encode. Without this, printing a perfectly good context
    dies with UnicodeEncodeError - a command that only works on some machines.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")


def add_scope_arguments(parser: argparse.ArgumentParser, required: bool = True) -> None:
    parser.add_argument(
        "--scope",
        required=required,
        choices=[s.value for s in Scope.ordered()],
        help="scope level to write to or read from",
    )
    parser.add_argument("--key", required=required, help="scope key, e.g. the repository name")


def provider_from_env() -> Mem0Provider:
    try:
        return Mem0Provider.from_env()
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        print(
            "set MEM0_API_URL, MEM0_API_KEY and MEM0_USER "
            "(the key is ADMIN_API_KEY from server/.env)",
            file=sys.stderr,
        )
        raise SystemExit(2)


def selector_from_env(user: str, repository: str | None, project: str | None, task: str | None = None) -> ScopeSelector:
    return ScopeSelector(user=user, repository=repository, project=project, task=task)


def emit(payload: Any, as_json: bool, human: str) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(human)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]
