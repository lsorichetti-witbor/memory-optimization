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

EntityType = Literal["user", "agent", "run"]
TYPE_TO_FIELD: dict[str, str] = {"user": "user_id", "agent": "agent_id", "run": "run_id"}

# There was briefly a fourth `scope` entity type here, added because a global
# memory carried no agent_id and so produced no entity of its own. Giving every
# scope an agent_id fixed that at the source, and keeping both made /entities list
# each set of memories twice - once as `agent global` and once as `scope global` -
# which reads as duplicated data when nothing is duplicated. branch/session/task
# share their repository's agent, but their run_id distinguishes them, so the
# scope view was redundant rather than merely noisy.


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

        for entity_type, field in TYPE_TO_FIELD.items():
            value = payload.get(field)
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
        memory.delete_all(**{TYPE_TO_FIELD[entity_type]: entity_id})
    except Exception:
        raise upstream_error()
    return MessageResponse(message="Entity deleted")
