"""The shared-scope memories: engineering lessons that hold anywhere.

Two groups live here.

**Rule incidents.** The concrete measurements that used to sit inside the rules
in the global `CLAUDE.md`, extracted by the handoff section 19 refactor. The
rule stayed; the evidence moved here. That makes this file load-bearing: after
the refactor these memories are the *only* record of why several rules exist, so
they have to be reproducible from version control rather than living only in a
database nobody backs up.

**Retrieval findings.** Things learned about how this stack behaves that a
future change could silently break.

Idempotent by topic: re-seeding replaces a memory rather than adding a second
one, because two memories on one topic make the conflict resolver mark one stale
and retrieval then measures staleness handling instead of relevance.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.memory.base import MemoryProvider
from src.memory.scopes import Scope

SHARED_SCOPE_KEY = "global"


@dataclass(frozen=True)
class SharedMemory:
    topic: str
    kind: str
    text: str
    importance: float = 0.8


RULE_INCIDENTS: tuple[SharedMemory, ...] = (
    SharedMemory(
        "rule.shortened_identifier", "incident",
        "Left-truncating identifiers that share a long generated prefix collapsed distinct objects "
        "into one label: measured on one corpus, three different files rendered as the identical "
        "string, and a pair printed as an item paired with itself. This is the evidence behind the "
        "CLAUDE.md rule that a shortened identifier must resolve back to exactly one object.",
    ),
    SharedMemory(
        "rule.truncated_output", "incident",
        "A report showing the top 10 of 53 record types hid 17 that had real activity but no "
        "deployments, because the ranking dimension could not reach rows scoring zero on it. This "
        "is the evidence behind the CLAUDE.md rule that truncated output must print its denominator "
        "and, where excluded rows matter, a second view ranked so they can appear.",
    ),
    SharedMemory(
        "rule.multi_value_field", "incident",
        "A pipe-delimited multi-value field keyed raw made every membership query match only rows "
        "whose value was exactly that combination: 'which deployments run in the UI' returned 2 of "
        "96. This is the evidence behind the CLAUDE.md rule to establish a field's cardinality "
        "before it becomes a key, and to split on ingest.",
    ),
    SharedMemory(
        "rule.relation_fan_in", "incident",
        "A relation that was 1:1 by construction yielded a flag rather than a quantity, so a delete "
        "count maxed out at 1 and reported 10 where the truth was 17. This is the evidence behind "
        "the CLAUDE.md rule to check maximum fan-in before printing len() of a relation as a count.",
    ),
    SharedMemory(
        "rule.single_element_pick", "incident",
        "Exactly one deployment in a corpus carried two records, and next(iter(a_set)) picked by "
        "hash order. The bug stayed invisible because both candidates happened to be equivalent - "
        "masked, not absent - and the choice moves with PYTHONHASHSEED between runs. This is the "
        "evidence behind the CLAUDE.md rule to establish multiplicity before taking one element.",
    ),
    SharedMemory(
        "rule.generated_artifact_unit", "incident",
        "Reverting one generated view from version control after its source had been rewritten left "
        "the view describing a structure the source no longer had. The result looked plausible and "
        "only a dedicated cross-check caught it. This is the evidence behind the CLAUDE.md rule to "
        "regenerate a generated set whole rather than restoring a member.",
    ),
    SharedMemory(
        "rule.generated_churn", "incident",
        "Version-controlling a generated artifact with unstable labels meant a no-op rebuild rewrote "
        "833 of 1,217 files, all of it renumbering, hiding real defects inside pure churn. This is "
        "the evidence behind the CLAUDE.md rule not to version-control a rendering whose source is "
        "untracked.",
    ),
)

RETRIEVAL_FINDINGS: tuple[SharedMemory, ...] = (
    SharedMemory(
        "retrieval.pgvector_filter_order", "discovery",
        "mem0's pgvector search issues 'SELECT ... WHERE <filters> ORDER BY vector <=> q LIMIT k', "
        "so the scope filter is applied BEFORE ranking and top_k applies to the already-filtered "
        "set. Verified by EXPLAIN ANALYZE: 'Seq Scan ... Filter: payload->>scope = global ... Rows "
        "Removed by Filter: 10' then Sort then Limit. A global-scope query therefore ranks globals "
        "against each other and is not crowded out by repository memories.",
        importance=0.7,
    ),
    SharedMemory(
        "retrieval.hnsw_post_filter_risk", "lesson",
        "The memories table carries an HNSW index (memories_hnsw_idx, vector_cosine_ops). At small "
        "row counts Postgres ignores it and seq-scans, so scope filtering is exact. Forcing the "
        "index with enable_seqscan=off changes the plan to 'Index Scan using memories_hnsw_idx ... "
        "Filter: ... Rows Removed by Filter: 8' - the filter is then applied to whatever the index "
        "already returned. At scale a selective filter such as scope=global can therefore return "
        "FEWER rows than exist and fewer than top_k, and that reads exactly like an empty scope "
        "rather than like an error. Failure direction: silently under-returning. Mitigations when "
        "the planner flips to the index, likely in the low thousands of rows: raise hnsw.ef_search, "
        "or add a partial/composite index on payload->>'scope'.",
        importance=0.9,
    ),
    SharedMemory(
        "retrieval.fastapi_ignores_unknown_params", "lesson",
        "FastAPI silently ignores query parameters that are not in the endpoint signature, so an "
        "unsupported filter looks like it works and does nothing. Verified against the self-hosted "
        "Mem0 server: GET /memories?scope=NONSENSE still returned every row, while POST /search "
        "does honour metadata filters. Any scope filtering on GET /memories has to happen "
        "client-side.",
        importance=0.8,
    ),
    SharedMemory(
        "retrieval.similarity_always_returns", "lesson",
        "Vector similarity search always returns nearest neighbours, so a query matching nothing "
        "still comes back with rows. Measured on the self-hosted Mem0 stack: a nonsense query "
        "scored 0.50-0.55 against real memories scoring 0.65-0.78. A result is not evidence of a "
        "match; its score is. Impose a threshold derived from your own data rather than trusting "
        "the presence of rows.",
        importance=0.8,
    ),
)

SHARED_MEMORIES: tuple[SharedMemory, ...] = RULE_INCIDENTS + RETRIEVAL_FINDINGS


def seed_shared(provider: MemoryProvider, source: str = "global_seed") -> dict[str, str]:
    """Ensure exactly one shared memory per topic. Returns topic -> memory id."""
    page = provider.get_all(scope=Scope.GLOBAL, scope_key=SHARED_SCOPE_KEY, top_k=1000)
    if page.truncated:
        raise RuntimeError(
            f"the shared scope holds at least {page.returned} memories, which is the page limit - "
            "raise top_k before seeding so duplicate topics can be found and removed."
        )

    by_topic: dict[str, list[str]] = {}
    for record in page.records:
        if record.envelope.topic:
            by_topic.setdefault(record.envelope.topic, []).append(record.id)

    written: dict[str, str] = {}
    for memory in SHARED_MEMORIES:
        for stale_id in by_topic.get(memory.topic, []):
            provider.delete(stale_id)
        records = provider.add(
            memory.text,
            scope=Scope.GLOBAL,
            scope_key=SHARED_SCOPE_KEY,
            kind=memory.kind,
            topic=memory.topic,
            confidence=1.0,
            importance=memory.importance,
            source=source,
        )
        written[memory.topic] = records[0].id
    return written
