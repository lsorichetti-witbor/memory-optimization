# Retrieval policy

## Multi-signal, not embedding-only

Embedding similarity alone is not enough (handoff section 12). The score is:

```
context_score =   relevance
                + task_scope_match
                + project_scope_match
                + confidence
                + recency
                + importance
                - redundancy
                - staleness
```

Each term is stored separately on the item, and every ranked item carries a
`ScoreBreakdown` naming the contribution of each component. That is deliberate:
a single opaque number makes it impossible to tell a retrieval failure from a
ranking failure.

## Where each signal comes from

| Signal | Source |
|---|---|
| `relevance` | memory layer: the server's embedding similarity. Other layers: BM25 over the candidate set |
| `task_scope_match` | item is one of the task's files, or scoped to this task/branch |
| `project_scope_match` | item's scope key equals the task's repository or project |
| `confidence` | memory envelope. Repository and instruction items are 1.0 by construction |
| `recency` | memory: `1 / (1 + age_days / 90)`. Current state: always 1.0 |
| `importance` | memory envelope; instructions: how near the file is to the task |
| `redundancy` | set by deduplication |
| `staleness` | 1.0 for a `stale` memory or one overruled by a conflict rule |

## The weights are unmeasured

`ScoringWeights` ships with every component at 1.0. These are **placeholders**.
The handoff is explicit that final weights must not be invented before
measurement, and the evaluation harness is what turns them into measured values.

Do not tune them by eye against one task that looks wrong. That is fitting the
tolerance to the result rather than deriving the target — and it is exactly the
failure mode these rules exist to prevent. Change a weight when a benchmark run
says to.

## Why BM25 is absolute, not min-max normalised

Min-max normalisation forces the best candidate in the set to 1.0 even when
nothing matched well. That value then gets summed against the memory layer's
embedding similarity, which is absolute. Two components on different bases in
one weighted sum produce a total that is right only by luck.

The lexical scorer therefore uses `1 - exp(-bm25_raw)`: bounded to [0, 1],
comparable across queries and corpora, and a weak match scores weakly even when
it is the best thing available.

## Chunking

Files are split on markdown headings, carrying the full heading path as the
title, so the budget operates per rule or per section rather than per file. A
whole `CLAUDE.md` injected as one unit defeats the point.

Non-markdown files are **not** heading-split: `#` in Python is a comment, and
splitting on it would shred the file.

Chunk ids embed the source path, so two files with identical headings never
collide, and a shortened id still names its file.

## Deduplication

Word-level 3-shingles, Jaccard similarity, default threshold 0.85. Items under
5 tokens compare by exact equality only — shingling short strings produces noise.

Which copy survives is not arbitrary: **higher-precedence layer wins first**,
score only breaks the tie. A memory that restates the README loses to the README.
The survivor records what it absorbed, so the drop is traceable.

## Budgeting

- Pinned items (instructions) are always included. If they alone exceed the cap,
  the build **raises** rather than trimming policy.
- Per-layer floors reserve room so a flood of high-scoring memories cannot
  starve the current-state layer.
- Selection is greedy by score but does not stop at the first item that does not
  fit — a smaller item can still earn the remaining space.
- The report always prints `N of M items included` and the token count, along
  with which counter measured it. Without `tiktoken` installed the counter is a
  4-chars-per-token heuristic; the report says so rather than implying precision
  it does not have.
