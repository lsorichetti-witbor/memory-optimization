"""Current task state: branch, working tree, diff, recent commits.

Two non-obvious rules apply to every git call here.

1. Inherited `GIT_DIR` / `GIT_WORK_TREE` override the `cwd` argument. Hooks,
   wrappers and CI runners inject them, so they are scrubbed rather than hoped
   about.
2. Status is read with `--porcelain=v1 -z`. The line-oriented form quotes paths
   containing spaces or non-ASCII into C escapes, and the corrupted path then
   fails every downstream comparison silently.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

from src.context.sources.base import ContextSource
from src.context.types import ContextItem, Layer, Signals, Task

# Anything that can redirect a git invocation away from the directory we passed.
_GIT_REDIRECT_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CEILING_DIRECTORIES",
)

DEFAULT_TIMEOUT = 15


def scrubbed_env(env: Optional[dict[str, str]] = None) -> dict[str, str]:
    source = dict(os.environ if env is None else env)
    for name in _GIT_REDIRECT_VARS:
        source.pop(name, None)
    return source


class CurrentStateSource(ContextSource):
    def __init__(self, root: Path, max_diff_chars: int = 4000, log_count: int = 5) -> None:
        self._root = Path(root)
        self._max_diff_chars = max_diff_chars
        self._log_count = log_count
        self.last_error: Optional[BaseException] = None

    @property
    def name(self) -> str:
        return "current_state"

    @property
    def layer(self) -> Layer:
        return Layer.CURRENT_STATE

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self._root,
            env=scrubbed_env(),
            capture_output=True,
            timeout=DEFAULT_TIMEOUT,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed [{result.returncode}]: "
                f"{result.stderr.decode('utf-8', 'replace').strip()[:200]}"
            )
        return result.stdout.decode("utf-8", "replace")

    def _working_tree(self) -> str:
        # -z gives NUL-delimited records with raw, unquoted paths.
        raw = self._git("status", "--porcelain=v1", "-z")
        records = [record for record in raw.split("\0") if record]
        paths: list[str] = []
        skip_next = False
        for record in records:
            if skip_next:
                # A rename record is followed by its source path as a separate field.
                skip_next = False
                continue
            status, _, path = record.partition(" ")
            # `XY path` - the status field is two columns wide, so partition on the
            # first space leaves the path intact even when it contains spaces.
            if len(record) > 3:
                status, path = record[:2], record[3:]
            if status.startswith("R"):
                skip_next = True
            paths.append(f"{status.strip() or '??'} {path}")
        if not paths:
            # An empty string here would be indistinguishable from a failed call.
            return "clean - no uncommitted changes"
        return "\n".join(paths)

    def collect(self, task: Task) -> list[ContextItem]:
        self.last_error = None
        items: list[ContextItem] = []
        signals = Signals(confidence=1.0, recency=1.0, importance=0.8)

        def add(title: str, content: str) -> None:
            if content.strip():
                items.append(
                    ContextItem(
                        id=f"state:{title.replace(' ', '_')}",
                        layer=Layer.CURRENT_STATE,
                        content=content.strip(),
                        source="git",
                        title=title,
                        signals=signals,
                    )
                )

        try:
            add("branch", self._git("rev-parse", "--abbrev-ref", "HEAD").strip())
            add("working tree", self._working_tree())
            diff = self._git("diff", "--stat", "HEAD")
            add("diff stat", diff[: self._max_diff_chars] if diff.strip() else "no staged or unstaged diff")
            add("recent commits", self._git("log", f"-{self._log_count}", "--format=%h %s"))
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            self.last_error = error
            return []
        return items
