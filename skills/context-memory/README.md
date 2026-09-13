# context-memory

A Claude Code skill for **context selection**: deciding what actually goes into
an agent's context, from four independently-sourced layers, rather than
retrieving as much as possible.

| Layer | Answers |
|---|---|
| Instructions | what the agent must or must not do |
| Current state | what is happening right now |
| Repository | what the repo currently is |
| Memory | what experience says (self-hosted Mem0) |

The pipeline is collect → rank → deduplicate → resolve conflicts → budget →
compile, and every stage reports what it dropped and why.

Implementation lives in [`src/context/`](../../src/context) and
[`src/memory/`](../../src/memory); tests in
[`tests/context_memory/`](../../tests/context_memory).

## Design commitments

- Instructions are policy: pinned, never trimmed by the budget. If they do not
  fit, the build raises.
- Historical memory never silently overrides current repository truth. A memory
  that loses is marked stale and shown with what overruled it.
- Every count is printed with its denominator.
- Conflict detection has a stated blind spot, and the number of unchecked
  memories is reported rather than assumed to be zero.
- The scoring weights are documented as unmeasured until the evaluation harness
  measures them.

## Quick start

```bash
python -m src.scripts.context_build --task "..." --report-only
python -m src.scripts.memory_store --scope repository --key <repo> --kind discovery --text "..."
python -m src.scripts.memory_search --scope repository --key <repo> --query "..."
```

See [SKILL.md](SKILL.md) for the full contract and
[references/](references) for policy detail.

Licensed Apache-2.0, matching the rest of this repository.
