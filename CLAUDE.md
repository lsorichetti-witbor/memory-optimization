AGENTS.md

# Context Memory (this fork)

This fork adds a **Context Manager** on top of self-hosted Mem0. Mem0 is the
long-term memory layer; the Context Manager is the layer above it that decides
what actually reaches an agent's context from four sources — instructions,
repository truth, current task state, and memory — then ranks, deduplicates,
resolves conflicts and budgets them.

Nothing above this line changes: `AGENTS.md` remains the contract for the
upstream packages (`mem0/`, `mem0-ts/`, `cli/`, `server/`, `docs/`).

## How it works

**→ [docs/context-memory/ARCHITECTURE.md](docs/context-memory/ARCHITECTURE.md)**

The whole system: scopes and lifecycle, the four-source context pipeline, both
write queues and why there are two, error classification, what is visible where,
the evaluation harnesses, and the known limits with their failure directions.

## Deploying and running it locally

**→ [docs/context-memory/SETUP.md](docs/context-memory/SETUP.md)**

Full guide: prerequisites, `server/.env`, the optional TLS-interception
certificate, starting the stack, the venv, a verification sequence, installing
the skills, and a troubleshooting section covering every failure actually hit
during the build. Most of those fail quietly, which is why they are written down.

Quick start on a machine that is already set up:

```powershell
.\scripts\stack.ps1 health      # up | down | restart | logs | health | reset
& "$env:USERPROFILE\.claude\skills\context-memory\scripts\ctx.ps1" health
.\.venv\Scripts\python.exe -m pytest tests/context_memory -q
```

`server/Makefile` is not usable on Windows (`lsof`). Use `scripts/stack.ps1` or
`docker compose` directly.

## Layout

| Path | What |
|---|---|
| `src/memory/` | `MemoryProvider` interface + `Mem0Provider`, `DurableProvider`, scopes, lifecycle, spool |
| `src/context/` | Four sources, ranker, dedupe, conflicts, budget, compiler, manager |
| `src/evaluation/` | Retrieval benchmark: five arms, metrics, seeded dataset |
| `src/scripts/` | CLI entry points |
| `skills/context-memory/` | The skill and its `ctx.ps1` launcher |
| `tests/context_memory/` | 348 tests; 2 require a live server |
| `docs/superpowers/plans/` | The implementation plan and its execution log |

## Rules that apply when working on this code

1. **Instructions are policy.** Instruction files on the task's own path are
   pinned and never trimmed by the budget; if they do not fit, the build raises.
   Files elsewhere in the tree are ranked candidates, not policy.
2. **Historical memory must never silently override current repository truth.**
   Precedence: instructions → current state → repository → memory. A memory that
   loses is marked stale and still shown, annotated with what overruled it.
3. **Every count is printed with its denominator.** `24 of 87 injected`, never
   `24 injected`.
4. **Name the silent failure direction.** Conflict detection cannot see a
   contradiction with no shared `topic`, so the report counts those as
   `unchecked` rather than implying agreement. Anything with a blind spot
   reports the size of it.
5. **Writes default to this repository.** `-Scope global` is opt-in and only
   when asked for. The two mistakes are not symmetric: a general lesson stuck in
   one repo is merely missed elsewhere, while a repo fact written to `global`
   surfaces on unrelated projects as universal truth and nothing about the write
   looks wrong at the time.
6. **The scoring weights are unmeasured.** They default to 1.0 and are
   documented as placeholders. Change them when the evaluation harness says to,
   not because one task looked wrong.
7. **Reads may fail fast; writes may not fail at all.** A failed search costs a
   retry. A failed write costs an observation that existed nowhere else. Every
   write goes through `DurableProvider` (`provider_from_env()`), which queues on
   disk *before* the request and deletes only on confirmation. The keep/drop
   default is **keep**: only a write the server calls malformed is dropped, so
   an error nobody has seen before lands on the safe side. See
   [docs/context-memory/DURABILITY.md](docs/context-memory/DURABILITY.md).
   Never write through a raw `Mem0Provider` unless the input survives its own
   failure — `memory_flush` and `memory_backup restore` are the two exceptions,
   and both say why in a docstring.

## Open TODOs

Kept here rather than in Mem0, deliberately. Both of these are about the memory
layer not yet being proven, and storing them *in* that layer would mean trusting
the thing under test to remember what has not been tested — a memory that cannot
be retrieved is indistinguishable from one that was never written. **Move them
into Mem0 once TODO 1 closes, and delete this section then.**

### 1. Five end-to-end checks have never run

`scripts/test-end-to-end.ps1` reports them as **BLOCKED**, not passed, while the
Gemini daily embedding quota is spent:

- alpha sees its own memory
- alpha does **not** see beta's memory
- the global memory is visible from alpha
- the server queue empties
- a queued memory becomes searchable

Every *failure* path is verified against the real failure. The **success** leg of
the write path — a queued memory draining and becoming searchable — is covered by
unit tests with a fake provider only, and has never been observed end to end.

To close it: wait for the quota to reset, then

```powershell
.\scripts\test-end-to-end.ps1        # probes the embedder first; expect 25 of 25
.\scripts\test-offline-spool.ps1     # 13 of 23 today, for the same reason
```

Both should reach full marks. Until they do, treat "writes are never lost" as
verified on the failure side and *assumed* on the success side.

### 2. No MCP server

`ctx.ps1` and the Python CLIs are the only entry points. Every non-Python
caller — the dashboard, an editor, another agent runtime — gets the server-side
embedding queue but **not** the client spool, which is the only thing that
survives being unable to reach the server at all.

An MCP server exposing build / search / store / flush over the same
`DurableProvider` would give those callers the write-ahead spool too. Recorded as
a known limit in
[docs/context-memory/ARCHITECTURE.md](docs/context-memory/ARCHITECTURE.md).
Not started.

## Verifying a change

```powershell
.\.venv\Scripts\python.exe -m pytest tests/context_memory -q
```

348 tests. The 2 integration tests need `MEM0_API_URL` / `MEM0_API_KEY` /
`MEM0_USER` set — **a skip there is a failure of the check, not a pass.**

A 502 now names its own cause, so read the message before suspecting the code:

```
Provider quota exhausted for the day:
generativelanguage.googleapis.com/embed_content_free_tier_requests,
limit 1000 (model gemini-embedding-1.0). Retrying will not help until the
quota resets.
```

The Gemini free tier allows 1,000 embedding requests per day, and a few
wipe/re-seed/benchmark cycles exhaust it. Every write and every search needs an
embedding, so the whole memory layer stops until it resets. A bare
`502: Upstream provider error.` with no reason means the classifier in
`server/errors.py` did not recognise the exception — that is the bug to fix, not
the symptom to work around; `docker compose logs mem0 --since 5m` has the
original traceback.

The embedder and the LLM are separate legs: tests default to `infer=False` and
exercise only the embedder. Verify the LLM path explicitly with
`memory_store --infer` before claiming the provider works.

The server-side embedding queue has its own end-to-end suite. It needs Postgres
(`FOR UPDATE SKIP LOCKED`, lease expiry and ordering are database behaviours, and
a fake would only prove the fake works) and it requests no embeddings, so it runs
with the provider down:

```powershell
docker compose --project-directory server exec -T mem0 python - < scripts/test-pending-queue.py
```

29 checks.

And the whole path a write takes, driven through the launcher from directories
standing in for separate checkouts, as a person or an agent would:

```powershell
.\scripts\test-end-to-end.ps1
```

20 checks: server down, server up with the embedder refusing, the auto-drain,
flush, scope isolation, and visibility. Phases that need a working embedder are
reported as **BLOCKED, never as passed** when it is unavailable.

To measure whether selection is still earning its place:

```powershell
& "$env:USERPROFILE\.claude\skills\context-memory\scripts\ctx.ps1" eval
```

Retrieval-only, on 8 repo-local questions. It cannot support a general claim
about retrieval quality — see `skills/context-memory/references/evaluation.md`.
