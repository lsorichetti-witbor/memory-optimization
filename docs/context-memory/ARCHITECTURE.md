# Architecture

What this fork adds on top of Mem0, and why each piece is shaped the way it is.

Mem0 is the long-term memory store. This fork adds two things above it:

- a **Context Manager** that decides what actually reaches an agent's context,
  from four competing sources, under a token budget
- a **durability layer** that makes a memory write survive the store being
  unable to accept it

Both exist for the same reason, from opposite directions: an agent is only as
good as what is in front of it, and a memory that was never stored is
indistinguishable from one that was never written.

Related documents: [SETUP.md](SETUP.md) to run it,
[DURABILITY.md](DURABILITY.md) for the write path in depth,
[../../CLAUDE.md](../../CLAUDE.md) for the rules that apply when changing it.

---

## The whole system

```
 ┌─────────────────────────────────────────────────────────────────────────┐
 │ CALLERS                                                                 │
 │   ctx.ps1 (the skill)   ·   agents   ·   dashboard   ·   future MCP     │
 └───────────────┬─────────────────────────────────────────────────────────┘
                 │
 ╔═══════════════▼═════════════════════════════════════════════════════════╗
 ║ CLIENT  (Python, src/)                                                  ║
 ║                                                                         ║
 ║  ┌──────────────────────────┐      ┌────────────────────────────────┐   ║
 ║  │ CONTEXT MANAGER          │      │ MEMORY LAYER                   │   ║
 ║  │ src/context/             │      │ src/memory/                    │   ║
 ║  │                          │      │                                │   ║
 ║  │  4 sources               │      │  DurableProvider  ← writes     │   ║
 ║  │   instructions           │      │    ↓ write-ahead               │   ║
 ║  │   repository             │      │  Spool (disk)                  │   ║
 ║  │   current_state          │      │    ↓                           │   ║
 ║  │   memory ────────────────┼──────┼→ Mem0Provider (HTTP)           │   ║
 ║  │      ↓                   │      │                                │   ║
 ║  │  rank → dedupe →         │      │  scopes · lifecycle ·          │   ║
 ║  │  conflicts → budget →    │      │  envelope · errors             │   ║
 ║  │  compile                 │      │                                │   ║
 ║  └──────────┬───────────────┘      └───────────────┬────────────────┘   ║
 ╚═════════════│══════════════════════════════════════│════════════════════╝
               │ context text                         │ HTTP
               ▼                                      ▼
        the agent's prompt        ╔═════════════════════════════════════════╗
                                  ║ SERVER  (FastAPI, server/)              ║
                                  ║                                         ║
                                  ║  POST /memories                         ║
                                  ║    ├── embed inline → 200               ║
                                  ║    └── embedder refused → 202           ║
                                  ║             ↓                           ║
                                  ║      pending_memories  (the queue)      ║
                                  ║             ↓                           ║
                                  ║      pending_worker ── circuit breaker  ║
                                  ║             ↓                           ║
                                  ║      errors.py (classifier)             ║
                                  ║                                         ║
                                  ║  dashboard (Next.js) · /dashboard/queue ║
                                  ╚══════════════┬═══════════════┬══════════╝
                                                 │               │
                                    ┌────────────▼───┐   ┌───────▼─────────┐
                                    │ Gemini         │   │ Postgres        │
                                    │  LLM (extract) │   │  memories       │
                                    │  embeddings    │   │   (pgvector)    │
                                    └────────────────┘   │  mem0_app       │
                                                         │   pending_...   │
                                                         └─────────────────┘
```

Two Postgres databases, deliberately: `postgres` holds mem0's `memories` vector
table, `mem0_app` holds this fork's own tables. An unembedded memory therefore
cannot leak into search — see [Why two tables](#why-two-tables).

---

## 1. Scopes: where a memory lives

Seven conceptual scopes mapped onto the three identifiers Mem0 can filter on.

| Scope | Mem0 identifier | Example |
|---|---|---|
| `global` | `agent_id = "global"` | knowledge that applies everywhere |
| `user` | `agent_id = "user:<name>"` | one person's preferences |
| `project` | `agent_id = "project:<key>"` | a group of repositories |
| `repo` | `agent_id = "repo:<key>"` | this checkout |
| `branch` | `run_id = "branch:<key>"` | + the repo's `agent_id` |
| `session` | `run_id = "session:<key>"` | + the repo's `agent_id` |
| `task` | `run_id = "task:<key>"` | + the repo's `agent_id` |

The scope also lands in `metadata.scope` / `metadata.scope_key`, so the
server-side filter and the identifier say the same word about the same memory.

Three rules the vocabulary enforces:

**Closed.** `Scope.parse` raises on an unknown word. A typo'd scope would write
memories no query ever matches, and "no results" is indistinguishable from
"nothing stored yet".

**One name per scope.** The scope value, the `agent_id` prefix and the CLI flag
are all `repo`, never `repo` in one place and `repository` in another. This was
learned the hard way: a launcher translating between the two kept passing a word
the CLI had stopped accepting, and every repo write failed while the launcher
source looked untouched.

**Every scope gets an `agent_id`.** Without one, a global memory carried only a
`user_id`: unfilterable server-side, invisible as its own entity in the
dashboard, and unaddressable by bulk delete without hitting every other scope
that user owned.

Reads widen narrow→broad (`task → … → global`); a scope with no key is skipped
rather than queried with a placeholder, because a query against a made-up key
silently returns nothing. **Writes default to `repo`** — a repo fact written to
`global` surfaces on unrelated projects as universal truth, and nothing about
the write looks wrong at the time.

## 2. Lifecycle

`candidate → durable → stale → superseded`. Memories enter as `candidate`;
promotion is a separate deliberate step (`memory_promote`), because not every
observation should become permanent. `stale` and `superseded` are set by
conflict resolution, and a losing memory is still shown — annotated with what
overruled it — rather than hidden.

---

## 3. The Context Manager

Four sources compete for a token budget. The pipeline is
**collect → rank → dedupe → conflicts → rank again → budget → compile**, and
every stage reports what it dropped.

```
instructions ─┐
repository ───┤
current_state ┼→ collect ─→ rank ─→ dedupe ─→ conflicts ─→ rank ─→ budget ─→ compile
memory ───────┘                                    │
                                                   └─ staleness changed, so the
                                                      scores that depended on it
                                                      are re-computed
```

**Precedence, highest authority first:** instructions → current state →
repository → memory. The rule behind it: *historical memory must never silently
override current repository truth.*

**Sources**

| Source | Reads | Note |
|---|---|---|
| `instructions` | CLAUDE.md / AGENTS.md on the task's path | pinned, never trimmed |
| `repository` | code and docs, chunked | ranked candidates |
| `current_state` | git status, diff, branch | what is true right now |
| `memory` | Mem0, across the selector's scopes | the only network source |

Instruction files **on the task's own path** are policy: pinned, and if they do
not fit the build raises rather than quietly dropping them. Files elsewhere in
the tree are ranked candidates, not policy.

**Ranking** combines eight signals — `relevance`, `task_scope_match`,
`project_scope_match`, `confidence`, `recency`, `importance`, minus `redundancy`
and `staleness`. Every weight defaults to **1.0 and is documented as an
unmeasured placeholder.** They are changed when the evaluation harness says to,
not because one task looked wrong.

Relevance blends embedding similarity with BM25. The BM25 leg uses an
**absolute** transform, `1 - exp(-raw)`, not min-max: min-max would rescale
lexical scores onto a different basis from the embedding scores and make the two
incomparable.

**Conflict detection has a blind spot and reports its size.** Two memories that
contradict each other but share no `topic` cannot be compared, so those are
counted as `unchecked` rather than implied to agree.

**Every count carries its denominator** — `24 of 87 injected`, never
`24 injected`.

---

## 4. The write path

This is where most of the design effort went, because the failure is silent.

> **Reads may fail fast; writes may not fail at all.** A search that fails is
> retried by whoever ran it, one second later, at no cost. A write that fails
> takes an observation with it and nobody notices, because what was lost never
> existed anywhere else.

Two queues, covering two different failures.

```
any caller
    │
    ▼  DurableProvider.add()
 ┌──────────────────────────────────────────────────────────┐
 │ 1. write-ahead: spool entry on disk + idempotency key     │  covers:
 │    (before the request, so Ctrl+C mid-POST survives)      │  "could not
 └──────────────────────┬───────────────────────────────────┘   hand it over"
                        ▼
                  POST /memories
                        │
        ┌───────────────┴────────────────┐
        ▼                                ▼
   embed inline OK                embedder refused
        │                                │
      200 ──→ delete spool entry         ▼
                              ┌──────────────────────────────┐  covers:
                              │ 2. pending_memories row       │  "took it, could
                              │    202 → client deletes its   │   not embed it"
                              │    spool entry and is done    │
                              └───────────┬──────────────────┘
                                          ▼
                                   pending_worker
                              claim → embed → insert → delete
```

**Inline first** is why nothing broke when the server queue was added: a caller
that never sees a failure never sees a difference. The 502 simply became a 202.

### Keep-or-drop: two opposite defaults, on purpose

| | Question | Default | Because being wrong costs |
|---|---|---|---|
| **Client spool** | keep this write? | **keep** | a lost memory vs. a file someone deletes |
| **Server breaker** | park the whole queue? | **do not park** | every other row stalls vs. one wasted request |

The client drops a write only when the server calls it *malformed*
(`provider_bad_request`, 400/422). An error nobody has seen before is kept. This
replaced a string-match on the error message that discarded a plain 500, a
`RemoteProtocolError`, a proxy's HTML error page and a bare 429 — all ordinary
embedding failures.

### Client spool (`src/memory/spool.py`)

One shared directory per machine, across every repository, scope and user — a
per-repo spool would strand a memory in whichever checkout you were in. Entries
keep their **original** `created_at`, because recency is a ranking signal and a
replayed month-old memory must not rank as brand new.

Nothing is deleted until the server confirms it stored. Failures accumulate
`attempts` with exponential backoff; after 10 the entry moves to `dead/` and is
**reported, never deleted**.

**Triggers:** a successful write drains the backlog (a success is direct
evidence that whatever blocked the queue has stopped), and `ctx flush` for a
human. Scoped to the caller's own user, capped at 25 per write, replayed through
the raw provider so it cannot re-enqueue what it is draining.

### Server queue (`server/pending*.py`)

Four states, and `pending` vs `error` is the distinction the design turns on:

| State | Meaning | Claimable |
|---|---|---|
| `pending` | nobody has tried yet | yes, once `next_attempt_at` passes |
| `embedding` | a worker holds it now (`claimed_by` + `lease_until`) | only if the lease expired or is implausible |
| `error` | the provider refused | yes, once its backoff passes |
| `dead` | out of attempts (12) | **no** — waits for a person, never deleted |

**Why the split:** when any embedding succeeds, every `error` row is swept back
to `pending` and its backoff discarded. That success is fresh evidence the
outage is over, and the delays were guesses about a condition that has changed.
`dead` rows are left alone — they failed for a reason a working provider does
not explain.

**Worker triggers:** on enqueue · on startup · on the backoff timer · on any
success.

### Never processing the same embedding twice

Four distinct collisions, four answers:

| Collision | Answer |
|---|---|
| Two workers, one row | `FOR UPDATE SKIP LOCKED` |
| A worker dies holding rows | `lease_until` expiry; an implausibly far lease is treated as expired |
| Two users, identical text | reuse the stored vector by `content_hash` — `add` task-type only, since Gemini embeds per task type |
| The same request twice | `idempotency_key` UNIQUE; a retry returns the same row |
| Crash between insert and dequeue | the worker checks `already_stored` by key before writing |

### Circuit breaker

A queue of N rows must not discover one outage N times. The free tier is 1,000
embeddings a day; letting each row find out for itself that the quota is gone
spends the next day's allowance on failures, so the queue could never drain.

A provider-wide failure parks everything and lets **one** probe through when the
window opens. A daily quota waits an hour — not the `retryDelay: 2s` Google
returns even on a daily exhaustion. A malformed row does not trip it.

The breaker is in-memory, so a restart forgets it. Survivable rather than ideal:
each row's `next_attempt_at` is in the database, so a fresh breaker costs at most
one probe per restart, never a retry storm.

### Ordering

The queue drains by **when the memory was written** (`source_created_at`, taken
from the metadata envelope), not when it arrived. A client replaying a spool
after an outage sends everything at once, hours late; ordering on arrival would
drain them in the wrong order and rank them as newly written.

Ordering is applied twice — on the claim *and* on the fetch of the claimed rows —
because `SELECT … WHERE id IN (…)` returns rows in whatever order Postgres
likes, so a batch chosen oldest-first was being embedded in an arbitrary one.

### Why two tables

An unembedded memory lives in `pending_memories`, **never** as a null-vector row
in `memories`. A row there that exists but cannot be found would make search
silently incomplete, and `memories.vector` is nullable so nothing would catch
it. Keeping them apart makes an unembedded memory *structurally invisible* to
search rather than *invisibly missing* from it, and the count is surfaced as
`not_searchable`.

---

## 5. Error classification

`server/errors.py` turns a provider exception into a typed `code` returned in
the response body. Everything downstream reads the code: whether to queue,
whether to park, what to tell the user.

`provider_quota_exhausted` · `provider_rate_limited` · `provider_unavailable` ·
`provider_timeout` · `provider_auth_failed` · `provider_bad_request` ·
`datastore_unavailable` · `vector_store_unavailable` · `unknown`

The HTTP status is read from `status_code` **or** `code`, because google-genai
puts it on `code` and reserves `status` for the string enum. Reading only
`status_code` classified every Gemini failure as `unknown`, which is how a spent
daily quota reached callers as a bare `502: Upstream provider error.` with the
reason visible only in the container log.

A `429` carrying `QuotaFailure` violations names the metric, the limit and the
model. Only a **per-day** `quotaId` says the allowance is spent; any other window
says the response did not state one, rather than guessing — both guesses cost
something.

---

## 6. Visibility

Nothing about an incomplete store is allowed to look complete.

| Surface | Shows |
|---|---|
| `ctx health` | client spool queued/dead, server queue by state, breaker status. **Exits non-zero while anything is unstored or unsearchable** |
| `ctx store` / `ctx extract` | the backlog after running, silent when empty |
| `ctx flush --list` | queued entries with attempt counts, and `dead/` |
| `/dashboard/queue` | counts by state, parked banner with next probe, per-row attempts and error, detail panel with copyable error, Refresh / Send all |
| `GET /memories/pending` | the same, as JSON |

A row in `embedding` past its lease renders as **stalled** — the state alone
would say the opposite of the truth, and "looks like progress" is the worst way
to be stuck.

---

## 7. Evaluation

Two harnesses, both retrieval-only, both self-reported, and both say so on every
run.

**Retrieval** (`src/evaluation/`) runs five arms — `full`, `static-docs-only`,
`memory-only`, `static+memory`, `static+memory+state` — over a seeded dataset.
`full` is the information ceiling, not a competitor: if the full Context Manager
does not beat it on tokens at comparable recall, selection is not earning its
place and the report should say so.

It refuses to score an outage. A source that errored produced no items, and
averaging that as 0.000 recall reports a quota exhaustion as a ranking
regression — so `run_arm` raises `SourceUnavailable` and the CLI exits 2, making
"could not measure" distinguishable from "measured, and it was bad".

Ground truth keys on `topic`, not memory id: an id is a fresh uuid on every
re-seed, so an id-keyed dataset would score zero forever and look like poor
retrieval rather than a broken key.

**Rule coverage** (`instruction_eval`) measures `present` vs `selected`
separately, because a rule that was never a candidate and a rule that was ranked
out are different failures.

---

## 8. Testing

| Suite | Covers | Needs |
|---|---|---|
| `tests/context_memory/` (345) | scopes, lifecycle, ranking, dedupe, conflicts, budget, spool, durability, error classification, queue policy | nothing |
| `scripts/test-pending-queue.py` (25) | claiming, leases, states, ordering, aborted batches, button safety, idempotency | Postgres |
| `scripts/test-offline-spool.ps1` (23) | multi-user × multi-repo offline queueing and replay | the stack |
| 2 integration tests | a real round trip | a live server |

The split is deliberate. `FOR UPDATE SKIP LOCKED`, lease expiry and ordering are
Postgres behaviours — a fake would only prove the fake works — so the policy
that can be tested without a database lives in `pending_policy.py` and the rest
runs against the real one. Neither DB suite requests an embedding, so both run
with the provider down.

A skip in the integration tests **is a failure of the check, not a pass.**

---

## 9. Known limits

Stated with their failure direction, because an "acceptable limitation" whose
direction is unnamed is a defect filed under the wrong label.

- **The scoring weights are unmeasured.** All 1.0. Fails toward mediocre
  ranking, visibly, not toward wrong answers.
- **The breaker is in-memory.** A restart forgets it; costs at most one probe.
- **Queue ordering is best-effort across workers.** Batches are claimed and
  processed oldest-first, but two workers can finish out of order.
- **Conflict detection cannot see a contradiction with no shared `topic`.**
  Counted as `unchecked`, never implied to agree.
- **The retrieval eval is 8 repo-local questions.** It cannot support a general
  claim about retrieval quality, and the report says so.
- **A queued write replaying to success is unverified live** at the time of
  writing — the Gemini quota has been spent throughout. Every failure path is
  verified against the real failure; the success leg is unit-tested only.
- **No MCP server yet.** The skill and the CLIs are the only entry points;
  non-Python clients get the server-side queue but not the client spool.
