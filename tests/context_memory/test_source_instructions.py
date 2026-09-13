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


def test_instructions_on_the_task_path_are_pinned(tmp_path):
    # Policy that applies to this task must never be dropped by the budget.
    write(tmp_path, "CLAUDE.md", "# P\n\nA rule.\n")
    items = InstructionsSource(root=tmp_path).collect(Task(description="x"))
    assert items
    assert all(i.pinned for i in items)


def test_instructions_from_unrelated_directories_are_not_pinned(tmp_path):
    # A polyglot monorepo has one AGENTS.md per package. Pinning all of them made
    # 13,373 tokens of "policy" unskippable against an 8,000 token budget, and the
    # build raised rather than ran. The repo's own rule is to read the AGENTS.md
    # NEAREST the files being edited - so distant ones are candidates, not policy.
    write(tmp_path, "AGENTS.md", "# Root\n\nRoot rule.\n")
    write(tmp_path, "server/AGENTS.md", "# Server\n\nServer rule.\n")
    write(tmp_path, "cli/node/AGENTS.md", "# Cli\n\nCli rule.\n")
    items = {
        i.source: i
        for i in InstructionsSource(root=tmp_path).collect(
            Task(description="edit", files=("server/main.py",))
        )
    }
    assert items["AGENTS.md"].pinned is True
    assert items["server/AGENTS.md"].pinned is True
    assert items["cli/node/AGENTS.md"].pinned is False


def test_nearest_instructions_outrank_root_ones(tmp_path):
    write(tmp_path, "AGENTS.md", "# Root\n\nRoot rule.\n")
    write(tmp_path, "server/AGENTS.md", "# Server\n\nServer rule.\n")
    task = Task(description="edit the server", files=("server/main.py",))
    items = InstructionsSource(root=tmp_path).collect(task)
    by_source = {i.source: i for i in items}
    assert by_source["server/AGENTS.md"].signals.importance > by_source["AGENTS.md"].signals.importance


def test_instruction_files_outside_the_task_directories_are_still_collected(tmp_path):
    # Measured on the real repo: 11 AGENTS.md files, but only the root one and
    # those on a task file's path were reachable. A question about the server
    # could not see server/AGENTS.md unless the task happened to name a file
    # under server/ - and a missing candidate reads as bad ranking.
    write(tmp_path, "AGENTS.md", "# Root\n\nRoot rule.\n")
    write(tmp_path, "server/AGENTS.md", "# Server\n\nServer rule.\n")
    items = InstructionsSource(root=tmp_path).collect(Task(description="how do I start the server"))
    assert any(i.source == "server/AGENTS.md" for i in items)


def test_a_task_file_still_outranks_a_merely_discovered_instruction_file(tmp_path):
    write(tmp_path, "server/AGENTS.md", "# Server\n\nServer rule.\n")
    write(tmp_path, "cli/AGENTS.md", "# Cli\n\nCli rule.\n")
    items = {
        i.source: i
        for i in InstructionsSource(root=tmp_path).collect(
            Task(description="edit", files=("server/main.py",))
        )
    }
    assert items["server/AGENTS.md"].signals.importance > items["cli/AGENTS.md"].signals.importance


def test_vendor_directories_are_never_walked_for_instructions(tmp_path):
    write(tmp_path, "AGENTS.md", "# Root\n\nRoot rule.\n")
    write(tmp_path, "node_modules/pkg/AGENTS.md", "# Vendor\n\nNot ours.\n")
    items = InstructionsSource(root=tmp_path).collect(Task(description="x"))
    assert all(not i.source.startswith("node_modules/") for i in items)


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
