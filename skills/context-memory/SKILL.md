---
name: context-memory
description: >
  Select what goes into an agent's context from four layers - instructions,
  repository truth, current task state, and long-term Mem0 memory - then rank,
  deduplicate, resolve conflicts and budget it. Also stores and retrieves
  durable engineering memory across sessions and repositories.
  TRIGGER when: starting work in a repo and wanting to know what is already
  known about it; deciding what context to give an agent or subagent; storing a
  decision, discovery, lesson or failure worth keeping; asking what was learned
  before about a topic; promoting or retiring a memory; investigating why a
  retrieved memory disagrees with the code.
  DO NOT TRIGGER when: the question is answerable by reading one known file; the
  user wants the Mem0 SDK itself (use the `mem0` skill); the task is operating
  the Mem0 server rather than using it as a context source.
---

# Context Memory

Mem0 is the long-term memory layer. It is **not** the context-management
solution. This skill is the layer above it, and its job is **selection**, not
accumulation: the smallest context that answers the task, not the largest that
fits.

## The one command

Everything runs through one launcher, from any repository:

```powershell
$ctx = "$env:USERPROFILE\.claude\skills\context-memory\scripts\ctx.ps1"
& $ctx health
```

It resolves the checkout, reads the API port, key and identity from
`server/.env`, and derives the repository scope key from the git remote. You
never pass connection details.

If `health` shows `API: down`, start the stack first — the command it prints.

## Scope rules — the part to get right

**Reading is automatic: this repository AND the global scope, every time.**
A lesson worth remembering everywhere is useless if it only surfaces where it
was learned, so `search` queries both and labels each result set.

**Writing defaults to THIS repository.** `store` with no `-Scope` writes to the
repo you are in. Use it freely — that is the common case and it needs no
ceremony.

**`-Scope global` is opt-in, and only when the user asks for it.** Words like
"remember this everywhere", "globally", "global", "for all projects", or a fact
that is plainly about a tool rather than this codebase.

| Scope | When | Example |
|---|---|---|
| default (`repository`) | anything about *this codebase* | "server/AGENTS.md documents a Neo4j service that docker-compose.yaml does not define" |
| `-Scope global` | the user asked for it, or it plainly holds anywhere | "FastAPI silently ignores query parameters not in the endpoint signature" |

The two mistakes are not symmetric, which is why the default is what it is:

- A general lesson stuck in one repo is **missed elsewhere** — annoying, and
  fixable later by re-storing it to `global`.
- A repo-specific fact written to `global` **surfaces on unrelated projects as
  universal truth**, and nothing about the write looks wrong at the time.

So the default fails toward under-sharing. When unsure, store to the repo and
say you did; do not reach for `global` to be safe, because it is the unsafe one.
`store -Scope global` prints a warning naming what it is about to do.

## Operations

### Check the connection

```powershell
& $ctx health
```

Prints the checkout, API status and URL, the repository scope key it derived,
and the identity. Check this first whenever a search comes back empty.

### Build a context for a task

```powershell
& $ctx build -Task "add a graph store to the self-hosted stack" -ReportOnly
& $ctx build -Task "why does the pgvector column width matter" -Files server/main.py
```

`-ReportOnly` shows the accounting without the context. Add `-Json` to pipe it,
`-MaxTokens` to change the budget, `-NoMemory` to use static layers only.

The report always prints denominators, e.g. `42 of 492 candidates injected
(42 duplicate, 408 over budget)`. Read them: a thin context is explained there.

### Search what is already known

```powershell
& $ctx search -Query "embedding dimensions pgvector"
& $ctx search -Query "line endings" -TopK 3
```

Always searches this repo **and** `global`. If both come back empty it says so and
names the two things that silently cause it — a wrong identity or a wrong repo
key — because an empty result and a wrong lookup look identical.

**Read the score.** Similarity search always returns nearest neighbours, so a
query matching nothing still comes back with rows: measured, a nonsense query
scored 0.50–0.55 against real memories scoring 0.65–0.78. A result is not
evidence of a match; its score is. Pass `-Threshold 0.6` to impose a floor, but
pick the number from your own data rather than inheriting one.

### Store a memory

```powershell
# this repository - the default, no -Scope needed
& $ctx store -Kind discovery -Topic server.graph_store `
  -Text "server/AGENTS.md documents Neo4j on ports 8474/8687 but docker-compose.yaml defines no such service."

# everywhere - only when the user asked for it
& $ctx store -Scope global -Kind lesson -Topic tooling.fastapi_query_params `
  -Text "FastAPI silently ignores query parameters not in the endpoint signature, so an unsupported filter looks like it works and does nothing."
```

`-Kind`: `decision`, `discovery`, `lesson`, `convention`, `failure`, `incident`,
`note`.

`-Topic` is the stable key conflict detection uses. **Supply it whenever the
memory claims something the repository could later contradict** — a memory
without a topic cannot be checked, and the build report counts it as
`unchecked` rather than as agreement.

Optional: `-Tag`, `-Confidence`, `-Importance` (0..1).

### Harvest memories from recent work

```powershell
& $ctx extract -Since HEAD~5           # review only, writes nothing
& $ctx extract -Since HEAD~5 -Store    # write the survivors as candidates
```

Ordinary commits are deliberately **not** extracted — they are already in git,
and the repository layer reads git live. Only a breaking change, or a fix that
states its cause, becomes a candidate.

Nothing is written without `-Store`, and even then everything lands at
`candidate`. That gap is the review gate; do not collapse it.

### When the server is down

Storing while the stack is down does **not** fail and does **not** lose the
memory. It queues to a spool shared by every repository, scope and user, warns
loudly, and exits 3 (distinct from 1 = bad write, 2 = bad config).

```powershell
& $ctx flush -List     # what is queued, writes nothing
& $ctx flush           # replay oldest-first, delete only on success
```

`& $ctx health` always prints the pending count — a queued write is not stored,
and nothing else will mention it.

The queued time is preserved, so a memory replayed a week later does not rank as
if it were written today. A failed replay keeps its file and exits non-zero.

### Promote or retire a memory

```powershell
& $ctx promote -Id <memory id> -To durable
& $ctx promote -Id <memory id> -To stale
& $ctx promote -Id <old id> -To superseded -Replacement <new id>
```

Lifecycle is `candidate → durable → stale → superseded`, enforced by a
transition table: illegal moves raise before anything is written. `superseded`
is terminal and always needs a pointer to what replaced it.

### Benchmark the selection itself

```powershell
& $ctx eval
```

Seeds known memories, runs five arms, prints a methodology block. Retrieval-only
— it measures what gets selected, not whether an agent answers correctly.

## Non-negotiables

1. **Instructions are policy, not memory.** Instruction files on the task's own
   path are pinned and never trimmed; if that policy does not fit, the build
   raises rather than silently cutting a rule. Instruction files elsewhere in
   the tree are ranked candidates, not policy — in a monorepo
   `cli/node/AGENTS.md` is not policy for a server task.
2. **Historical memory must never silently override current repository truth.**
   Precedence is instructions → current state → repository → memory. A memory
   that loses is marked stale and still shown, annotated with what overruled it.
3. **Do not copy `CLAUDE.md` into Mem0.** If forgetting it would make the agent
   break a rule, it stays in `CLAUDE.md`; if forgetting it would only make the
   agent less informed, it is a Mem0 candidate. See
   [memory-policy.md](references/memory-policy.md).
4. **Do not store every observation.** Memories enter as `candidate`. Promotion
   is deliberate.
5. **Writes go to this repository unless the user asks for `global`.** Do not
   default to `global` to be safe - it is the unsafe direction.
6. **Never report a retrieval number without its denominator.**
7. **Conflict detection has a named blind spot.** A memory contradicting the
   repo while sharing no `topic` is not detected; the report counts those as
   `unchecked`. Zero conflicts is not the same as agreement. See
   [context-precedence.md](references/context-precedence.md).

## References

- [memory-policy.md](references/memory-policy.md) — what belongs in Mem0 vs `CLAUDE.md`, promotion, scopes
- [retrieval-policy.md](references/retrieval-policy.md) — scoring signals, and why the weights are unmeasured
- [context-precedence.md](references/context-precedence.md) — the ladder, conflict rules, the blind spot
- [evaluation.md](references/evaluation.md) — metrics, and why retrieval quality is not answer quality
- [PROMPT_EXAMPLES.md](../../PROMPT_EXAMPLES.md) — what to type, per operation, from any repo
- [OFFLINE-SPOOL-TEST.md](../../docs/context-memory/OFFLINE-SPOOL-TEST.md) — the spool's design, test and limits
