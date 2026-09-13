from pathlib import Path

from src.context.sources.instructions import InstructionsSource
from src.context.types import Layer, Task


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_collects_claude_and_agents_files(tmp_path):
    write(tmp_path, "CLAUDE.md", "# Root policy\n\nAlways verify numbers.\n")
    write(tmp_path, "AGENTS.md", "# Agents\n\nUse pnpm, never npm.\n")
    items = InstructionsSource(root=tmp_path).collect(Task(description="anything"))
    assert {i.layer for i in items} == {Layer.INSTRUCTIONS}
    assert any("verify numbers" in i.content for i in items)
    assert any("pnpm" in i.content for i in items)


def test_every_instruction_item_is_pinned(tmp_path):
    # Policy must never be dropped by the budget.
    write(tmp_path, "CLAUDE.md", "# P\n\nA rule.\n")
    items = InstructionsSource(root=tmp_path).collect(Task(description="x"))
    assert items
    assert all(i.pinned for i in items)


def test_nearest_instructions_outrank_root_ones(tmp_path):
    write(tmp_path, "AGENTS.md", "# Root\n\nRoot rule.\n")
    write(tmp_path, "server/AGENTS.md", "# Server\n\nServer rule.\n")
    task = Task(description="edit the server", files=("server/main.py",))
    items = InstructionsSource(root=tmp_path).collect(task)
    by_source = {i.source: i for i in items}
    assert by_source["server/AGENTS.md"].signals.importance > by_source["AGENTS.md"].signals.importance


def test_a_directory_with_no_instruction_files_contributes_nothing(tmp_path):
    assert InstructionsSource(root=tmp_path).collect(Task(description="x")) == []


def test_skill_files_are_collected_only_when_the_task_names_them(tmp_path):
    write(tmp_path, "skills/context-memory/SKILL.md", "# Skill\n\nHow to build a context.\n")
    unrelated = InstructionsSource(root=tmp_path).collect(Task(description="rename a column"))
    named = InstructionsSource(root=tmp_path).collect(Task(description="run the context-memory skill"))
    assert unrelated == []
    assert any("SKILL.md" in i.source for i in named)


def test_rule_files_under_dot_claude_are_collected(tmp_path):
    write(tmp_path, ".claude/rules/testing.md", "# Testing\n\nRun the old behaviour first.\n")
    items = InstructionsSource(root=tmp_path).collect(Task(description="x"))
    assert any(".claude/rules/testing.md" in i.source for i in items)


def test_a_pointer_file_is_not_injected_as_policy(tmp_path):
    # This repo's own CLAUDE.md contains the single word "AGENTS.md". Injecting it
    # as a rule wastes budget and tells the agent nothing.
    write(tmp_path, "CLAUDE.md", "AGENTS.md\n")
    write(tmp_path, "AGENTS.md", "# Agents\n\nThe real rules live here.\n")
    items = InstructionsSource(root=tmp_path).collect(Task(description="x"))
    assert all(i.source != "CLAUDE.md" for i in items)
    assert any(i.source == "AGENTS.md" for i in items)


def test_each_section_is_a_separate_item_so_the_budget_works_per_rule(tmp_path):
    write(tmp_path, "CLAUDE.md", "# Policy\n\n## Rule A\n\nFirst.\n\n## Rule B\n\nSecond.\n")
    items = InstructionsSource(root=tmp_path).collect(Task(description="x"))
    titles = {i.title for i in items}
    assert "Policy > Rule A" in titles
    assert "Policy > Rule B" in titles


def test_sources_are_reported_relative_to_the_root_with_forward_slashes(tmp_path):
    write(tmp_path, "server/AGENTS.md", "# S\n\nRule.\n")
    items = InstructionsSource(root=tmp_path).collect(Task(description="x"))
    assert all("\\" not in i.source for i in items)
