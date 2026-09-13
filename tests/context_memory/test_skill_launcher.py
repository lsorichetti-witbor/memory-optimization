"""The skill launcher and the CLI it drives must share one scope vocabulary.

`ctx.ps1` is the only thing most callers ever run, and it is PowerShell, so no
import, type check or Python test touches it. When `Scope.REPOSITORY` changed
value from `repository` to `repo` the launcher kept translating `repo` back to
`repository` on its way to the CLI, and argparse rejected it: every
`ctx.ps1 store -Scope repo` and the repo half of every `ctx.ps1 search` failed,
while the launcher source looked untouched and every Python test stayed green.

These tests read the launcher as text and assert the property that broke.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.memory.scopes import SCOPES

LAUNCHER = Path(__file__).resolve().parents[2] / "skills" / "context-memory" / "scripts" / "ctx.ps1"


def scope_words_handed_to_the_cli(text: str) -> set[str]:
    """Every literal scope word the launcher can pass as `--scope`.

    Two call shapes appear in the launcher: a bare argument (`--scope repo`) and
    an array element (`'--scope', 'repo'`). A word passed through a variable is
    invisible here, which is why `validate_set_values` is checked as well - the
    variable's domain is the ValidateSet.
    """
    bare = re.findall(r"--scope\s+([A-Za-z][\w-]*)", text)
    quoted = re.findall(r"'--scope'\s*,\s*'([^']+)'", text)
    return {w for w in bare + quoted if not w.startswith("$")}


def validate_set_values(text: str, parameter: str) -> set[str]:
    """The declared domain of a `[ValidateSet(...)] $Parameter` in the launcher."""
    match = re.search(
        r"\[ValidateSet\(([^)]*)\)\]\s*\[[^\]]+\]\s*\$" + re.escape(parameter) + r"\b",
        text,
    )
    if not match:
        raise AssertionError(f"no ValidateSet found for ${parameter} in {LAUNCHER.name}")
    return set(re.findall(r"'([^']+)'", match.group(1)))


@pytest.fixture(scope="module")
def launcher_text() -> str:
    assert LAUNCHER.is_file(), f"launcher missing: {LAUNCHER}"
    return LAUNCHER.read_text(encoding="utf-8-sig")


def test_every_scope_word_the_launcher_passes_is_one_the_cli_accepts(launcher_text):
    words = scope_words_handed_to_the_cli(launcher_text)
    assert words, "found no --scope arguments at all; the extraction is broken, not the launcher"
    unknown = sorted(words - set(SCOPES))
    assert not unknown, (
        f"{LAUNCHER.name} passes --scope {unknown} but the CLI accepts only {sorted(SCOPES)}. "
        f"argparse rejects the call, so the command fails for the user with no Python test failing."
    )


def test_the_user_facing_scope_flag_offers_only_real_scopes(launcher_text):
    # -Scope is what a caller types. Every value it permits has to survive the
    # trip to argparse, whatever the launcher does with it in between.
    unknown = sorted(validate_set_values(launcher_text, "Scope") - set(SCOPES))
    assert not unknown, f"-Scope accepts {unknown}, which the CLI's --scope does not"


def test_the_launcher_does_not_translate_the_scope_word_on_its_way_to_the_cli(launcher_text):
    # The translation is what broke: one name on the command line, another in
    # the data, and nothing to notice when only one of them moved.
    assert "$scopeName = $Scope" in launcher_text, (
        "the launcher is mapping -Scope to some other word again; keep one name per scope"
    )


def test_the_extraction_catches_a_bad_scope_word():
    # Guards the guard: if the regex stopped matching, the tests above would
    # pass on a launcher that passes nonsense. This is the red state the real
    # test was written against - the launcher as it stood before the fix.
    broken = "$repoOut = & $Python @common --scope repository --key $RepoKey\n"
    assert scope_words_handed_to_the_cli(broken) == {"repository"}
    assert "repository" not in set(SCOPES)
