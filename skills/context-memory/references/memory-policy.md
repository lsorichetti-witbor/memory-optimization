# Memory policy

What belongs in Mem0, what belongs in `CLAUDE.md`, and how something moves
between them.

## The split

```
CLAUDE.md  =  permanent instruction / policy
Mem0       =  learned or historical knowledge
```

The test:

> If forgetting it could cause the agent to **violate a rule**, it stays in
> `CLAUDE.md`.
>
> If forgetting it would only make the agent **less informed**, it is a Mem0
> candidate.

Do not move whole sections. Split individual entries:

```
RULE                        -> CLAUDE.md
HISTORICAL EVIDENCE         -> Mem0
```

A permanent rule such as "verify a number before presenting it as evidence"
stays. The concrete incident that taught you why — a report that showed the top
10 of 53 record types and hid 17 with real activity — is a Mem0 memory.

Keep the example in `CLAUDE.md` when removing it would make a safety-critical
rule ambiguous. Ambiguity in a safety rule costs more than the tokens.

## What is worth storing

| Kind | Store when |
|---|---|
| `decision` | an architectural choice was made and the reasoning will not be obvious from the code |
| `discovery` | third-party or environment behaviour was observed that the docs do not state |
| `lesson` | a debugging session produced a generalisable insight |
| `convention` | a project convention was established that no linter enforces |
| `failure` | an approach was tried and rejected, with the reason |
| `incident` | a concrete measured event worth citing later |
| `note` | none of the above, and you are not sure — expect it to stay a candidate |

## What is NOT worth storing

- Anything the repository already states. Repository truth is retrieved live
  (handoff section 14); a copy in Mem0 only creates something to go stale.
- Whole files. Mem0 is not a second copy of the codebase.
- Conversation transcripts. Extract the decision, not the dialogue.
- Duplicates of permanent rules, merely because they also have evidence.

## Lifecycle

```
candidate ──> durable ──> stale ──> superseded
    │             │          │
    └─────────────┴──────────┴──> superseded
```

- `candidate` — extracted, not yet trusted. Everything enters here.
- `durable` — deliberately promoted. Repeatedly useful, still true.
- `stale` — contradicted by current truth, or simply old. Still retrievable,
  always rendered with a warning.
- `superseded` — replaced. Terminal, and never retrieved. Always carries a
  pointer to its replacement; a superseded memory with no successor is
  unrecoverable context, so the code refuses to write one.

`stale → durable` is legal: it means the memory was re-checked and still holds.
Nothing returns to `candidate` — a memory that was once trusted cannot become
untested again.

## Promotion, and the CLAUDE.md ceiling

```
agent experience
      │
      ▼
candidate memory
      │
  ┌───┴────┐
transient  durable
   │          │
 session    Mem0
              │
     repeated / important / broadly applicable
              │
              ▼
      CLAUDE.md candidate
```

A Mem0 memory earns promotion into a permanent rule only when it is repeatedly
relevant, stable, broadly applicable, and correctness- or safety-critical.
This ceiling is what stops `CLAUDE.md` becoming a historical log.

## Scopes

| Scope | Holds |
|---|---|
| `global` | broadly applicable engineering knowledge |
| `user` | preferences and working style, validated |
| `project` | architecture decisions |
| `repository` | conventions and discovered behaviour |
| `branch` | temporary implementation context |
| `session` | what happened in one working session |
| `task` | previous attempts and failures on one task |

**Never store repository-specific information at `global` scope.** It will
surface on unrelated work and read as universal truth.

Scopes map onto Mem0's identifiers as: `user` → `user_id`,
`project`/`repository` → `agent_id`, `branch`/`session`/`task` → `run_id`, with
the scope level and key also written into metadata so they can be filtered
directly.
