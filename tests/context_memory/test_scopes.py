import pytest

from src.memory.scopes import SCOPES, Scope, ScopeSelector, scope_identifiers


def test_scope_vocabulary_is_exactly_the_seven_documented_levels():
    # Spelled out independently of the implementation's own constant on purpose:
    # iterating SCOPES to build this set would pass by construction.
    assert set(SCOPES) == {
        "global",
        "user",
        "project",
        "repository",
        "branch",
        "session",
        "task",
    }


def test_scope_is_ordered_from_broad_to_narrow():
    assert [s.value for s in Scope.ordered()] == [
        "global",
        "user",
        "project",
        "repository",
        "branch",
        "session",
        "task",
    ]


def test_unknown_scope_raises():
    with pytest.raises(ValueError, match="unknown scope"):
        Scope.parse("repositry")


def test_scope_identifiers_always_populate_user_id():
    # POST /memories rejects a body with no user_id/agent_id/run_id.
    ids = scope_identifiers(Scope.GLOBAL, key="engineering", user="lautaro")
    assert ids["user_id"] == "lautaro"


def test_repository_scope_lands_on_agent_id():
    ids = scope_identifiers(Scope.REPOSITORY, key="memory-optimization", user="lautaro")
    assert ids["agent_id"] == "repo:memory-optimization"
    assert "run_id" not in ids


def test_task_scope_lands_on_run_id_and_keeps_the_repository():
    ids = scope_identifiers(
        Scope.TASK, key="2026-09-13-context-memory", user="lautaro", repository="memory-optimization"
    )
    assert ids["run_id"] == "task:2026-09-13-context-memory"
    assert ids["agent_id"] == "repo:memory-optimization"


def test_selector_widens_from_narrow_to_broad():
    selector = ScopeSelector(
        user="lautaro",
        project="gestion",
        repository="memory-optimization",
        branch="main",
        session="s1",
        task="t1",
    )
    assert selector.scopes_to_query() == [
        Scope.TASK,
        Scope.SESSION,
        Scope.BRANCH,
        Scope.REPOSITORY,
        Scope.PROJECT,
        Scope.USER,
        Scope.GLOBAL,
    ]


def test_selector_skips_scopes_with_no_key():
    selector = ScopeSelector(user="lautaro", repository="memory-optimization")
    assert Scope.PROJECT not in selector.scopes_to_query()
    assert Scope.GLOBAL in selector.scopes_to_query()


def test_selector_exposes_the_key_for_each_scope_it_will_query():
    selector = ScopeSelector(user="lautaro", repository="memory-optimization")
    assert selector.key_for(Scope.REPOSITORY) == "memory-optimization"
    assert selector.key_for(Scope.USER) == "lautaro"
    assert selector.key_for(Scope.GLOBAL) == "global"
    assert selector.key_for(Scope.PROJECT) is None
