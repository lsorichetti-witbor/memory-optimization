import pytest

from src.evaluation.instructions import (
    InstructionSetReport,
    RuleCase,
    compare,
    evaluate_instruction_set,
)


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def case(id_, task, must_match, invariant="an invariant"):
    return RuleCase(id=id_, task=task, invariant=invariant, must_match=tuple(must_match))


# ---------------------------------------------------------------- the case


def test_a_case_must_name_something_to_match():
    # A case matching nothing scores every instruction set identically and so
    # measures nothing, while still counting towards the total.
    with pytest.raises(ValueError, match="must_match"):
        RuleCase(id="c1", task="t", invariant="i", must_match=())


# ------------------------------------------------------------- presence


def test_a_rule_in_the_instruction_set_is_present(tmp_path):
    write(tmp_path, "CLAUDE.md", "# Policy\n\n## Package manager\n\nUse pnpm install, never npm install.\n")
    report = evaluate_instruction_set(
        tmp_path, [case("pm", "install the dependencies", ["pnpm"])], name="before"
    )
    assert report.results[0].present is True


def test_a_rule_absent_from_the_instruction_set_is_reported_absent(tmp_path):
    write(tmp_path, "CLAUDE.md", "# Policy\n\n## Something else\n\nUnrelated text.\n")
    report = evaluate_instruction_set(
        tmp_path, [case("pm", "install the dependencies", ["pnpm"])], name="before"
    )
    assert report.results[0].present is False
    assert report.results[0].selected is False


def test_every_token_of_must_match_has_to_appear_in_one_chunk(tmp_path):
    # Split across two unrelated rules, the invariant is not intact anywhere.
    write(
        tmp_path,
        "CLAUDE.md",
        "# P\n\n## A\n\nUse pnpm for things.\n\n## B\n\nNever use npm install.\n",
    )
    report = evaluate_instruction_set(
        tmp_path, [case("pm", "install deps", ["pnpm", "npm install"])], name="before"
    )
    assert report.results[0].present is False


def test_a_rule_in_an_imported_rules_file_counts_as_present(tmp_path):
    write(tmp_path, "CLAUDE.md", "# Policy\n\nSee the rules directory.\n")
    write(tmp_path, ".claude/rules/netsuite.md", "# NetSuite\n\nAlways apply the SuiteScript best practices skill.\n")
    report = evaluate_instruction_set(
        tmp_path, [case("ns", "write a suitelet", ["SuiteScript best practices"])], name="after"
    )
    assert report.results[0].present is True
    assert ".claude/rules/netsuite.md" in report.results[0].matched_source


# ------------------------------------------------------------- selection


def test_a_present_rule_that_fits_the_budget_is_selected(tmp_path):
    write(tmp_path, "CLAUDE.md", "# Policy\n\n## Package manager\n\nUse pnpm install, never npm install.\n")
    report = evaluate_instruction_set(
        tmp_path, [case("pm", "install the dependencies", ["pnpm"])], name="before", max_tokens=4000
    )
    assert report.results[0].selected is True


def test_present_but_unreachable_is_not_the_same_as_present(tmp_path):
    # The distinction the whole benchmark exists for: a rule can survive a
    # refactor and still never reach the agent, and "present" alone would call
    # that a success.
    write(tmp_path, "CLAUDE.md", "# Policy\n\n## Rule\n\nUse pnpm install, never npm install.\n")
    tiny = evaluate_instruction_set(
        tmp_path, [case("pm", "install deps", ["pnpm"])], name="tiny", max_tokens=1
    )
    assert tiny.results[0].present is True
    assert tiny.results[0].selected is False


def test_tokens_are_measured_for_the_instruction_layer(tmp_path):
    write(tmp_path, "CLAUDE.md", "# Policy\n\n" + ("word " * 400))
    report = evaluate_instruction_set(tmp_path, [case("x", "t", ["Policy"])], name="before")
    assert report.tokens > 100


def test_the_report_states_both_denominators(tmp_path):
    write(tmp_path, "CLAUDE.md", "# P\n\n## A\n\nUse pnpm install here.\n")
    report = evaluate_instruction_set(
        tmp_path,
        [case("a", "t", ["pnpm"]), case("b", "t", ["something absent"])],
        name="before",
    )
    assert "1 of 2 rules present" in report.summary()
    assert "1 of 2 selected" in report.summary()


# ------------------------------------------------------------- comparison


def _report(name, results):
    return InstructionSetReport(name=name, tokens=100, results=results)


def test_comparison_flags_a_rule_that_was_lost(tmp_path):
    write(tmp_path, "before/CLAUDE.md", "# P\n\n## A\n\nUse pnpm install, never npm.\n")
    write(tmp_path, "after/CLAUDE.md", "# P\n\n## A\n\nUnrelated policy text.\n")
    cases = [case("pm", "install deps", ["pnpm"])]
    before = evaluate_instruction_set(tmp_path / "before", cases, name="before")
    after = evaluate_instruction_set(tmp_path / "after", cases, name="after")

    result = compare(before, after)
    assert result.lost == ["pm"]
    assert result.regressed is True


def test_comparison_flags_a_rule_that_survived_but_became_unreachable(tmp_path):
    write(tmp_path, "before/CLAUDE.md", "# P\n\n## A\n\nUse pnpm install, never npm.\n")
    write(tmp_path, "after/CLAUDE.md", "# P\n\n## A\n\nUse pnpm install, never npm.\n")
    cases = [case("pm", "install deps", ["pnpm"])]
    before = evaluate_instruction_set(tmp_path / "before", cases, name="before", max_tokens=4000)
    after = evaluate_instruction_set(tmp_path / "after", cases, name="after", max_tokens=1)

    result = compare(before, after)
    assert result.lost == []
    assert result.unreachable == ["pm"]
    assert result.regressed is True


def test_a_clean_refactor_is_not_a_regression(tmp_path):
    write(tmp_path, "before/CLAUDE.md", "# P\n\n## A\n\nUse pnpm install, never npm.\n\n## Noise\n\n" + ("filler " * 300))
    write(tmp_path, "after/CLAUDE.md", "# P\n\n## A\n\nUse pnpm install, never npm.\n")
    cases = [case("pm", "install deps", ["pnpm"])]
    before = evaluate_instruction_set(tmp_path / "before", cases, name="before")
    after = evaluate_instruction_set(tmp_path / "after", cases, name="after")

    result = compare(before, after)
    assert result.regressed is False
    assert result.token_delta < 0  # the whole point of the refactor


def test_comparison_reports_the_token_reduction_as_a_percentage(tmp_path):
    write(tmp_path, "before/CLAUDE.md", "# P\n\n## A\n\nUse pnpm install.\n\n" + ("filler " * 400))
    write(tmp_path, "after/CLAUDE.md", "# P\n\n## A\n\nUse pnpm install.\n")
    cases = [case("pm", "install deps", ["pnpm"])]
    before = evaluate_instruction_set(tmp_path / "before", cases, name="before")
    after = evaluate_instruction_set(tmp_path / "after", cases, name="after")
    assert "%" in compare(before, after).summary()


def test_the_comparison_refuses_to_call_a_regression_an_improvement(tmp_path):
    # A refactor that saves tokens by dropping a rule must not read as a win.
    write(tmp_path, "before/CLAUDE.md", "# P\n\n## A\n\nUse pnpm install, never npm.\n\n" + ("filler " * 400))
    write(tmp_path, "after/CLAUDE.md", "# P\n\n## A\n\nUnrelated.\n")
    cases = [case("pm", "install deps", ["pnpm"])]
    before = evaluate_instruction_set(tmp_path / "before", cases, name="before")
    after = evaluate_instruction_set(tmp_path / "after", cases, name="after")
    result = compare(before, after)
    assert result.regressed is True
    assert "REGRESSION" in result.summary()
