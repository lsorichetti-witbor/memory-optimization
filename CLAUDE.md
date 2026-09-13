AGENTS.md

# Context Memory (this fork)

This fork adds a **Context Manager** on top of self-hosted Mem0. Mem0 is the
long-term memory layer; the Context Manager is the layer above it that decides
what actually reaches an agent's context from four sources — instructions,
repository truth, current task state, and memory — then ranks, deduplicates,
resolves conflicts and budgets them.

Nothing above this line changes: `AGENTS.md` remains the contract for the
upstream packages (`mem0/`, `mem0-ts/`, `cli/`, `server/`, `docs/`).

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
| `src/memory/` | `MemoryProvider` interface + `Mem0Provider`, scopes, lifecycle, extraction |
| `src/context/` | Four sources, ranker, dedupe, conflicts, budget, compiler, manager |
| `src/evaluation/` | Retrieval benchmark: five arms, metrics, seeded dataset |
| `src/scripts/` | CLI entry points |
| `skills/context-memory/` | The skill and its `ctx.ps1` launcher |
| `tests/context_memory/` | 229 tests; 2 require a live server |
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
5. **Writes ask for their scope.** `repository` vs `global` is never guessed — a
   repository fact written to the global scope surfaces on unrelated projects as
   universal truth, and nothing about the write looks wrong at the time.
6. **The scoring weights are unmeasured.** They default to 1.0 and are
   documented as placeholders. Change them when the evaluation harness says to,
   not because one task looked wrong.

## Verifying a change

```powershell
.\.venv\Scripts\python.exe -m pytest tests/context_memory -q
```

229 tests. The 2 integration tests need `MEM0_API_URL` / `MEM0_API_KEY` /
`MEM0_USER` set — **a skip there is a failure of the check, not a pass.**

The embedder and the LLM are separate legs: tests default to `infer=False` and
exercise only the embedder. Verify the LLM path explicitly with
`memory_store --infer` before claiming the provider works.

To measure whether selection is still earning its place:

```powershell
& "$env:USERPROFILE\.claude\skills\context-memory\scripts\ctx.ps1" eval
```

Retrieval-only, on 8 repo-local questions. It cannot support a general claim
about retrieval quality — see `skills/context-memory/references/evaluation.md`.
