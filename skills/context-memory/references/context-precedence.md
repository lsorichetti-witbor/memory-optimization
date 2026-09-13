# Context precedence and conflict resolution

## The ladder

```
System / platform instructions
            ▼
CLAUDE.md / AGENTS.md / skill rules
            ▼
Current task state        (git diff, tests, tool output)
            ▼
Current repository / code
            ▼
README / docs
            ▼
Recent validated memory
            ▼
Older historical memory
```

Implemented as four layers: `INSTRUCTIONS → CURRENT_STATE → REPOSITORY → MEMORY`.

Current state sits above repository truth because a file the task is actively
editing is more current than its committed version.

The rule that matters:

> **Historical memory must not silently override current repository truth.**

Worked example:

```
Mem0 says:            "Project uses Redis."
Repository says:      "PostgreSQL is the vector store."
Result:               use PostgreSQL; the Redis memory is marked stale.
```

The stale memory is still rendered, annotated `STALE — overruled by <id>`. It is
not deleted and not silently dropped: a reader who knows a memory existed and
lost is better informed than one who never saw it.

## The two conflict rules

**`topic_supersession`** — items sharing a `metadata["topic"]` key form a group.
The most authoritative member wins: higher-precedence layer first, and between
two memories, the newer one. Only memory items can lose. Instructions and
repository truth are never marked stale by anything below them.

**`explicit_supersession`** — a memory whose envelope carries `superseded_by` is
marked stale regardless of topic.

## The blind spot, stated plainly

A memory that contradicts repository truth **while sharing no `topic` key is not
detected.** Free-text contradiction detection is not something these rules can do
reliably, and claiming otherwise would be the exact defect this codebase is
written to catch.

What happens instead:

- Both items are injected, ordered by precedence, so the authoritative one is
  read first.
- The memory is rendered with its confidence and age so a reader can weigh it.
- `ConflictReport.unchecked` counts every memory no rule could examine, and
  `summary()` prints it: `"1 of 2 memories checked for conflicts, 1 conflict
  found, 1 unchecked"`.

**Failure direction:** undetected contradiction. The context contains a wrong
historical claim next to the right current one, ordered correctly but not
flagged. That is why the count is printed rather than assumed to be zero.

**Mitigation:** supply `--topic` whenever a memory makes a claim the repository
could later contradict. A memory with a topic is checkable; one without is not.

## Degraded builds

A source that errored produces no items, which is not the same as a source with
nothing to say. `BuildReport.degraded` is set whenever any source reported an
error, and the error text is carried in `source_errors`.

Without that flag a dead Mem0 server looks exactly like an empty memory store —
the same output, the same silence, and a completely different meaning.
