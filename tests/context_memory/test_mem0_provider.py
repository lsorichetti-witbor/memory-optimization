import json

import httpx
import pytest

from src.memory.mem0_provider import Mem0Provider
from src.memory.scopes import Scope
from src.memory.types import SearchQuery


def make_provider(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="http://mem0.test")
    return Mem0Provider(client=client, api_key="k", user="lautaro", **kwargs)


def test_add_posts_messages_scope_identifiers_and_encoded_envelope():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["key"] = request.headers.get("x-api-key")
        return httpx.Response(200, json={"results": [{"id": "m1", "memory": "a fact", "event": "ADD"}]})

    provider = make_provider(handler)
    records = provider.add(
        "Gemini is the only bundled provider that supplies both llm and embedder.",
        scope=Scope.REPOSITORY,
        scope_key="memory-optimization",
        kind="discovery",
        topic="server.default_provider",
    )

    assert seen["url"] == "http://mem0.test/memories"
    assert seen["key"] == "k"
    assert seen["body"]["messages"] == [
        {
            "role": "user",
            "content": "Gemini is the only bundled provider that supplies both llm and embedder.",
        }
    ]
    assert seen["body"]["user_id"] == "lautaro"
    assert seen["body"]["agent_id"] == "repo:memory-optimization"
    assert seen["body"]["metadata"]["scope"] == "repository"
    assert seen["body"]["metadata"]["lifecycle"] == "candidate"
    assert seen["body"]["metadata"]["topic"] == "server.default_provider"
    assert [r.id for r in records] == ["m1"]


def test_add_defaults_infer_to_false_for_verbatim_facts():
    # A curated memory must be stored as written. Letting the LLM re-extract it
    # can drop or reword the fact, and the loss is silent.
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"results": [{"id": "m1", "memory": "a verbatim fact"}]})

    make_provider(handler).add("a verbatim fact", scope=Scope.GLOBAL, scope_key="eng")
    assert seen["body"]["infer"] is False


def test_add_raises_when_the_server_returns_no_results_for_an_inferred_write():
    # results == [] means nothing was stored. Returning an empty list to the caller
    # would be indistinguishable from a successful write.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    with pytest.raises(RuntimeError, match="stored no memories"):
        make_provider(handler).add("a fact", scope=Scope.GLOBAL, scope_key="eng", infer=True)


def test_search_sends_filters_and_top_k_and_parses_scores():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "m1",
                        "memory": "a fact",
                        "score": 0.71,
                        "metadata": {"scope": "repository", "scope_key": "memory-optimization"},
                        "created_at": "2026-09-01T10:00:00+00:00",
                    }
                ]
            },
        )

    provider = make_provider(handler)
    results = provider.search(
        SearchQuery(query="which provider", scope=Scope.REPOSITORY, scope_key="memory-optimization", top_k=7)
    )

    assert seen["body"]["query"] == "which provider"
    assert seen["body"]["top_k"] == 7
    assert seen["body"]["filters"]["agent_id"] == "repo:memory-optimization"
    assert seen["body"]["filters"]["user_id"] == "lautaro"
    assert results[0].score == 0.71
    assert results[0].envelope.scope is Scope.REPOSITORY


def test_search_does_not_send_the_deprecated_top_level_identifiers():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"results": []})

    make_provider(handler).search(SearchQuery(query="q", scope=Scope.GLOBAL, scope_key="eng"))
    assert "user_id" not in seen["body"]
    assert "agent_id" not in seen["body"]


def test_get_all_reports_the_denominator_and_whether_it_truncated():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"id": f"m{i}", "memory": "x", "metadata": {"scope": "global", "scope_key": "eng"}}
                    for i in range(3)
                ]
            },
        )

    page = make_provider(handler).get_all(scope=Scope.GLOBAL, scope_key="eng", top_k=3)
    assert page.returned == 3
    assert page.limit == 3
    assert page.truncated is True  # returned == limit, so there may be more


def test_get_all_is_not_marked_truncated_when_it_came_back_short():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [{"id": "m0", "memory": "x", "metadata": {"scope": "global", "scope_key": "eng"}}]},
        )

    page = make_provider(handler).get_all(scope=Scope.GLOBAL, scope_key="eng", top_k=10)
    assert page.truncated is False


def test_update_sends_only_the_fields_the_caller_set():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": "Memory updated successfully"})

    make_provider(handler).update("m1", metadata={"lifecycle": "durable"})
    assert seen["method"] == "PUT"
    assert seen["body"] == {"metadata": {"lifecycle": "durable"}}


def test_http_error_carries_the_server_detail():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "At least one identifier is required."})

    with pytest.raises(RuntimeError, match="At least one identifier is required."):
        make_provider(handler).delete("m1")


def test_delete_all_requires_a_scope_key():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be reached
        raise AssertionError("no request should be sent")

    with pytest.raises(ValueError, match="scope_key"):
        make_provider(handler).delete_all(scope=Scope.GLOBAL, scope_key="")


def test_a_non_json_error_body_still_produces_a_useful_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    with pytest.raises(RuntimeError, match="502"):
        make_provider(handler).get("m1")


def test_promote_moves_a_candidate_to_durable_through_the_lifecycle_table():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200, json={"id": "m1", "memory": "x", "metadata": {"scope": "global", "lifecycle": "candidate"}}
            )
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": "Memory updated successfully"})

    make_provider(handler).promote("m1")
    assert seen["body"]["metadata"]["lifecycle"] == "durable"
    assert "lifecycle_changed_at" in seen["body"]["metadata"]


def test_supersede_requires_the_replacement_id():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"id": "m1", "memory": "x", "metadata": {"scope": "global", "lifecycle": "durable"}}
        )

    with pytest.raises(ValueError, match="superseded_by"):
        make_provider(handler).supersede("m1", replacement_id="")


def test_search_filters_on_scope_metadata_not_just_the_identifier_triple():
    # Measured defect: scope_identifiers(GLOBAL, ...) emits only user_id, so a
    # global search filtered by user alone and returned every repository-scoped
    # memory that user had ever written. The shared scope was not isolated at
    # all, and the leak looked exactly like a relevant result.
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"results": []})

    make_provider(handler).search(SearchQuery(query="q", scope=Scope.GLOBAL, scope_key="global"))
    assert seen["body"]["filters"]["scope"] == "global"
    assert seen["body"]["filters"]["scope_key"] == "global"


def test_a_repository_search_is_scoped_to_that_repository_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"results": []})

    make_provider(handler).search(
        SearchQuery(query="q", scope=Scope.REPOSITORY, scope_key="memory-optimization")
    )
    assert seen["body"]["filters"]["scope"] == "repository"
    assert seen["body"]["filters"]["scope_key"] == "memory-optimization"


def test_get_all_drops_rows_from_other_scopes():
    # GET /memories accepts only the identifier triple and top_k; FastAPI ignores
    # any other query parameter. Verified against the live server: `?scope=NONSENSE`
    # still returned all 5 repository memories. So asserting what we SEND would
    # pass while the scope filter did nothing - this asserts what comes back.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"id": "g1", "memory": "global fact", "metadata": {"scope": "global", "scope_key": "global"}},
                    {"id": "r1", "memory": "repo fact",
                     "metadata": {"scope": "repository", "scope_key": "memory-optimization"}},
                ]
            },
        )

    page = make_provider(handler).get_all(scope=Scope.GLOBAL, scope_key="global", top_k=10)
    assert [r.id for r in page.records] == ["g1"]
    assert page.returned == 1


def test_get_all_truncation_reflects_the_server_page_not_the_filtered_count():
    # Rows dropped by the client-side scope filter were still real rows, so a
    # full server page means more may exist even when few survive filtering.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"id": f"r{i}", "memory": "x",
                     "metadata": {"scope": "repository", "scope_key": "other"}}
                    for i in range(3)
                ]
            },
        )

    page = make_provider(handler).get_all(scope=Scope.GLOBAL, scope_key="global", top_k=3)
    assert page.returned == 0
    assert page.truncated is True
