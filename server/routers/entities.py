from collections import defaultdict
from datetime import datetime
from typing import Any, Literal, Optional

from auth import require_admin, verify_auth
from errors import upstream_error
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from schemas import MessageResponse
from server_state import get_memory_instance

router = APIRouter(prefix="/entities", tags=["entities"])

SCAN_LIMIT = 10_000

EntityType = Literal["user", "agent", "run", "scope"]
TYPE_TO_FIELD: dict[str, str] = {"user": "user_id", "agent": "agent_id", "run": "run_id"}

# `scope` is not one of Mem0's identifiers. It is derived from the scope/scope_key
# metadata the Context Manager writes, and it exists because the three identifiers
# cannot express every grouping: a global-scope memory carries only a user_id, so
# it contributed to the `user` bucket and produced no entity of its own. Measured:
# 11 shared memories were invisible here while `user lautaro` silently counted 16.
SCOPE_TYPE = "scope"


def _scope_label(payload: dict[str, Any]) -> Optional[str]:
    scope = payload.get("scope")
    if not scope:
        return None
    key = payload.get("scope_key")
    return str(scope) if not key or key == scope else f"{scope}:{key}"


class Entity(BaseModel):
    id: str
    type: EntityType
    total_memories: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


def _iter_rows() -> list[Any]:
    results = get_memory_instance().vector_store.list(top_k=SCAN_LIMIT)
    return results[0] if results and isinstance(results, list) and isinstance(results[0], list) else results or []


def _iter_payloads() -> list[dict[str, Any]]:
    return [getattr(row, "payload", None) or {} for row in _iter_rows()]


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


@router.get("", response_model=list[Entity])
def list_entities(_auth=Depends(verify_auth)):
    buckets: dict[tuple[EntityType, str], dict[str, Any]] = defaultdict(
        lambda: {"total_memories": 0, "created_at": None, "updated_at": None}
    )

    for payload in _iter_payloads():
        created = _parse_timestamp(payload.get("created_at"))
        updated = _parse_timestamp(payload.get("updated_at")) or created

        values: list[tuple[str, Optional[str]]] = [
            (entity_type, payload.get(field)) for entity_type, field in TYPE_TO_FIELD.items()
        ]
        values.append((SCOPE_TYPE, _scope_label(payload)))

        for entity_type, value in values:
            if not value:
                continue
            bucket = buckets[(entity_type, str(value))]
            bucket["total_memories"] += 1
            if created and (bucket["created_at"] is None or created < bucket["created_at"]):
                bucket["created_at"] = created
            if updated and (bucket["updated_at"] is None or updated > bucket["updated_at"]):
                bucket["updated_at"] = updated

    return [
        Entity(id=entity_id, type=entity_type, **data)
        for (entity_type, entity_id), data in sorted(buckets.items(), key=lambda item: (item[0][0], item[0][1]))
    ]


@router.delete("/{entity_type}/{entity_id}", response_model=MessageResponse)
def delete_entity(entity_type: EntityType, entity_id: str, _auth=Depends(require_admin)):
    memory = get_memory_instance()
    try:
        if entity_type == SCOPE_TYPE:
            # Deliberately NOT delete_all: scope is metadata, and the only
            # identifier a scope like `global` has in common is user_id, so
            # delete_all would remove every memory that user owns in every
            # scope. Enumerate and delete by id instead - it cannot over-reach.
            removed = 0
            for row in _iter_rows():
                payload = getattr(row, "payload", None) or {}
                if _scope_label(payload) == entity_id:
                    memory.delete(memory_id=str(getattr(row, "id", "")))
                    removed += 1
            return MessageResponse(message=f"Deleted {removed} memories in scope {entity_id}")
        memory.delete_all(**{TYPE_TO_FIELD[entity_type]: entity_id})
    except Exception:
        raise upstream_error()
    return MessageResponse(message="Entity deleted")
