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


def test_every_scope_produces_an_agent_id():
    # Without one, a global memory has only a user_id: it cannot be narrowed
    # server-side, it forms no entity of its own in the dashboard, and the bulk
    # delete endpoint cannot address it without hitting every other scope the
    # user owns. Measured before this: 11 shared memories were visible only
    # inside "user lautaro: 16".
    for scope, key in (
        (Scope.GLOBAL, "global"),
        (Scope.USER, "lautaro"),
        (Scope.PROJECT, "gestion"),
        (Scope.REPOSITORY, "memory-optimization"),
        (Scope.BRANCH, "main"),
        (Scope.SESSION, "s1"),
        (Scope.TASK, "t1"),
    ):
        ids = scope_identifiers(scope, key=key, user="lautaro", repository="memory-optimization")
        assert ids.get("agent_id"), f"{scope.value} produced no agent_id: {ids}"


def test_the_global_scope_agent_is_the_same_for_every_user():
    # Shared knowledge is shared. Keying the agent per user would split one
    # shared scope into one per person.
    a = scope_identifiers(Scope.GLOBAL, key="global", user="lautaro")
    b = scope_identifiers(Scope.GLOBAL, key="global", user="someone-else")
    assert a["agent_id"] == b["agent_id"] == "global"


def test_the_user_scope_agent_names_the_user():
    ids = scope_identifiers(Scope.USER, key="lautaro", user="lautaro")
    assert ids["agent_id"] == "user:lautaro"


def test_a_task_still_carries_its_repository_agent_not_a_task_agent():
    # Unchanged on purpose: a task memory has to stay visible to a
    # repository-wide query, so the agent names the repository and run_id
    # carries the task.
    ids = scope_identifiers(Scope.TASK, key="t1", user="u", repository="memory-optimization")
    assert ids["agent_id"] == "repo:memory-optimization"
    assert ids["run_id"] == "task:t1"
