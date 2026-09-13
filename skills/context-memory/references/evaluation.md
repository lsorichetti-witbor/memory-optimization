# Evaluation

Evaluate this as a **context-selection system**, not as a memory database. The
question is whether the Context Manager improves context *efficiency*, not
whether it retrieves more.

## Retrieval quality is not answer quality

```
retrieval_recall@k  !=  answer_accuracy
```

A run can retrieve the right memory and still answer wrong, or answer right
having retrieved nothing useful. Report them separately, and never present a
retrieval Recall@K as an end-to-end memory QA score.

## Metrics

Retrieval:

```
retrieval_recall_at_k
retrieval_precision_at_k
MRR
NDCG
```

Selection and cost:

```
context_tokens
memory_tokens
memories_retrieved
memories_injected
duplicate_rate
stale_memory_rate
contradiction_rate
latency_ms
```

Downstream:

```
answer_accuracy
abstention_accuracy
```

`memories_retrieved` vs `memories_injected` is the pair that matters most here:
a system that retrieves 50 and injects 6 correct ones is working; one that
injects all 50 is a retrieval system wearing a context manager's clothes.

## Configurations to compare

Handoff section 18 phase 6 — each is a separate arm, all on the same tasks:

1. full context (everything, no selection)
2. static docs only
3. Mem0 only
4. static + Mem0
5. static + Mem0 + current state

Arm 1 is the ceiling on information and the floor on efficiency. If arm 5 does
not beat arm 1 on tokens at comparable accuracy, the Context Manager is not
earning its place.

## Methodology block

Every reported result carries:

```
dataset
model
retrieval configuration
top-K
reranker
token budget
latency
number of runs
whether the result is self-reported
```

A number without this block is not comparable to anything.

## Benchmarks

LongMemEval, LongMemEval-V2, BEAM, MemoryArena, and LoCoMo where useful.

Before tuning anything to hit a published number, confirm the number was
measured on a comparable object. A threshold carried over from a sparser or
differently-filtered corpus can be unreachable by construction — and then the
tuning cannot succeed, only the tolerance can be widened.

## What the harness must decide

The ranker's `ScoringWeights` currently default to 1.0 on every component,
documented in-code as unmeasured placeholders. Turning those into measured
values is the point of this phase. Until it runs, they stay labelled unmeasured
rather than quietly treated as tuned.
