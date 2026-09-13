"""Heading-aware markdown chunking.

Injecting a whole CLAUDE.md defeats the point of a context manager, so file
sources emit one chunk per section and the budget decides which sections
survive. Chunk ids embed the source path: two files with the same headings must
never share an id, and a truncated id must still resolve to one chunk.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")

DEFAULT_MAX_CHARS = 4000


@dataclass(frozen=True)
class Chunk:
    id: str
    title: str
    content: str
    source: str
    heading_level: int
    start_line: int


def _chunk_id(source: str, title: str, ordinal: int) -> str:
    digest = hashlib.sha1(f"{source}::{title}::{ordinal}".encode("utf-8")).hexdigest()[:12]
    # The source basename leads so a shortened id still names its file.
    return f"{source.rsplit('/', 1)[-1]}:{digest}"


def _split_oversized(body: str, max_chars: int) -> list[str]:
    """Split on paragraph boundaries, never mid-paragraph, and lose nothing."""
    paragraphs = body.split("\n\n")
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for paragraph in paragraphs:
        addition = len(paragraph) + 2
        if current and size + addition > max_chars:
            parts.append("\n\n".join(current))
            current, size = [], 0
        current.append(paragraph)
        size += addition
    if current:
        parts.append("\n\n".join(current))
    return parts or [body]


def single_chunk(text: str, source: str) -> list[Chunk]:
    """One chunk for the whole file, for formats where `#` is not a heading.

    Running the markdown splitter over Python or shell would shred the file on
    every comment line.
    """
    body = text.strip()
    if not body:
        return []
    return [
        Chunk(
            id=_chunk_id(source, source, 0),
            title=source,
            content=body,
            source=source,
            heading_level=0,
            start_line=1,
        )
    ]


def chunk_markdown(text: str, source: str, max_chars: int = DEFAULT_MAX_CHARS) -> list[Chunk]:
    lines = text.splitlines()

    sections: list[tuple[str, int, int, list[str]]] = []  # title, level, start_line, body
    path: list[tuple[int, str]] = []
    current: list[str] | None = None
    current_title = source
    current_level = 0
    current_start = 1
    in_fence = False

    def flush() -> None:
        if current is not None:
            sections.append((current_title, current_level, current_start, current))

    for number, line in enumerate(lines, start=1):
        if _FENCE.match(line):
            in_fence = not in_fence
        match = None if in_fence else _HEADING.match(line)
        if match:
            flush()
            level = len(match.group(1))
            heading = match.group(2).strip()
            while path and path[-1][0] >= level:
                path.pop()
            path.append((level, heading))
            current_title = " > ".join(part for _, part in path)
            current_level = level
            current_start = number
            current = []
        else:
            if current is None:
                current = []
                current_title = source
                current_level = 0
                current_start = number
            current.append(line)
    flush()

    chunks: list[Chunk] = []
    pending: list[tuple[str, int, int, str]] = []
    for title, level, start_line, body_lines in sections:
        body = "\n".join(body_lines).strip()
        if not body and level == 0:
            continue
        for part in _split_oversized(body, max_chars) if len(body) > max_chars else [body]:
            pending.append((title, level, start_line, part))

    # Ordinal suffixes are only meaningful once the total is known.
    if len(pending) > 1 and len({t for t, _, _, _ in pending}) < len(pending):
        counts: dict[str, int] = {}
        for title, _, _, _ in pending:
            counts[title] = counts.get(title, 0) + 1
        seen: dict[str, int] = {}
        renamed = []
        for title, level, start_line, body in pending:
            if counts[title] > 1:
                seen[title] = seen.get(title, 0) + 1
                renamed.append((f"{title} ({seen[title]}/{counts[title]})", level, start_line, body))
            else:
                renamed.append((title, level, start_line, body))
        pending = renamed

    for ordinal, (title, level, start_line, body) in enumerate(pending):
        if not body.strip():
            continue
        chunks.append(
            Chunk(
                id=_chunk_id(source, title, ordinal),
                title=title,
                content=body,
                source=source,
                heading_level=level,
                start_line=start_line,
            )
        )
    return chunks
