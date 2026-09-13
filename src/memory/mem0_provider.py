"""MemoryProvider backed by the self-hosted Mem0 REST API.

Endpoint shapes were read from `server/main.py` and re-verified against the
running server's `/openapi.json`. Auth is the `X-API-Key` header
(`server/auth.py`); a JWT works too but an API key is what a headless caller has.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

import httpx

from src.memory.base import MemoryProvider
from src.memory.envelope import Envelope, decode_envelope, encode_envelope
from src.memory.errors import InvalidWrite, Mem0Error
from src.memory.lifecycle import Lifecycle, parse_lifecycle, transition
from src.memory.scopes import Scope, scope_identifiers
from src.memory.types import MemoryPage, MemoryRecord, SearchQuery

DEFAULT_TIMEOUT = 30.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class Mem0Provider(MemoryProvider):
    def __init__(
        self,
        client: httpx.Client,
        api_key: str,
        user: str,
        repository: Optional[str] = None,
        project: Optional[str] = None,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._user = user
        self._repository = repository
        self._project = project

    @classmethod
    def from_env(cls) -> "Mem0Provider":
        base_url = os.environ.get("MEM0_API_URL")
        if not base_url:
            raise RuntimeError("MEM0_API_URL is not set")
        api_key = os.environ.get("MEM0_API_KEY", "")
        user = os.environ.get("MEM0_USER")
        if not user:
            raise RuntimeError("MEM0_USER is not set: every Mem0 write needs an identifier")
        return cls(
            client=httpx.Client(base_url=base_url.rstrip("/"), timeout=DEFAULT_TIMEOUT),
            api_key=api_key,
            user=user,
            repository=os.environ.get("MEM0_REPOSITORY"),
            project=os.environ.get("MEM0_PROJECT"),
        )

    def close(self) -> None:
        self._client.close()

    # -- transport ---------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"X-API-Key": self._api_key} if self._api_key else {}
        response = self._client.request(method, path, headers=headers, **kwargs)
        if response.status_code >= 400:
            detail, code, request_id = self._error_fields(response)
            raise Mem0Error(
                f"Mem0 {method} {path} failed [{response.status_code}]: {detail}",
                code=code,
                status=response.status_code,
                request_id=request_id,
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            # A body that will not parse is an infrastructure problem - a proxy
            # error page, a truncated response - not a malformed write. It gets
            # the default code so it is treated as temporary.
            raise Mem0Error(
                f"Mem0 {method} {path} returned a non-JSON body: {response.text[:200]!r}",
                status=response.status_code,
            ) from None

    @staticmethod
    def _error_fields(response: httpx.Response) -> tuple[str, str, Optional[str]]:
        """Pull the detail, the server's classification, and the request id.

        `code` and `request_id` come from server/errors.py's handler. A response
        without them (an upstream 502 from a proxy, say) yields "unknown", which
        is treated as temporary - the safe direction.
        """
        try:
            body = response.json()
        except ValueError:
            return (response.text[:200], "unknown", None)
        if not isinstance(body, dict):
            return (str(body)[:200], "unknown", None)
        detail = str(body["detail"]) if "detail" in body else str(body)[:200]
        code = str(body.get("code") or "unknown")
        request_id = body.get("request_id")
        return (detail, code, str(request_id) if request_id else None)

    # -- mapping -----------------------------------------------------------

    def _identifiers(self, scope: Scope, scope_key: str) -> dict[str, str]:
        return scope_identifiers(
            scope,
            key=scope_key,
            user=self._user,
            repository=self._repository,
            project=self._project,
        )

    @staticmethod
    def _to_record(row: dict[str, Any]) -> MemoryRecord:
        envelope = decode_envelope(row.get("metadata"))
        return MemoryRecord(
            id=str(row.get("id", "")),
            text=row.get("memory") or row.get("data") or "",
            envelope=envelope,
            score=row.get("score"),
            created_at=_parse_dt(row.get("created_at")) or envelope.created_at,
            updated_at=_parse_dt(row.get("updated_at")),
            raw=row,
        )

    # -- MemoryProvider ----------------------------------------------------

    def add(
        self,
        text: str,
        *,
        scope: Scope,
        scope_key: str,
        kind: str = "note",
        topic: Optional[str] = None,
        tags: Sequence[str] = (),
        confidence: float = 0.5,
        importance: float = 0.5,
        source: Optional[str] = None,
        infer: bool = False,
        created_at: Optional[datetime] = None,
        idempotency_key: Optional[str] = None,
    ) -> list[MemoryRecord]:
        """Store a memory.

        `infer` defaults to False: a curated memory must be stored as written.
        Letting the extraction LLM rewrite it loses detail silently.

        `idempotency_key` is carried in the metadata so a spooled write that was
        stored but whose confirmation never got back to the client can be
        recognised on replay instead of stored twice.
        """
        if not scope_key:
            raise InvalidWrite("scope_key is required")

        envelope = Envelope(
            scope=scope,
            scope_key=scope_key,
            kind=kind,
            lifecycle=Lifecycle.CANDIDATE.value,
            confidence=confidence,
            importance=importance,
            topic=topic,
            source=source,
            # A replayed write keeps the time it was MADE, not the time it was
            # replayed: recency is a ranking signal, and a month-old memory
            # restored from the spool must not rank as brand new.
            created_at=created_at or datetime.now(timezone.utc),
            tags=tuple(tags),
            extra={"idempotency_key": idempotency_key} if idempotency_key else {},
        )
        body: dict[str, Any] = {
            "messages": [{"role": "user", "content": text}],
            "metadata": encode_envelope(envelope),
            "infer": infer,
        }
        body.update(self._identifiers(scope, scope_key))

        payload = self._request("POST", "/memories", json=body) or {}
        rows = payload.get("results") or []
        if not rows:
            # An empty results array means nothing was stored. Returning [] here
            # would make a failed write look exactly like a successful one.
            raise RuntimeError(
                f"Mem0 accepted the request but stored no memories for scope {scope.value}:{scope_key}. "
                "With infer=True this usually means the extraction LLM found no fact in the text."
            )
        return [self._to_record(row) for row in rows]

    def _scope_filters(self, scope: Scope, scope_key: str) -> dict[str, Any]:
        """Identifier triple PLUS the scope metadata.

        The triple alone does not isolate every scope: `scope_identifiers` emits
        only `user_id` for GLOBAL, so a global search filtered by user and
        returned every repository-scoped memory that user had ever written.
        Measured: a global-scope query came back with all 5 memories belonging to
        one repository, and the leak was indistinguishable from a relevant hit.

        `scope` and `scope_key` are written into metadata on every add, and Mem0
        surfaces metadata as payload keys, so filtering on them isolates the
        scope properly.
        """
        filters: dict[str, Any] = dict(
            scope_identifiers(
                scope,
                key=scope_key,
                user=self._user,
                repository=self._repository,
                project=self._project,
            )
        )
        filters["scope"] = scope.value
        filters["scope_key"] = scope_key
        return filters

    def search(self, query: SearchQuery) -> list[MemoryRecord]:
        filters = self._scope_filters(query.scope, query.scope_key)
        body: dict[str, Any] = {"query": query.query, "filters": filters, "top_k": query.top_k}
        if query.threshold is not None:
            body["threshold"] = query.threshold

        payload = self._request("POST", "/search", json=body) or {}
        return [self._to_record(row) for row in payload.get("results") or []]

    def get(self, memory_id: str) -> MemoryRecord:
        payload = self._request("GET", f"/memories/{memory_id}") or {}
        return self._to_record(payload)

    def get_all(self, *, scope: Scope, scope_key: str, top_k: int = 100) -> MemoryPage:
        if not scope_key:
            raise ValueError("scope_key is required")
        # GET /memories takes only user_id/agent_id/run_id/top_k (server/main.py).
        # FastAPI silently ignores any other query parameter, so sending scope
        # here would look like a filter and do nothing - verified against the
        # live server: `?scope=NONSENSE` still returned all 5 repository
        # memories. The scope filter therefore has to be applied client-side.
        params = {k: str(v) for k, v in self._identifiers(scope, scope_key).items()}
        params["top_k"] = str(top_k)
        payload = self._request("GET", "/memories", params=params) or {}
        rows = payload.get("results") or []
        # `truncated` reflects the SERVER page, not the filtered result: the rows
        # dropped here were real, and more may exist beyond the page boundary.
        server_page_full = len(rows) >= top_k
        records = tuple(
            record
            for record in (self._to_record(row) for row in rows)
            if record.envelope.scope is scope and record.envelope.scope_key == scope_key
        )
        return MemoryPage(
            records=records,
            limit=top_k,
            returned=len(records),
            truncated=server_page_full,
        )

    def update(
        self,
        memory_id: str,
        *,
        text: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        body: dict[str, Any] = {}
        if text is not None:
            body["text"] = text
        if metadata is not None:
            body["metadata"] = metadata
        if not body:
            raise ValueError("update needs text or metadata")
        self._request("PUT", f"/memories/{memory_id}", json=body)

    def delete(self, memory_id: str) -> None:
        self._request("DELETE", f"/memories/{memory_id}")

    def delete_all(self, *, scope: Scope, scope_key: str, page: int = 1000) -> int:
        """Delete every memory in one scope. Returns how many were removed.

        Deliberately NOT the bulk `DELETE /memories` endpoint. That endpoint
        filters on the identifier triple only, and `scope_identifiers` emits just
        `user_id` for the GLOBAL scope - so deleting "global" sent
        `DELETE /memories?user_id=<user>` and removed everything that user had in
        every scope. Measured destructively: it wiped a seeded evaluation set
        while the caller had asked only for the global scope.

        Enumerating and deleting by id is slower and cannot over-reach: the
        listing is already scope-filtered client-side.
        """
        if not scope_key:
            raise ValueError("scope_key is required: deleting without one would target the whole scope")

        removed = 0
        while True:
            listing = self.get_all(scope=scope, scope_key=scope_key, top_k=page)
            if not listing.records:
                return removed
            for record in listing.records:
                self.delete(record.id)
                removed += 1
            if not listing.truncated:
                return removed

    def history(self, memory_id: str) -> list[dict[str, Any]]:
        payload = self._request("GET", f"/memories/{memory_id}/history")
        return payload or []

    def reset(self) -> None:
        self._request("POST", "/reset")

    # -- lifecycle ---------------------------------------------------------

    def _set_lifecycle(self, memory_id: str, target: Lifecycle, replacement_id: Optional[str] = None) -> None:
        current = self.get(memory_id)
        source = parse_lifecycle(current.envelope.lifecycle)
        patch = transition(source, target, at=_now_iso(), superseded_by=replacement_id)
        merged = encode_envelope(current.envelope)
        merged.update(patch)
        self.update(memory_id, metadata=merged)

    def promote(self, memory_id: str) -> None:
        """candidate/stale -> durable."""
        self._set_lifecycle(memory_id, Lifecycle.DURABLE)

    def mark_stale(self, memory_id: str) -> None:
        self._set_lifecycle(memory_id, Lifecycle.STALE)

    def supersede(self, memory_id: str, replacement_id: str) -> None:
        if not replacement_id:
            raise ValueError("superseded_by is required: a superseded memory needs a pointer to its replacement")
        self._set_lifecycle(memory_id, Lifecycle.SUPERSEDED, replacement_id=replacement_id)
