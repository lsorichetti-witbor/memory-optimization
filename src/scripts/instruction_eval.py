"""Compare two instruction sets rule-by-rule (handoff section 19.6).

    # score the live global instruction set
    python -m src.scripts.instruction_eval --before "$env:USERPROFILE\\.claude"

    # compare a refactor against it
    python -m src.scripts.instruction_eval --before "$env:USERPROFILE\\.claude" --after ./candidate

An instruction set is a directory holding `CLAUDE.md` and optionally
`rules/*.md`. Both layouts are accepted: `rules/` beside `CLAUDE.md` (how
`~/.claude` is laid out) and `.claude/rules/` (how a repo is laid out). It is
staged into a temporary repo-shaped directory so the real InstructionsSource
runs against it unchanged - scoring the refactor with a different reader than
the product uses would measure the reader, not the refactor.

Measures rule COVERAGE: is each invariant still present, and still selected into
the budget. It does NOT measure whether an agent behaves the same. A refactor
can pass every check here and still change behaviour, because wording matters
and this only checks the words are reachable.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from src.evaluation.instructions import RuleCase, compare, evaluate_instruction_set
from src.scripts._common import add_common_arguments, configure_stdout, emit, repo_root

DEFAULT_DATASET = "src/evaluation/datasets/instruction-rules.json"


def load_cases(path: Path) -> tuple[list[RuleCase], str]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = [
        RuleCase(
            id=c["id"],
            task=c["task"],
            invariant=c["invariant"],
            must_match=tuple(c["must_match"]),
            files=tuple(c.get("files", ())),
        )
        for c in data["cases"]
    ]
    return cases, data.get("name", str(path))


def stage(source: Path, into: Path) -> Path:
    """Lay an instruction set out the way InstructionsSource expects to find it."""
    source = Path(source)
    into.mkdir(parents=True, exist_ok=True)

    for name in ("CLAUDE.md", "AGENTS.md", "GEMINI.md"):
        candidate = source / name
        if candidate.is_file():
            shutil.copy2(candidate, into / name)

    rules_dir = into / ".claude" / "rules"
    for rules in (source / "rules", source / ".claude" / "rules"):
        if rules.is_dir():
            rules_dir.mkdir(parents=True, exist_ok=True)
            for rule in sorted(rules.glob("*.md")):
                shutil.copy2(rule, rules_dir / rule.name)
    return into


def main(argv: list[str] | None = None) -> int:
    configure_stdout()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--before", required=True, help="directory holding the current CLAUDE.md")
    parser.add_argument("--after", default=None, help="directory holding the refactored CLAUDE.md")
    parser.add_argument("--dataset", default=None)
    parser.add_argument("--max-tokens", type=int, default=8000)
    parser.add_argument("--out", default=None, help="write the report here instead of stdout")
    add_common_arguments(parser)
    args = parser.parse_args(argv)

    dataset_path = Path(args.dataset) if args.dataset else Path(repo_root()) / DEFAULT_DATASET
    if not dataset_path.is_file():
        print(f"error: dataset not found: {dataset_path}", file=sys.stderr)
        return 2
    cases, dataset_name = load_cases(dataset_path)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        before = evaluate_instruction_set(
            stage(Path(args.before), tmp_path / "before"), cases, name="before", max_tokens=args.max_tokens
        )
        after = (
            evaluate_instruction_set(
                stage(Path(args.after), tmp_path / "after"), cases, name="after", max_tokens=args.max_tokens
            )
            if args.after
            else None
        )

    if after is None:
        lines = [f"dataset: {dataset_name} ({len(cases)} rules)", "", before.summary(), ""]
        missing = [r for r in before.results if not r.present]
        if missing:
            lines.append("NOT PRESENT in this instruction set:")
            lines += [f"  {r.case_id}" for r in missing]
        unselected = [r for r in before.results if r.present and not r.selected]
        if unselected:
            lines.append("present but NOT selected into the budget:")
            lines += [f"  {r.case_id}" + ("  (pinned set exceeded the budget)" if r.budget_exceeded else "") for r in unselected]
        emit({"before": before.__dict__}, args.json, "\n".join(lines))
        return 0

    result = compare(before, after)
    lines = [
        "# Instruction set comparison",
        "",
        f"dataset: {dataset_name} ({len(cases)} rules)",
        f"budget:  {args.max_tokens} tokens",
        "",
        result.summary(),
        "",
        "| rule | before present | before selected | after present | after selected |",
        "|---|:--:|:--:|:--:|:--:|",
    ]
    b = {r.case_id: r for r in before.results}
    a = {r.case_id: r for r in after.results}
    for case in cases:
        rb, ra = b[case.id], a[case.id]
        mark = lambda v: "yes" if v else "NO"  # noqa: E731
        lines.append(
            f"| {case.id} | {mark(rb.present)} | {mark(rb.selected)} | {mark(ra.present)} | {mark(ra.selected)} |"
        )
    text = "\n".join(lines) + "\n"

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}")
        print()
        print(result.summary())
    elif args.json:
        emit(
            {
                "before": {"tokens": before.tokens, "present": before.present_count, "selected": before.selected_count},
                "after": {"tokens": after.tokens, "present": after.present_count, "selected": after.selected_count},
                "lost": result.lost,
                "unreachable": result.unreachable,
                "token_delta_pct": result.token_delta_pct,
                "regressed": result.regressed,
            },
            True,
            "",
        )
    else:
        print(text)

    # Non-zero on regression: a caller checking only the exit code must not read
    # a refactor that dropped a rule as a success.
    return 1 if result.regressed else 0


if __name__ == "__main__":
    raise SystemExit(main())
