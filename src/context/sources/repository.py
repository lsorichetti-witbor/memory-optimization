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
_BINARY_PROBE_BYTES = 8192

MANIFESTS = (
    "pyproject.toml",
    "package.json",
    "docker-compose.yaml",
    "docker-compose.yml",
    "requirements.txt",
    "Makefile",
)


@dataclass
class RepositoryReport:
    considered: int = 0
    collected: int = 0
    skipped: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        return f"{self.collected} of {self.considered} repository files collected, {len(self.skipped)} skipped"


class RepositorySource(ContextSource):
    def __init__(
        self,
        root: Path,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_chars: int = 4000,
    ) -> None:
        self._root = Path(root)
        self._max_file_bytes = max_file_bytes
        self._max_chars = max_chars
        self.last_error: Optional[BaseException] = None
        self.last_report = RepositoryReport()

    @property
    def name(self) -> str:
        return "repository"

    @property
    def layer(self) -> Layer:
        return Layer.REPOSITORY

    def _candidates(self, task: Task) -> list[Path]:
        found: list[Path] = []

        def add(path: Path) -> None:
            if path not in found:
                found.append(path)

        readme = self._root / "README.md"
        if readme.is_file():
            add(readme)
        docs = self._root / "docs"
        if docs.is_dir():
            for path in sorted(docs.rglob("*.md")):
                add(path)
        for manifest in MANIFESTS:
            path = self._root / manifest
            if path.is_file():
                add(path)
        for file in task.files:
            add(self._root / file)
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
            for path in self._candidates(task):
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
