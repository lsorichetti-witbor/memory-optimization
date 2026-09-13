"""Instruction layer: CLAUDE.md, AGENTS.md, rule files, skill definitions.

Instructions are policy, not memory (handoff section 3), so instruction files on
the task's own path are pinned: if that policy does not fit the budget it is an
error the operator must see, not something to trim quietly.

Instruction files found elsewhere in the tree are NOT pinned. In a polyglot
monorepo every package carries its own AGENTS.md, and `cli/node/AGENTS.md` is
not policy for a task about the server - this repo's own rule is to read the
AGENTS.md nearest the files being edited. They are still collected, ranked and
budgeted like any other candidate, because a file that is never a candidate
reads downstream as a ranking failure rather than as missing input.

Nearest wins. A repo where `server/AGENTS.md` overrides the root one is the
normal case, and the root file must not outrank the specific one.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from src.context.chunking import chunk_markdown
from src.context.sources.base import ContextSource
from src.context.sources.repository import is_excluded
from src.context.types import ContextItem, Layer, Signals, Task

INSTRUCTION_FILENAMES = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")
RULES_GLOB = ".claude/rules/*.md"
SKILLS_GLOB = "skills/*/SKILL.md"

# A file whose entire content is a pointer to another instruction file, e.g. this
# repo's own CLAUDE.md, which contains the single word "AGENTS.md".
_POINTER = re.compile(r"^\s*[\w./-]+\.md\s*$")

_MIN_IMPORTANCE = 0.5
_IMPORTANCE_STEP = 0.1
# Distance assigned to an instruction file found by walking the tree rather than
# by following the task's own path. Large enough that anything on the task's path
# outranks it, and it still floors at _MIN_IMPORTANCE.
_DISCOVERED_DISTANCE = 50


class InstructionsSource(ContextSource):
    def __init__(self, root: Path, max_chars: int = 4000) -> None:
        self._root = Path(root)
        self._max_chars = max_chars
        self.last_error: Optional[BaseException] = None

    @property
    def name(self) -> str:
        return "instructions"

    @property
    def layer(self) -> Layer:
        return Layer.INSTRUCTIONS

    def _relative(self, path: Path) -> str:
        return path.relative_to(self._root).as_posix()

    def _task_directories(self, task: Task) -> list[Path]:
        """Directories the task touches, deepest first, plus the root."""
        directories: list[Path] = []
        for file in task.files:
            current = (self._root / file).parent
            while True:
                if current not in directories:
                    directories.append(current)
                if current == self._root or self._root not in current.parents:
                    break
                current = current.parent
        if self._root not in directories:
            directories.append(self._root)
        return directories

    def _candidate_files(self, task: Task) -> list[tuple[Path, int]]:
        """(path, distance-from-task) pairs. Distance 0 is the task's own directory."""
        found: dict[Path, int] = {}

        for distance, directory in enumerate(self._task_directories(task)):
            for filename in INSTRUCTION_FILENAMES:
                path = directory / filename
                if path.is_file():
                    found.setdefault(path, distance)

        # Instruction files anywhere else in the tree. Measured on this repo:
        # 11 AGENTS.md, of which only the root one and those on a task file's
        # path were reachable - so a question about the server could not see
        # server/AGENTS.md at all unless the task named a file under server/.
        # They enter at a distance beyond any real one, so a file the task
        # actually touches still outranks them.
        for filename in INSTRUCTION_FILENAMES:
            for path in sorted(self._root.rglob(filename)):
                relative = path.relative_to(self._root).parts
                if is_excluded(relative) or not path.is_file():
                    continue
                found.setdefault(path, _DISCOVERED_DISTANCE)

        for path in sorted(self._root.glob(RULES_GLOB)):
            found.setdefault(path, 0)

        description = task.description.lower()
        for path in sorted(self._root.glob(SKILLS_GLOB)):
            skill_name = path.parent.name.lower()
            if skill_name in description:
                found.setdefault(path, 0)

        return sorted(found.items(), key=lambda pair: (pair[1], pair[0].as_posix()))

    def collect(self, task: Task) -> list[ContextItem]:
        self.last_error = None
        items: list[ContextItem] = []
        try:
            for path, distance in self._candidate_files(task):
                text = path.read_text(encoding="utf-8", errors="replace")
                if _POINTER.match(text):
                    # A pointer file carries no policy; injecting it wastes budget.
                    continue
                importance = max(_MIN_IMPORTANCE, 1.0 - _IMPORTANCE_STEP * distance)
                source = self._relative(path)
                # Only instructions on the task's own path are policy for THIS
                # task, and only policy gets pinned. A monorepo has one AGENTS.md
                # per package; pinning all of them made the pinned set exceed any
                # sane budget and the build raised instead of running.
                on_task_path = distance < _DISCOVERED_DISTANCE
                for chunk in chunk_markdown(text, source=source, max_chars=self._max_chars):
                    items.append(
                        ContextItem(
                            id=chunk.id,
                            layer=Layer.INSTRUCTIONS,
                            content=chunk.content,
                            source=source,
                            title=chunk.title,
                            pinned=on_task_path,
                            signals=Signals(confidence=1.0, importance=importance, recency=1.0),
                            metadata={"distance": distance},
                        )
                    )
        except OSError as error:
            self.last_error = error
        return items
