# Context Manager evaluation

## Methodology

- dataset: repo-local (8 cases)
- model: none (retrieval only)
- retrieval configuration: layers as named per arm
- top_k: 10
- reranker: none
- token_budget: 8000
- runs: 1
- self_reported: True

**This is a retrieval-only benchmark.** It measures which items the
Context Manager selects, not whether an agent given that context answers
correctly. Recall@K here is not an end-to-end memory QA score and must
not be reported as one.

## Results over 8 cases

| arm                    | recall@k | prec@k |    mrr |   ndcg |  tokens | mem |  ms | scored |
|------------------------|---------:|-------:|-------:|-------:|--------:|----:|----:|-------:|
| full                   |   0.510 |   0.125 |  0.557 |  0.436 |   87112 |    5 |   5038.2 |     8/8 |
| static-docs-only       |   0.375 |   0.075 |  0.500 |  0.352 |    8000 |    0 |   3485.5 |     8/8 |
| memory-only            |   0.167 |   0.100 |  0.500 |  0.233 |     301 |    5 |    952.6 |     8/8 |
| static+memory          |   0.479 |   0.112 |  0.560 |  0.423 |    8000 |    5 |   4385.3 |     8/8 |
| static+memory+state    |   0.479 |   0.112 |  0.557 |  0.421 |    8000 |    5 |   5123.6 |     8/8 |

`scored` is the number of cases that could be scored at all; a case whose arm returned nothing is undefined rather than zero, and is excluded from the mean rather than dragging it down.

## Notes

- repo-local (repo-local): 8 cases - Questions about this repository whose answers live in known files or in seeded memories. Ground truth is source PATHS for file layers and mem0:<topic> for the memory layer - both stable across edits and re-seeds, unlike chunk ids and Mem0 uuids. Run `python -m src.scripts.context_eval --seed` first, or the memory arms score against whatever happens to be stored. This is NOT LongMemEval: a handful of cases about one repository cannot support a claim about general retrieval quality, and any weight tuned here has been measured on a different basis than a published benchmark number.
- Memory layer present only when a Mem0 server was reachable; arms naming memory score n/a without one, rather than zero.
- The `full` arm runs with an effectively unlimited budget so it is the information ceiling, not a differently-budgeted competitor.
- CEILING: 4 ground-truth item(s) are not collected by any source, so no arm can reach them and recall is capped below 1.0 by construction: .gitattributes, server/main.py, skills/context-memory/SKILL.md, skills/context-memory/references/context-precedence.md. This is a source coverage limit, not a ranking failure - read it before treating a low recall as a scoring problem.
