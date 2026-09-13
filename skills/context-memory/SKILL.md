---
name: context-memory
description: >
  Select what goes into an agent's context from four layers - instructions,
  repository truth, current task state, and long-term Mem0 memory - then rank,
  deduplicate, resolve conflicts and budget it.
  TRIGGER when: deciding what context to give an agent or subagent for a task;
  storing a decision, discovery, lesson or failure for later; asking what is
  already remembered about a repository or topic; promoting or retiring a
  memory; investigating why a retrieved memory disagrees with the code.
  DO NOT TRIGGER when: the question is answerable by reading one known file;
  the user wants the Mem0 SDK itself (use the `mem0` skill); the task is about
  running or operating the Mem0 server rather than using it as a context source.
---

# Context Memory

Mem0 is the long-term memory layer. It is **not** the context-management
solution. This skill is the layer above it, and its job is **selection**, not
accumulation: the goal is the smallest context that answers the task, not the
largest one that fits.

## The four layers, and what each is for

| Layer | Answers | Examples |
|---|---|---|
| Instructions | what the agent MUST or MUST NOT do | `CLAUDE.md`, `AGENTS.md`, `.claude/rules/*.md`, `SKILL.md` |
| Current state | what is happening right now | branch, working tree, diff, recent commits, test output |
| Repository | what the repo currently is | `README.md`, `docs/`, manifests, the source itself |
| Memory | what experience says | decisions, discoveries, debugging lessons, past failures |

## Non-negotiables

1. **Instructions are policy, not memory.** They are pinned and never trimmed by
   the budget. If policy does not fit, that is an error to surface, not
   something to silently cut.
2. **Historical memory must never silently override current repository truth.**
   Precedence is instructions → current state → repository → memory. A memory
   that loses is marked stale and still shown, annotated with what overruled it -
   never deleted, never injected as if it agreed.
3. **Do not copy `CLAUDE.md` into Mem0.** The split: if forgetting it would make
   the agent break a rule, it stays in `CLAUDE.md`; if forgetting it would only
   make the agent less informed, it is a Mem0 candidate. See
   [memory-policy.md](references/memory-policy.md).
4. **Do not store every observation.** Memories enter as `candidate`. Promotion
   to `durable` is deliberate.
5. **Never report a retrieval number without its denominator.** "24 of 87
   injected" is the honest form; "24 injected" hides the 63 that were dropped.
6. **Conflict detection has a named blind spot.** A memory contradicting the repo
   while sharing no `topic` key is not detected. The build report counts those as
   `unchecked`. Read the count; do not assume zero conflicts means agreement.
   See [context-precedence.md](references/context-precedence.md).

## Scopes

`global`, `user`, `project`, `repository`, `branch`, `session`, `task` — broad to
narrow. Queries run narrow first, so the most specific memory is seen first.
**Never store repository-specific facts at `global` scope.**

## Building a context

```bash
python -m src.scripts.context_build \
  --task "add graph memory to the self-hosted stack" \
  --files server/docker-compose.yaml \
  --repository memory-optimization \
  --max-tokens 8000
```

Add `--report-only` to see the accounting without the context itself, and
`--json` for machine use. It runs without a Mem0 server; the report then shows
the memory layer absent rather than pretending it was empty.

## Storing a memory

```bash
python -m src.scripts.memory_store \
  --scope repository --key memory-optimization \
  --kind discovery --topic server.default_provider \
  --text "The self-hosted compose stack ships pgvector only; there is no Neo4j service."
```

`--kind` is one of `decision`, `discovery`, `lesson`, `convention`, `failure`,
`incident`, `note`. `--topic` is the stable key conflict detection uses — supply
it whenever the memory makes a claim that the repository could later contradict.

## Searching and lifecycle

```bash
python -m src.scripts.memory_search --scope repository --key memory-optimization --query "embedding provider"
python -m src.scripts.memory_promote --id <id> --to durable
python -m src.scripts.memory_promote --id <id> --to superseded --replacement <new id>
```

Lifecycle is `candidate → durable → stale → superseded`, and the transition
table is enforced: illegal moves raise before anything is written. `superseded`
is terminal and always requires a pointer to the replacement.

## Environment

```
MEM0_API_URL     http://localhost:8888
MEM0_API_KEY     ADMIN_API_KEY from server/.env
MEM0_USER        who the memory belongs to
MEM0_REPOSITORY  optional repository scope key
```

## References

- [memory-policy.md](references/memory-policy.md) — what belongs in Mem0 vs `CLAUDE.md`, the promotion model, scopes
- [retrieval-policy.md](references/retrieval-policy.md) — the scoring signals and why the weights are still unmeasured
- [context-precedence.md](references/context-precedence.md) — the ladder, conflict rules, and their blind spot
- [evaluation.md](references/evaluation.md) — metrics, and why retrieval quality is not answer quality
