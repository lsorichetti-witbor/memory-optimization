# Instruction set comparison

dataset: global-instruction-rules (40 rules)
budget:  8000 tokens

before: 40 of 40 rules present, 40 of 40 selected, 3911 instruction tokens
after: 40 of 40 rules present, 40 of 40 selected, 3242 instruction tokens

instruction tokens: 3911 -> 3242 (-669, -17.1%)
No rule was lost or made unreachable. This does NOT establish that agent behaviour is unchanged - see the module docstring.

| rule | before present | before selected | after present | after selected |
|---|:--:|:--:|:--:|:--:|
| caveman | yes | yes | yes | yes |
| pnpm | yes | yes | yes | yes |
| npm-others | yes | yes | yes | yes |
| subagent-model | yes | yes | yes | yes |
| verify-number | yes | yes | yes | yes |
| trust-subagent | yes | yes | yes | yes |
| revision-scope | yes | yes | yes | yes |
| aggregate-layer | yes | yes | yes | yes |
| short-id | yes | yes | yes | yes |
| discrepancy | yes | yes | yes | yes |
| silent-direction | yes | yes | yes | yes |
| exit-zero | yes | yes | yes | yes |
| acceptable-limit | yes | yes | yes | yes |
| valid-subset | yes | yes | yes | yes |
| truncated-output | yes | yes | yes | yes |
| inherited-target | yes | yes | yes | yes |
| measure-env | yes | yes | yes | yes |
| env-override | yes | yes | yes | yes |
| machine-readable | yes | yes | yes | yes |
| reader-shell | yes | yes | yes | yes |
| cardinality | yes | yes | yes | yes |
| fan-in | yes | yes | yes | yes |
| single-element | yes | yes | yes | yes |
| sweep-class | yes | yes | yes | yes |
| test-independent | yes | yes | yes | yes |
| predicate-name | yes | yes | yes | yes |
| red-first | yes | yes | yes | yes |
| fixture-drift | yes | yes | yes | yes |
| assert-outcome | yes | yes | yes | yes |
| live-subagent | yes | yes | yes | yes |
| file-tools | yes | yes | yes | yes |
| artifact-unit | yes | yes | yes | yes |
| untracked-source | yes | yes | yes | yes |
| diff-compliance | yes | yes | yes | yes |
| two-checkers | yes | yes | yes | yes |
| batch-findings | yes | yes | yes | yes |
| memory-scope | yes | yes | yes | yes |
| netsuite-rules | yes | yes | yes | yes |
| graphify | yes | yes | yes | yes |
| memory-stack-up | yes | yes | yes | yes |
