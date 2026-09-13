# Prompt examples

How to get the `context-memory` skill to do the right thing from any repository,
written as the things you would actually type.

The skill triggers on intent, not on a command name. You rarely need `/context-memory`
— asking the question is usually enough.

Two rules shape every example below:

- **Reads are automatic and cover two scopes**: this repository *and* the shared
  scope. You never choose.
- **Writes are never guessed.** Claude must ask you `repo` or `shared` before
  storing. If it stores without asking, that is a bug — say so.

---

## Starting work in a repository

> **What do we already know about this repo?**

> I'm picking up the payments service again. Anything worth knowing before I start?

> Before you touch the billing module, check what we've learned about it.

Claude searches both scopes and reports what it found. From a repo with nothing
stored, expect it to say so rather than inventing relevance — and to name the two
things that silently cause an empty result (wrong identity, wrong repo key).

## Asking about something specific

> Have we hit anything like this before? The build fails only on CI.

> What did we decide about storing uploads?

> Why is the node version pinned here?

## Building a context before a hard task

> Build me a context for adding a graph store to the self-hosted stack.

> I'm about to refactor `server/main.py`. Pull together what matters, report only.

> Give me the context for this task but cap it at 4000 tokens.

The report is the useful part: `42 of 492 candidates injected (42 duplicate, 408
over budget)`. If the context feels thin, that line explains it.

---

## Storing — and the question you should be asked

> **Remember this:** the payments module needs two approvals on every PR.

Claude should come back with something like:

> Is that true of *this repository*, or true *everywhere*?

Answer `repo` or `shared`. Getting it wrong is quiet: a repository fact stored as
shared will surface on unrelated projects as if it were universal.

### Repository-scoped — most things

> Remember that repo alpha pins the build to node 20 because node 22 breaks the native addon. Repo scope.

> Store this for this project: uploads go to S3, not the database, after the 2GB row incident.

> Note for this repo only — the integration tests need the VPN.

### Shared — engineering lessons that travel

> Remember this everywhere: piping a build into a pager replaces the exit code with the pager's, so a failed build reports success.

> Shared scope: antivirus HTTPS scanning re-signs certificates, so package managers fail with CERTIFICATE_VERIFY_FAILED until the root is trusted.

> This one's general, not project-specific — FastAPI ignores query params that aren't in the endpoint signature.

**The test:** *would this still be true in a different codebase?* Yes → shared.
No → repo.

### Give it a topic when it could later be contradicted

> Remember, repo scope, topic `server.graph_store`: AGENTS.md documents a Neo4j service that docker-compose.yaml doesn't define.

A memory with a `topic` can be checked against the repository later and marked
stale if the code moves on. One without a topic cannot be — the build report
counts it as `unchecked`, not as agreement.

---

## When the server is down

Storing while the stack is down **does not fail and does not lose the memory.**
It queues and warns:

```
WARNING: the Mem0 server is unreachable. The memory was NOT stored.
  reason:  [WinError 10061] ...
  queued:  ...\context-memory\spool\20260913T161004-e30ac5d5.json
  pending: 1 write(s) waiting in ...\context-memory\spool
  Start the stack, then replay with:
    python -m src.scripts.memory_flush
```

One spool serves every repository, the shared scope and every user on the
machine — a per-repo queue would strand a memory in whichever checkout you
happened to be in.

Then:

> Start the stack and replay whatever got queued while it was down.

> What's sitting in the memory queue?

> Flush the queue.

The queued time is preserved, so a memory replayed a week later does not rank as
if it were written today.

`& $ctx health` always prints the pending count. A queued write is not stored,
and nothing else in the system will mention it.

---

## Lifecycle

Everything enters as `candidate`. Promotion is deliberate.

> That memory about the node version has proved right several times — promote it to durable.

> Mark the Neo4j one stale, the compose file has changed.

> That's been replaced by the new decision — supersede it with `<new id>`.

`superseded` is terminal and always needs a pointer to the replacement.

## Harvesting from recent work

> Look at the last 5 commits and tell me what's worth remembering.

> Extract memories from this branch, but don't store anything yet.

> Now store those as candidates.

Ordinary commits are deliberately ignored — they are already in git. Only a
breaking change, or a fix that states its cause, becomes a candidate.

---

## Working across repositories

Each repository gets its own scope key, derived from the git remote — so two
clones of one project in differently named folders share one scope, and two
different projects never mix.

From `repo-alpha`:

> What do we know about this repo?

Returns repo-alpha's memories plus the shared ones. Never repo-beta's.

From `repo-beta`, the same prompt returns repo-beta's plus the same shared ones.

> Is this something we learned here, or is it general?

is the question worth asking yourself before every write.

---

## Checking it is working

> Is the memory server up?

> Which repo scope am I in right now?

> How many memories are queued?

All three come from `& $ctx health`, which prints the checkout, API status and
URL, the repository scope key, the identity and the spool count. It is the first
thing to run when a search comes back empty.

---

## When a result looks wrong

> That search returned nothing — is the server actually up, or am I in the wrong scope?

> Why did it return that? It's not relevant.

Worth knowing: similarity search **always** returns nearest neighbours. A query
matching nothing still comes back with rows — measured, a nonsense query scored
0.50–0.55 against real memories at 0.65–0.78. A result is not evidence of a
match; its score is.

> That memory contradicts the code now.

> Why is this one marked STALE?

A memory that loses to current repository truth is marked stale and still shown,
annotated with what overruled it — never silently dropped, never injected as
though it agreed.

---

## Things it should refuse

If Claude does any of these, it is a bug:

| Should not | Why |
|---|---|
| Store without asking repo vs shared | The wrong scope fails silently, forever |
| Store every observation | Memories enter as `candidate` for a reason |
| Copy `CLAUDE.md` rules into memory | Policy stays in `CLAUDE.md`; memory is what was *learned* |
| Report a count without its denominator | `24 injected` hides the 63 dropped |
| Claim "no conflicts" without the unchecked count | Undetected is not the same as absent |
| Silently lose a write when the server is down | It queues, warns, and tells you how to replay |
