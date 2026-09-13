"""Seed the memories the repo-local dataset expects.

Without this the benchmark is not reproducible: the memory arms would score
against whatever happens to be in the store, so two runs on two machines would
produce different numbers for reasons unrelated to the code.

Idempotent by topic. Re-seeding replaces the memory carrying a topic rather than
adding a second one, because two memories on one topic make the conflict
resolver mark one stale and the run would then measure staleness handling
instead of retrieval.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.memory.base import MemoryProvider
from src.memory.scopes import Scope
from src.memory.types import MemoryPage


@dataclass(frozen=True)
class SeedMemory:
    topic: str
    text: str
    kind: str
    importance: float = 0.7


# These are the memories the repo-local dataset's `mem0:<topic>` ground truth
# refers to. Changing a topic here means changing the dataset too.
SEED_MEMORIES: tuple[SeedMemory, ...] = (
    SeedMemory(
        topic="server.graph_store",
        kind="discovery",
        text=(
            "server/AGENTS.md documents Neo4j in the dev stack on ports 8474 and 8687, but "
            "server/docker-compose.yaml defines no Neo4j service. The doc is stale; the "
            "self-hosted stack ships pgvector only."
        ),
        importance=0.8,
    ),
    SeedMemory(
        topic="server.default_provider",
        kind="decision",
        text=(
            "The self-hosted server runs on Gemini for both legs, selected with "
            "MEM0_DEFAULT_LLM_PROVIDER and MEM0_DEFAULT_EMBEDDER_PROVIDER. Anthropic is "
            "LLM-only and ships no embedding model. gemini-2.0-flash is retired and 404s; "
            "MEM0_DEFAULT_LLM_MODEL is pinned to gemini-3.6-flash."
        ),
        importance=0.8,
    ),
    SeedMemory(
        topic="server.embedding_dims",
        kind="lesson",
        text=(
            "pgvector defaults embedding_model_dims to 1536 while the Gemini embedder emits "
            "768. The mismatch is silent until the first insert, after the table already "
            "exists. MEM0_EMBEDDING_DIMS drives both sides from one value."
        ),
        importance=0.9,
    ),
    SeedMemory(
        topic="server.init_db_line_endings",
        kind="incident",
        text=(
            "server/init-db.sh checked out with CRLF under core.autocrlf=true, so its shebang "
            "resolved to /bin/bash\\r and postgres logged 'cannot execute: required file not "
            "found'. Postgres still reported healthy; the symptom appeared later as "
            "'database mem0_app does not exist'. Fixed with .gitattributes '*.sh text eol=lf'."
        ),
        importance=0.9,
    ),
    SeedMemory(
        topic="server.gemini_sdk",
        kind="discovery",
        text=(
            "mem0/llms/gemini.py and mem0/embeddings/gemini.py import 'from google import "
            "genai', which lives in the google-genai package, not google-generativeai. "
            "server/requirements.txt originally pinned only the latter."
        ),
        importance=0.8,
    ),
)


def seed(provider: MemoryProvider, scope_key: str, scope: Scope = Scope.REPOSITORY) -> dict[str, str]:
    """Ensure exactly one memory per seed topic. Returns topic -> memory id."""
    existing: MemoryPage = provider.get_all(scope=scope, scope_key=scope_key, top_k=1000)
    if existing.truncated:
        # Silently seeding on top of a page we could not see all of would leave
        # duplicate topics behind and change what the benchmark measures.
        raise RuntimeError(
            f"scope {scope.value}:{scope_key} holds at least {existing.returned} memories, "
            "which is the page limit - raise top_k before seeding so duplicate topics "
            "can be found and removed."
        )

    by_topic: dict[str, list[str]] = {}
    for record in existing.records:
        topic = record.envelope.topic
        if topic:
            by_topic.setdefault(topic, []).append(record.id)

    written: dict[str, str] = {}
    for memory in SEED_MEMORIES:
        for stale_id in by_topic.get(memory.topic, []):
            provider.delete(stale_id)
        records = provider.add(
            memory.text,
            scope=scope,
            scope_key=scope_key,
            kind=memory.kind,
            topic=memory.topic,
            importance=memory.importance,
            confidence=1.0,
            source="evaluation:seed",
        )
        written[memory.topic] = records[0].id
    return written
