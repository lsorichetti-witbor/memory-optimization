"""Repository layer: what the repo currently is.

Handoff section 14: README/docs/config/source stay repository sources, they are
not embedded into Mem0. Retrieved fresh every build, so they are never stale by
construction.

Every exclusion is recorded. A file skipped for size or encoding that vanished
silently would look identical to a file that was never relevant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.context.chunking import chunk_markdown, single_chunk
from src.context.sources.base import ContextSource
from src.context.types import ContextItem, Layer, Signals, Task

DEFAULT_MAX_FILE_BYTES = 200_000
DEFAULT_MAX_FILES = 400
_BINARY_PROBE_BYTES = 8192

MANIFESTS = (
    "pyproject.toml",
    "package.json",
    "docker-compose.yaml",
    "docker-compose.yml",
    "requirements.txt",
    "Makefile",
    # The canonical list of every configuration variable a service accepts, and
    # the file the server's own AGENTS.md points at for configuration. It is a
    # manifest in every sense except the extension.
    ".env.example",
)

# NOT collected, stated so the limit is visible rather than discovered:
# source files (except those named in `task.files`) and dotfile config such as
# .gitattributes. A question whose answer lives only in code is unanswerable
# from this layer, and the evaluation harness reports that as an unreachable
# ground-truth item rather than as a ranking failure.

# Walked anywhere in the tree, not just at the root. Measured on this repo: 30
# README.md files and 1 docker-compose.yaml, none of them at the root except the
# top README - a root-only list made most of the repository unreachable, and an
# unreachable file reads downstream as a ranking failure rather than as missing
# input.
NESTED_DOC_NAMES = ("README.md",)

EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".next",
        ".turbo",
        "dist",
        "build",
        "site-packages",
        ".mypy_cache",
        ".ruff_cache",
        "coverage",
        ".tox",
    }
)


def is_excluded(relative_parts: tuple[str, ...]) -> bool:
    return any(part in EXCLUDED_DIRS for part in relative_parts)


@dataclass
class RepositoryReport:
    considered: int = 0
    collected: int = 0
    capped: bool = False
    skipped: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        tail = " (file cap reached - the walk was truncated)" if self.capped else ""
        return (
            f"{self.collected} of {self.considered} repository files collected, "
            f"{len(self.skipped)} skipped{tail}"
        )


class RepositorySource(ContextSource):
    def __init__(
        self,
        root: Path,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_chars: int = 4000,
        max_files: int = DEFAULT_MAX_FILES,
    ) -> None:
        self._root = Path(root)
        self._max_file_bytes = max_file_bytes
        self._max_chars = max_chars
        self._max_files = max_files
        self.last_error: Optional[BaseException] = None
        self.last_report = RepositoryReport()

    @property
    def name(self) -> str:
        return "repository"

    @property
    def layer(self) -> Layer:
        return Layer.REPOSITORY

    def _walk(self, names: tuple[str, ...]) -> list[Path]:
        """Every file with one of these names, anywhere except a vendor directory."""
        found: list[Path] = []
        for name in names:
            for path in self._root.rglob(name):
                try:
                    relative = path.relative_to(self._root).parts
                except ValueError:
                    continue
                if is_excluded(relative) or not path.is_file():
                    continue
                found.append(path)
        return sorted(found, key=lambda p: (len(p.relative_to(self._root).parts), p.as_posix()))

    def _candidates(self, task: Task) -> list[Path]:
        found: list[Path] = []

        def add(path: Path) -> None:
            if path not in found:
                found.append(path)

        # Task files first: they are the only candidates the caller named
        # explicitly, so they must survive the file cap.
        for file in task.files:
            add(self._root / file)

        docs = self._root / "docs"
        if docs.is_dir():
            for path in sorted(docs.rglob("*.md")):
                if not is_excluded(path.relative_to(self._root).parts):
                    add(path)

        for path in self._walk(NESTED_DOC_NAMES):
            add(path)
        for path in self._walk(MANIFESTS):
            add(path)
        return found

    def _readable(self, path: Path, relative: str) -> Optional[str]:
        if not path.is_file():
            self.last_report.skipped[relative] = "not found"
            return None
        size = path.stat().st_size
        if size > self._max_file_bytes:
            self.last_report.skipped[relative] = f"too large ({size} bytes > {self._max_file_bytes})"
            return None
        with path.open("rb") as handle:
            probe = handle.read(_BINARY_PROBE_BYTES)
        if b"\x00" in probe:
            self.last_report.skipped[relative] = "binary"
            return None
        return path.read_text(encoding="utf-8", errors="replace")

    def collect(self, task: Task) -> list[ContextItem]:
        self.last_error = None
        self.last_report = RepositoryReport()
        items: list[ContextItem] = []

        try:
            candidates = self._candidates(task)
            if len(candidates) > self._max_files:
                # Say the walk was truncated. A silently capped candidate set
                # makes a missing file look like a ranking decision.
                self.last_report.capped = True
                candidates = candidates[: self._max_files]
            for path in candidates:
                try:
                    relative = path.relative_to(self._root).as_posix()
                except ValueError:
                    relative = path.as_posix()
                self.last_report.considered += 1

                text = self._readable(path, relative)
                if text is None:
                    continue

                if path.suffix.lower() in {".md", ".markdown"}:
                    chunks = chunk_markdown(text, source=relative, max_chars=self._max_chars)
                else:
                    # Only markdown gets heading-split. A `#` in Python is a comment,
                    # and splitting on it would shred the file into meaningless pieces.
                    chunks = single_chunk(text, source=relative)

                if not chunks:
                    self.last_report.skipped[relative] = "empty"
                    continue

                self.last_report.collected += 1
                for chunk in chunks:
                    items.append(
                        ContextItem(
                            id=chunk.id,
                            layer=Layer.REPOSITORY,
                            content=chunk.content,
                            source=relative,
                            title=chunk.title,
                            signals=Signals(confidence=1.0, recency=1.0, staleness=0.0),
                        )
                    )
        except OSError as error:
            self.last_error = error
        return items
