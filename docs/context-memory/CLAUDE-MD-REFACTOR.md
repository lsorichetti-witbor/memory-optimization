# Handoff section 19 — the CLAUDE.md refactor

**Date:** 2026-09-13 · **Status:** built, benchmarked, **not yet applied to the
live file** · **Result:** 40 of 40 rules preserved and still selected,
instruction tokens 3,911 → 3,242 (**−17.1%**)

Candidate: [`CLAUDE.md.candidate`](CLAUDE.md.candidate) ·
[`candidate-rules/graphify.md`](candidate-rules/graphify.md)
Per-rule table: [`CLAUDE-MD-REFACTOR-RESULTS.md`](CLAUDE-MD-REFACTOR-RESULTS.md)

---

## Why this was deferred, and what changed

Section 19.6 asks whether **task success** regressed after the refactor. The
retrieval harness built in phase 6 measures which items get *selected* on eight
repo-local questions — it cannot answer that, and running 19 against it would be
inheriting a target from a different measurement basis.

So rather than claim more than could be measured, a second benchmark was built
that answers a narrower question honestly: **is every invariant still present,
and is it still reachable within the budget?** That is the actual risk of
editing an instruction file. It is not the same as "behaviour is unchanged", and
the report says so on every run.

## The benchmark

`src/evaluation/instructions.py`, driven by
`src/evaluation/datasets/instruction-rules.json` (40 cases) via
`python -m src.scripts.instruction_eval`.

Each case pairs an invariant with a task that should surface it, plus the markers
that must survive any rewording:

```json
{ "id": "exit-zero",
  "task": "check whether the build command succeeded",
  "invariant": "Exit status 0 is not evidence of success",
  "must_match": ["exit status 0", "empty"] }
```

Four decisions that make the measurement mean something:

- **Markers are written by hand, not generated from the file.** Deriving them
  from the text under test would make every case pass by construction and could
  never catch a rule being dropped.
- **All markers must appear in ONE chunk.** Split across two unrelated rules the
  invariant is not intact anywhere, and counting it present would hide exactly
  the kind of loss a refactor causes.
- **`present` and `selected` are separate.** A rule can survive intact and still
  never reach the agent — ranked below the budget cut, or in a chunk too large
  to fit. Checking presence alone would call that a success.
- **The real `InstructionsSource` reads the candidate**, staged into a
  repo-shaped temp directory. Scoring a refactor with a different reader than
  the product uses would measure the reader, not the refactor.

A `BudgetExceeded` — the build refusing to trim pinned policy — is recorded as
"not selected" with a flag, because an instruction set that cannot fit its own
budget reaches the agent as an error, not as policy.

## Classification (19.1 – 19.5)

| Class | Count | Disposition |
|---|--:|---|
| **A** Permanent instruction | 33 | Kept in `CLAUDE.md`, some tightened |
| **B** Project/tool-specific | 2 | `graphify` → `rules/graphify.md`; NetSuite already in `rules/netsuite.md` |
| **C** Historical knowledge | 1 | The MCP-server TODO → Mem0 `global`, `kind=decision` |
| **D** Concrete evidence | 7 | → Mem0 `global`, `kind=incident` |
| **E** Duplicate/redundant | 2 | Two subagent-model bullets merged into one |
| **F** No longer valid | 0 | Nothing was obsolete |

**No rule was deleted.** The entire saving comes from extracting evidence,
merging two duplicate bullets, and tightening prose.

### What moved to Mem0 (19.3)

Seven measured incidents, `scope=global`, `kind=incident`, one topic each:

| Topic | The measurement |
|---|---|
| `rule.shortened_identifier` | three files rendered as one identical label; a pair printed as an item paired with itself |
| `rule.truncated_output` | top 10 of 53 record types hid 17 with real activity |
| `rule.multi_value_field` | "which deployments run in the UI" returned 2 of 96 |
| `rule.relation_fan_in` | delete count maxed at 1, reported 10 where the truth was 17 |
| `rule.single_element_pick` | one deployment carried two records; masked because both were equivalent |
| `rule.generated_artifact_unit` | a reverted view described a structure its source no longer had |
| `rule.generated_churn` | a no-op rebuild rewrote 833 of 1,217 files, all renumbering |

Plus `context_memory.mcp_server` as `kind=decision`.

**Retrieval verified, not assumed** — each returns its own incident from the
shared scope at 0.70–0.76, against a ~0.50 noise floor:

```
"why must a truncated report print its denominator"   → 0.704  rule.truncated_output
"what went wrong with pipe delimited multi value fields" → 0.759  rule.multi_value_field
"why can a shortened identifier be dangerous"          → 0.707  rule.shortened_identifier
```

### What was kept deliberately

Section 6 warns against moving every example. Three stayed because they make the
rule *actionable* rather than merely illustrating it:

- `PYTHONHASHSEED` — names the mechanism that makes the ordering non-deterministic.
- `VAR=1 cmd` is bash-only — the concrete trap the shell rule exists for.
- `(10 of 53)` — shows the required *form* of a denominator, not an incident.

## Results (19.6)

```
before: 40 of 40 rules present, 40 of 40 selected, 3911 instruction tokens
after:  40 of 40 rules present, 40 of 40 selected, 3242 instruction tokens

instruction tokens: 3911 -> 3242 (-669, -17.1%)
```

Every one of the 40 rules is `yes/yes/yes/yes` in
[the per-rule table](CLAUDE-MD-REFACTOR-RESULTS.md). No rule lost, none made
unreachable, `exit 0`.

### The structural check found what the benchmark missed

A bullet-level diff was run *in addition to* the benchmark, and it earned its
place: the first candidate silently dropped the line giving the command to start
the memory stack. No case covered it, so the benchmark reported a clean 39/39.

Restored, and `memory-stack-up` added as case 40 — so a future refactor cannot
drop it quietly. **Two checks disagreeing is the point of running both.**

Full accounting, 42 bullets → 39:

| Change | Effect |
|---|---|
| Two subagent-model bullets merged | −1 |
| MCP TODO → Mem0 | −1 |
| `graphify` bullet → `rules/graphify.md` | −1 |
| Mem0 pointer bullet added | +1 |
| Stack-up line dropped, then **restored** | 0 |

Headings 13 → 12: `# graphify` moved into its own rules file.

## What this does NOT establish

Stated plainly, because the temptation is to read −17.1% as a clean win:

- **It does not show agent behaviour is unchanged.** Wording matters, and this
  measures only that the words are present and reachable. A refactor can pass
  every check here and still change how a rule is followed.
- **40 cases are not every possible way a rule matters.** The stack-up line
  proved that: it was uncovered until a different check found it.
- **Token counts use the heuristic counter** (~4 chars/token), since `tiktoken`
  is not installed and this stack runs on Gemini. The −17.1% is a ratio of two
  numbers measured the same way, which is the comparison that matters, but it is
  not an exact token count for any specific model.
- **Nothing was measured about the rules moved to `rules/*.md`.** They are still
  `@`-imported, so the loaded token count is unchanged by relocation — the
  saving is from extraction and tightening, not from moving text around.

## Applying it

Not applied. The live `~/.claude/CLAUDE.md` is unchanged; the candidate sits in
this directory. To apply:

```powershell
$src = "D:\Desktop D\Gestión\memory-optimization\docs\context-memory"
Copy-Item "$env:USERPROFILE\.claude\CLAUDE.md" "$env:USERPROFILE\.claude\CLAUDE.md.bak-$(Get-Date -f yyyyMMdd)"
Copy-Item "$src\CLAUDE.md.candidate" "$env:USERPROFILE\.claude\CLAUDE.md"
Copy-Item "$src\candidate-rules\graphify.md" "$env:USERPROFILE\.claude\rules\graphify.md"
```

Then re-verify against the live file:

```powershell
.\.venv\Scripts\python.exe -m src.scripts.instruction_eval --before "$env:USERPROFILE\.claude"
```

Expect `40 of 40 rules present, 40 of 40 selected`. The incidents are already in
Mem0, so the evidence survives the swap either way.

## Re-running

```powershell
.\.venv\Scripts\python.exe -m src.scripts.instruction_eval `
  --before "$env:USERPROFILE\.claude" --after <candidate dir>
```

Exit code is non-zero on regression, so a caller checking only the exit code
cannot read a refactor that dropped a rule as a success. Unit coverage:
`tests/context_memory/test_instruction_eval.py` (14 tests, no server needed).
