"""Render the selected items into the final context.

Sections follow the precedence ladder, so an agent reading top to bottom meets
policy before history. Every item states where it came from: an assertion with
no provenance cannot be checked.

Memory items carry confidence and age. Instruction items deliberately do not -
policy is not probabilistic, and a confidence score beside a rule invites
treating it as advisory.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.context.types import ContextItem, Layer

_HEADINGS = {
    Layer.INSTRUCTIONS: "Instructions (policy - must be followed)",
    Layer.CURRENT_STATE: "Current state",
    Layer.REPOSITORY: "Repository",
    Layer.MEMORY: "Memory (historical - outranked by anything above)",
}

EMPTY_MARKER = "_No context selected._"


@dataclass
class CompiledContext:
    text: str
    items: list[ContextItem]


class ContextCompiler:
    def compile(self, items: list[ContextItem]) -> CompiledContext:
        if not items:
            # A blank string would be indistinguishable from a failed build.
            return CompiledContext(text=EMPTY_MARKER, items=[])

        lines: list[str] = []
        for layer in Layer.precedence():
            in_layer = [i for i in items if i.layer is layer]
            if not in_layer:
                continue
            lines.append(f"## {_HEADINGS[layer]}")
            lines.append("")
            for item in in_layer:
                lines.append(f"### {item.title or item.id}")
                lines.append(f"_source: {item.source}_{self._annotation(item)}")
                lines.append("")
                lines.append(item.content.strip())
                lines.append("")
        return CompiledContext(text="\n".join(lines).strip() + "\n", items=list(items))

    @staticmethod
    def _annotation(item: ContextItem) -> str:
        if item.layer is not Layer.MEMORY:
            return ""
        parts = [f"confidence {item.signals.confidence:g}"]
        age = item.metadata.get("age_days")
        if age is not None:
            parts.append(f"{age}d old")
        if item.signals.staleness >= 1.0:
            overruled = item.metadata.get("overruled_by")
            parts.append("**STALE**" + (f", overruled by {overruled}" if overruled else ""))
        return " — " + ", ".join(parts)
