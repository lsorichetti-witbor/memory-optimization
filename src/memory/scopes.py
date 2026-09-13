"""Scope vocabulary and its mapping onto Mem0's three identifiers.

Handoff section 8 names seven conceptual scopes. Mem0 exposes only `user_id`,
`agent_id` and `run_id` as first-class filters, so the mapping is:

    user                -> user_id
    project/repository  -> agent_id
    branch/session/task -> run_id
    the scope level     -> metadata["scope"]
    the scope key       -> metadata["scope_key"]

The vocabulary is closed. A typo'd scope that passed through would write
memories no query ever matches, and "no results" is indistinguishable from
"nothing stored yet" - so parsing an unknown scope raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Scope(str, Enum):
    GLOBAL = "global"
    USER = "user"
    PROJECT = "project"
    REPOSITORY = "repository"
    BRANCH = "branch"
    SESSION = "session"
    TASK = "task"

    @classmethod
    def ordered(cls) -> list["Scope"]:
        """Broad to narrow."""
        return [cls.GLOBAL, cls.USER, cls.PROJECT, cls.REPOSITORY, cls.BRANCH, cls.SESSION, cls.TASK]

    @classmethod
    def parse(cls, value: str) -> "Scope":
        try:
            return cls(value)
        except ValueError:
            raise ValueError(f"unknown scope: {value!r}. Known: {', '.join(SCOPES)}") from None


SCOPES: tuple[str, ...] = tuple(s.value for s in Scope.ordered())

# Which Mem0 identifier carries each scope, and how its key is prefixed.
_AGENT_PREFIX = {Scope.PROJECT: "project:", Scope.REPOSITORY: "repo:", Scope.USER: "user:"}
_RUN_PREFIX = {Scope.BRANCH: "branch:", Scope.SESSION: "session:", Scope.TASK: "task:"}

# The global scope's agent. A constant rather than a per-user value: knowledge at
# this scope belongs to everyone, and keying it per user would split one scope
# into one per person.
GLOBAL_AGENT = "global"


def scope_identifiers(
    scope: Scope,
    key: str,
    user: str,
    repository: Optional[str] = None,
    project: Optional[str] = None,
) -> dict[str, str]:
    """Build the Mem0 identifier set for a scope.

    `user_id` is always present: POST /memories rejects a body carrying none of
    the three identifiers (server/main.py, add_memory).
    """
    if not user:
        raise ValueError("user is required: Mem0 needs at least one identifier on every write")

    ids: dict[str, str] = {"user_id": user}

    # Every scope gets an agent_id. Without one, a global memory carried only a
    # user_id: it could not be narrowed server-side, formed no entity of its own
    # in the dashboard, and the bulk delete endpoint could not address it without
    # hitting every other scope that user owned.
    if scope is Scope.GLOBAL:
        ids["agent_id"] = GLOBAL_AGENT
    elif scope in _AGENT_PREFIX:
        ids["agent_id"] = f"{_AGENT_PREFIX[scope]}{key}"
    elif scope in _RUN_PREFIX:
        ids["run_id"] = f"{_RUN_PREFIX[scope]}{key}"
        # A branch/session/task memory still belongs to a repository. Keeping the
        # agent_id lets a repository-wide query see it.
        if repository:
            ids["agent_id"] = f"repo:{repository}"
        elif project:
            ids["agent_id"] = f"project:{project}"

    return ids


@dataclass(frozen=True)
class ScopeSelector:
    """The keys available for the current situation, and the scopes worth querying.

    A scope with no key is skipped rather than queried with a placeholder: a
    query against a made-up key silently returns nothing.
    """

    user: str
    project: Optional[str] = None
    repository: Optional[str] = None
    branch: Optional[str] = None
    session: Optional[str] = None
    task: Optional[str] = None
    include_global: bool = True

    def key_for(self, scope: Scope) -> Optional[str]:
        if scope is Scope.GLOBAL:
            return "global" if self.include_global else None
        return {
            Scope.USER: self.user,
            Scope.PROJECT: self.project,
            Scope.REPOSITORY: self.repository,
            Scope.BRANCH: self.branch,
            Scope.SESSION: self.session,
            Scope.TASK: self.task,
        }[scope]

    def scopes_to_query(self) -> list[Scope]:
        """Narrow to broad: the most specific memory should be seen first."""
        return [s for s in reversed(Scope.ordered()) if self.key_for(s)]
