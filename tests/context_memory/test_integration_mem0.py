"""Integration tests against a live self-hosted Mem0.

Run with:
    MEM0_API_URL=http://localhost:8888 MEM0_API_KEY=<admin key> MEM0_USER=lautaro \
        .venv/Scripts/python.exe -m pytest tests/context_memory/test_integration_mem0.py -q

A skip here is not a pass: it means the environment did not reach pytest.
"""

import os
import uuid

import pytest

from src.memory.mem0_provider import Mem0Provider
from src.memory.scopes import Scope
from src.memory.types import SearchQuery

pytestmark = pytest.mark.integration

API_URL = os.environ.get("MEM0_API_URL")


@pytest.fixture
def provider():
    if not API_URL:
        pytest.skip("MEM0_API_URL not set")
    p = Mem0Provider.from_env()
    yield p
    p.close()


def test_add_then_search_round_trips_against_a_live_server(provider):
    key = f"itest-{uuid.uuid4().hex[:8]}"
    provider.add(
        "The offline harness reads production data from a JSON snapshot, never from the live account.",
        scope=Scope.TASK,
        scope_key=key,
        kind="decision",
    )
    try:
        results = provider.search(
            SearchQuery(
                query="where does the offline harness read data from",
                scope=Scope.TASK,
                scope_key=key,
                top_k=5,
            )
        )
        assert results, "live server returned no results for a memory just written"
        assert "snapshot" in results[0].text.lower()
    finally:
        provider.delete_all(scope=Scope.TASK, scope_key=key)


def test_metadata_filters_survive_the_round_trip(provider):
    key = f"itest-{uuid.uuid4().hex[:8]}"
    provider.add(
        "pgvector is the only vector store in the self-hosted compose file.",
        scope=Scope.TASK,
        scope_key=key,
        kind="discovery",
        topic="vector_store",
        tags=("infra",),
    )
    try:
        results = provider.search(
            SearchQuery(query="vector store", scope=Scope.TASK, scope_key=key, top_k=5)
        )
        assert results, "live server returned no results"
        envelope = results[0].envelope
        assert envelope.topic == "vector_store"
        assert envelope.kind == "discovery"
        assert "infra" in envelope.tags
    finally:
        provider.delete_all(scope=Scope.TASK, scope_key=key)
