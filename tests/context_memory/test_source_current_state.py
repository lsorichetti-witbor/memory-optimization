import subprocess

import pytest

from src.context.sources.current_state import CurrentStateSource, scrubbed_env
from src.context.types import Layer, Task


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-b", "work")
    git("config", "user.email", "t@t.test")
    git("config", "user.name", "t")
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    git("add", "a.txt")
    git("commit", "-m", "initial")
    return tmp_path


def items_by_title(root):
    return {i.title: i.content for i in CurrentStateSource(root=root).collect(Task(description="x"))}


def test_reports_the_branch(repo):
    items = CurrentStateSource(root=repo).collect(Task(description="x"))
    assert {i.layer for i in items} == {Layer.CURRENT_STATE}
    assert "work" in next(i.content for i in items if i.title == "branch")


def test_reports_modified_files(repo):
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    assert "a.txt" in items_by_title(repo)["working tree"]


def test_paths_with_spaces_survive_intact(repo):
    (repo / "a file with spaces.txt").write_text("x\n", encoding="utf-8")
    assert "a file with spaces.txt" in items_by_title(repo)["working tree"]


def test_paths_with_non_ascii_survive_intact(repo):
    (repo / "gestión.txt").write_text("x\n", encoding="utf-8")
    # A line-split parse of `git status` quotes this into \303\263 escapes.
    assert "gestión.txt" in items_by_title(repo)["working tree"]


def test_reports_recent_commits(repo):
    assert "initial" in items_by_title(repo)["recent commits"]


def test_a_clean_tree_says_so_rather_than_emitting_an_empty_item(repo):
    # An empty string is indistinguishable from a failed git call.
    assert "clean" in items_by_title(repo)["working tree"].lower()


def test_env_scrub_removes_the_vars_that_override_cwd():
    env = scrubbed_env({"GIT_DIR": "/elsewhere/.git", "GIT_WORK_TREE": "/elsewhere", "PATH": "/usr/bin"})
    assert "GIT_DIR" not in env
    assert "GIT_WORK_TREE" not in env
    assert env["PATH"] == "/usr/bin"


def test_a_non_git_directory_yields_nothing_and_says_so(tmp_path):
    source = CurrentStateSource(root=tmp_path)
    assert source.collect(Task(description="x")) == []
    assert source.last_error is not None


def test_current_state_items_are_maximally_recent(repo):
    items = CurrentStateSource(root=repo).collect(Task(description="x"))
    assert all(i.signals.recency == 1.0 for i in items)


def test_an_inherited_git_dir_does_not_redirect_the_reads(repo, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("GIT_DIR", str(other))
    items = CurrentStateSource(root=repo).collect(Task(description="x"))
    assert "work" in next(i.content for i in items if i.title == "branch")
