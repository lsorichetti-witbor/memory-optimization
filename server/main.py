import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pending
import pending_worker
import telemetry
from auth import ADMIN_API_KEY, AUTH_DISABLED, JWT_SECRET, require_admin, verify_auth
from db import SessionLocal
from dotenv import load_dotenv
from errors import (
    UpstreamError,
    _classify,
    install_request_id_logging,
    new_request_id,
    request_id_var,
    upstream_error,
    upstream_error_handler,
)
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from models import PendingMemory, RequestLog, User
from pydantic import BaseModel, Field
from rate_limit import limiter
from routers import api_keys as api_keys_router
from routers import auth as auth_router
from routers import entities as entities_router
from routers import requests as requests_router
from schemas import MessageResponse
from server_state import (
    get_current_config,
    get_memory_instance,
    initialize_state,
    set_session_factory,
    update_config,
)
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import func, select, update

from mem0.exceptions import ValidationError as Mem0ValidationError

load_dotenv()

install_request_id_logging()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - [%(request_id)s] %(message)s")

MIN_KEY_LENGTH = 16
SENSITIVE_CONFIG_KEYS = {
    "admin_api_key",
    "api_key",
    "authorization",
    "jwt_secret",
    "password",
    "password_hash",
    "secret",
    "token",
}
SKIPPED_REQUEST_LOG_PATHS = {"/api/health", "/docs", "/redoc", "/openapi.json"}
SKIPPED_REQUEST_LOG_PREFIXES = ("/requests",)

BUNDLED_LLM_PROVIDERS = ("openai", "anthropic", "gemini")
BUNDLED_EMBEDDER_PROVIDERS = ("openai", "gemini")


def _warn_if_unconfigured() -> None:
    """Pre-auth deployments upgrading into this build will 401 everywhere until
    an admin key or admin user exists. Surface the fix before the support tickets."""
    try:
        with SessionLocal() as session:
            if session.scalar(select(func.count(User.id))) > 0:
                return
    except Exception:
        return

    logging.warning(
        "\n%s\n"
        "  Auth is enabled by default and this server has no admin configured.\n"
        "  Protected endpoints will return 401 until you either:\n"
        "    1. Set ADMIN_API_KEY=<long-random-value>  (fastest, no client changes)\n"
        "    2. Register an admin at http://<host>:3000/setup\n"
        "    3. Set AUTH_DISABLED=true                 (local development only)\n"
        "  Docs: https://docs.mem0.ai/open-source/features/rest-api#authentication\n"
        "%s",
        "=" * 72,
        "=" * 72,
    )


if not AUTH_DISABLED and not JWT_SECRET:
    raise RuntimeError(
        "JWT_SECRET is required. Set it in .env (generate with `openssl rand -base64 48`) "
        "or set AUTH_DISABLED=true for local development only."
    )

if AUTH_DISABLED:
    logging.warning("AUTH_DISABLED is enabled. Protected endpoints are open for local development only.")
elif ADMIN_API_KEY and len(ADMIN_API_KEY) < MIN_KEY_LENGTH:
    logging.warning(
        "ADMIN_API_KEY is shorter than %d characters - consider using a longer key for production.",
        MIN_KEY_LENGTH,
    )
elif not ADMIN_API_KEY:
    _warn_if_unconfigured()

telemetry.log_status()

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "postgres")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5432")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "postgres")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "postgres")
POSTGRES_COLLECTION_NAME = os.environ.get("POSTGRES_COLLECTION_NAME", "memories")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
HISTORY_DB_PATH = os.environ.get("HISTORY_DB_PATH", "/app/history/history.db")
DEFAULT_LLM_PROVIDER = os.environ.get("MEM0_DEFAULT_LLM_PROVIDER", "openai")
DEFAULT_EMBEDDER_PROVIDER = os.environ.get("MEM0_DEFAULT_EMBEDDER_PROVIDER", "openai")
DEFAULT_LLM_MODEL = os.environ.get("MEM0_DEFAULT_LLM_MODEL", "gpt-5-mini")
DEFAULT_EMBEDDER_MODEL = os.environ.get("MEM0_DEFAULT_EMBEDDER_MODEL", "text-embedding-3-small")
# The pgvector column is created with this width. It MUST match what the embedder
# emits: text-embedding-3-small is 1536, gemini-embedding-001 defaults to 768.
# A mismatch fails at insert time, after the table already exists.
DEFAULT_EMBEDDING_DIMS = int(os.environ.get("MEM0_EMBEDDING_DIMS", "1536"))

_PROVIDER_KEYS = {
    "openai": OPENAI_API_KEY,
    "anthropic": ANTHROPIC_API_KEY,
    "gemini": GOOGLE_API_KEY,
}


def _provider_api_key(provider: str) -> Optional[str]:
    """Resolve the API key for a bundled provider.

    An unknown provider name would otherwise build a config with no api_key and
    fail later as an opaque upstream 500 on the first request. Fail at boot instead.
    """
    if provider not in _PROVIDER_KEYS:
        raise RuntimeError(
            f"Unknown provider '{provider}'. Bundled: {', '.join(sorted(_PROVIDER_KEYS))}. "
            "Set MEM0_DEFAULT_LLM_PROVIDER / MEM0_DEFAULT_EMBEDDER_PROVIDER to a bundled provider."
        )
    return _PROVIDER_KEYS[provider]

DEFAULT_CONFIG = {
    "version": "v1.1",
    "vector_store": {
        "provider": "pgvector",
        "config": {
            "host": POSTGRES_HOST,
            "port": int(POSTGRES_PORT),
            "dbname": POSTGRES_DB,
            "user": POSTGRES_USER,
            "password": POSTGRES_PASSWORD,
            "collection_name": POSTGRES_COLLECTION_NAME,
            "embedding_model_dims": DEFAULT_EMBEDDING_DIMS,
        },
    },
    "llm": {
        "provider": DEFAULT_LLM_PROVIDER,
        "config": {
            "api_key": _provider_api_key(DEFAULT_LLM_PROVIDER),
            "temperature": 0.2,
            "model": DEFAULT_LLM_MODEL,
        },
    },
    "embedder": {
        "provider": DEFAULT_EMBEDDER_PROVIDER,
        "config": {
            "api_key": _provider_api_key(DEFAULT_EMBEDDER_PROVIDER),
            "model": DEFAULT_EMBEDDER_MODEL,
            "embedding_dims": DEFAULT_EMBEDDING_DIMS,
        },
    },
    "history_db_path": HISTORY_DB_PATH,
}


set_session_factory(SessionLocal)
initialize_state(DEFAULT_CONFIG)


app = FastAPI(
    title="Mem0 REST APIs",
    description=(
        "A REST API for managing and searching memories for your AI Agents and Apps.\n\n"
        "## Authentication\n"
        "Supports Bearer JWT tokens, per-user API keys via `X-API-Key` header, "
        "or the legacy `ADMIN_API_KEY` environment variable. Set `AUTH_DISABLED=true` for local development only."
    ),
    version="1.0.0",
    redirect_slashes=False,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_exception_handler(UpstreamError, upstream_error_handler)
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://localhost:3000")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[DASHBOARD_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(api_keys_router.router)
app.include_router(entities_router.router)
app.include_router(requests_router.router)


@app.on_event("startup")
async def _start_pending_worker() -> None:
    """Drain the embedding queue in the background.

    Started here rather than as its own container so a single-container
    deployment still drains. Multiple replicas are safe: claims use
    FOR UPDATE SKIP LOCKED, so two workers never take the same row.
    """
    pending_worker.start(get_memory_instance)


@app.on_event("shutdown")
async def _stop_pending_worker() -> None:
    await pending_worker.stop()


class Message(BaseModel):
    role: str = Field(..., description="Role of the message (user or assistant).")
    content: str = Field(..., description="Message content.")


class MemoryCreate(BaseModel):
    messages: List[Message] = Field(..., description="List of messages to store.")
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    run_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    expiration_date: Optional[str] = Field(None, description="Expiration date in YYYY-MM-DD format.")
    infer: Optional[bool] = Field(None, description="Whether to extract facts from messages. Defaults to True.")
    memory_type: Optional[str] = Field(None, description="Type of memory to store (e.g. 'core').")
    prompt: Optional[str] = Field(None, description="Custom prompt to use for fact extraction.")


class MemoryUpdate(BaseModel):
    text: Optional[str] = Field(None, description="New content to update the memory with.")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Metadata to update.")
    expiration_date: Optional[str] = Field(None, description="Expiration date in YYYY-MM-DD format, or null to clear.")


class SearchRequest(BaseModel):
    query: str = Field(..., description="Search query.")
    user_id: Optional[str] = Field(None, description="Deprecated: pass inside `filters` instead.", deprecated=True)
    run_id: Optional[str] = Field(None, description="Deprecated: pass inside `filters` instead.", deprecated=True)
    agent_id: Optional[str] = Field(None, description="Deprecated: pass inside `filters` instead.", deprecated=True)
    filters: Optional[Dict[str, Any]] = None
    top_k: Optional[int] = Field(None, description="Maximum number of results to return.")
    threshold: Optional[float] = Field(None, description="Minimum similarity score for results.")
    explain: Optional[bool] = Field(None, description="Include score details for each search result.")
    show_expired: Optional[bool] = Field(None, description="Include expired memories.")


class GenerateInstructionsRequest(BaseModel):
    use_case: str = Field(..., description="Description of what the user will use Mem0 for.")


def _client_error(exc: Exception) -> HTTPException:
    """Map core validation / not-found errors to 4xx so clients can tell a bad
    request from an upstream outage. 'not found' is a 404, everything else a 400."""
    detail = str(exc)
    status_code = 404 if isinstance(exc, ValueError) and "not found" in detail.lower() else 400
    return HTTPException(status_code=status_code, detail=detail)


def _redact_config(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {item_key: _redact_config(item_value, item_key) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact_config(item_value, key) for item_value in value]
    if key is not None and key.lower() in SENSITIVE_CONFIG_KEYS:
        return "[redacted]" if value else value
    return value


def _validate_bundled_providers(config: Dict[str, Any]) -> None:
    llm = config.get("llm")
    if isinstance(llm, dict) and (provider := llm.get("provider")) and provider not in BUNDLED_LLM_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"LLM provider '{provider}' is not bundled in this image. "
                f"Bundled providers: {', '.join(BUNDLED_LLM_PROVIDERS)}. "
                "To use another provider, install its Python package, rebuild the container, "
                "and extend BUNDLED_LLM_PROVIDERS in server/main.py."
            ),
        )

    embedder = config.get("embedder")
    if (
        isinstance(embedder, dict)
        and (provider := embedder.get("provider"))
        and provider not in BUNDLED_EMBEDDER_PROVIDERS
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Embedder provider '{provider}' is not bundled in this image. "
                f"Bundled providers: {', '.join(BUNDLED_EMBEDDER_PROVIDERS)}. "
                "To use another provider, install its Python package, rebuild the container, "
                "and extend BUNDLED_EMBEDDER_PROVIDERS in server/main.py."
            ),
        )


def _should_log_request(request: Request) -> bool:
    if request.method == "OPTIONS":
        return False
    path = request.url.path
    if path in SKIPPED_REQUEST_LOG_PATHS:
        return False
    return not path.startswith(SKIPPED_REQUEST_LOG_PREFIXES)


def _persist_request_log(method: str, path: str, status_code: int, latency_ms: float, auth_type: str) -> None:
    session = SessionLocal()

    try:
        session.add(
            RequestLog(
                method=method,
                path=path,
                status_code=status_code,
                latency_ms=latency_ms,
                auth_type=auth_type,
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        logging.exception("Failed to persist request log")
    finally:
        session.close()


@app.middleware("http")
async def log_requests(request: Request, call_next):
    request.state.auth_type = getattr(request.state, "auth_type", "none")
    rid = new_request_id()
    token = request_id_var.set(rid)
    start = time.perf_counter()
    status_code = 500

    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = rid
        return response
    except Exception:
        status_code = 500
        raise
    finally:
        request_id_var.reset(token)
        if _should_log_request(request):
            asyncio.get_running_loop().run_in_executor(
                None,
                _persist_request_log,
                request.method,
                request.url.path,
                status_code,
                round((time.perf_counter() - start) * 1000, 2),
                getattr(request.state, "auth_type", "none"),
            )


@app.get("/configure", summary="Get current Mem0 configuration")
def get_config(_auth=Depends(verify_auth)):
    return _redact_config(get_current_config())


@app.get("/configure/providers", summary="List bundled LLM and embedder providers")
def list_bundled_providers(_auth=Depends(verify_auth)):
    return {"llm": list(BUNDLED_LLM_PROVIDERS), "embedder": list(BUNDLED_EMBEDDER_PROVIDERS)}


@app.post("/configure", summary="Configure Mem0")
def set_config(config: Dict[str, Any], _auth=Depends(require_admin)):
    """Set memory configuration. Requires admin role."""
    _validate_bundled_providers(config)
    update_config(config)
    return {"message": "Configuration set successfully"}


@app.post("/generate-instructions", summary="Generate custom instructions from a use case")
def generate_instructions(req: GenerateInstructionsRequest, _auth=Depends(verify_auth)):
    """Generate custom instructions and a contextual test message tailored to a use case."""
    try:
        llm = get_memory_instance().llm
        prompt = (
            "You are configuring a memory system. Given the use case below, produce two things:\n"
            "1. INSTRUCTIONS: A short paragraph of custom instructions telling the memory extraction system "
            "what kinds of facts, preferences, and context to prioritize. Be specific to the use case.\n"
            "2. TEST_MESSAGE: A single realistic sentence a user in this use case would say, suitable for "
            "testing that the memory system works.\n\n"
            "Respond in exactly this format (no markdown, no extra text):\n"
            "INSTRUCTIONS: <your instructions>\n"
            f"TEST_MESSAGE: <your test message>\n\nUse case: {req.use_case}"
        )
        response = llm.generate_response([{"role": "user", "content": prompt}])
        instructions = response
        test_message = "I like to hike on weekends."
        if "INSTRUCTIONS:" in response and "TEST_MESSAGE:" in response:
            parts = response.split("TEST_MESSAGE:")
            instructions = parts[0].replace("INSTRUCTIONS:", "").strip()
            test_message = parts[1].strip()
        return {"custom_instructions": instructions, "test_message": test_message}
    except Exception:
        raise upstream_error()


@app.post("/memories", summary="Create memories")
def add_memory(
    memory_create: MemoryCreate,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    _auth=Depends(verify_auth),
):
    """Store new memories.

    Inline first: the happy path is unchanged and still returns 200 with the
    stored records. Only when the *embedder* refuses does the write divert into
    the server-side queue and come back 202 - so a caller that never sees a
    failure never sees a difference, and a caller that does gets "I have it,
    it is not searchable yet" instead of "it is gone".
    """
    if not any([memory_create.user_id, memory_create.agent_id, memory_create.run_id]):
        raise HTTPException(status_code=400, detail="At least one identifier (user_id, agent_id, run_id) is required.")

    params = {k: v for k, v in memory_create.model_dump().items() if v is not None and k != "messages"}
    messages = [m.model_dump() for m in memory_create.messages]
    key = idempotency_key or (memory_create.metadata or {}).get("idempotency_key")
    if key:
        # The key has to travel with the memory into the vector store, or the
        # worker has nothing to compare against and cannot tell "already stored"
        # from "never stored" after a crash between insert and dequeue.
        params["metadata"] = {**(params.get("metadata") or {}), "idempotency_key": key}

    # A retry of a request whose response never arrived is the normal case, not
    # an error: answer it the same way twice rather than storing it twice.
    if key:
        with SessionLocal() as db:
            existing = pending.find_existing(db, key)
            if existing is not None:
                return JSONResponse(
                    status_code=202,
                    content={"results": [], "queued": True, "pending_id": str(existing.id),
                             "state": existing.state, "duplicate": True},
                )

    try:
        response = get_memory_instance().add(messages=messages, **params)
        if response.get("results"):
            telemetry.log_dashboard_nudge_once(DASHBOARD_URL)
        return JSONResponse(content=response)
    except (ValueError, Mem0ValidationError) as e:
        raise _client_error(e)
    except Exception as error:  # noqa: BLE001 - the classification decides
        code, detail = _classify(error)
        if code not in pending.QUEUEABLE_CODES:
            # A malformed write, or something that says nothing about the
            # provider. Queueing it would retry a guaranteed failure.
            raise upstream_error()
        text_content = "\n".join(m.get("content", "") for m in messages).strip()
        if not text_content:
            raise upstream_error()
        try:
            with SessionLocal() as db:
                row = pending.enqueue(
                    db, text=text_content,
                    payload={**params, "messages": messages},
                    idempotency_key=key, error=str(error), error_code=code,
                )
            pending.breaker.trip(code, detail)
            pending_worker.wake()
        except Exception:  # noqa: BLE001 - could not queue either; the client keeps it
            raise upstream_error()
        return JSONResponse(
            status_code=202,
            content={"results": [], "queued": True, "pending_id": str(row.id),
                     "state": row.state, "reason": detail, "code": code},
        )


@app.get("/memories/pending", summary="The server-side embedding queue")
def list_pending(state: Optional[str] = None, limit: int = 100, _auth=Depends(verify_auth)):
    """Counts by state, and the rows themselves.

    Exists because a queued memory is one that cannot be found by searching for
    it. Without somewhere to see the backlog, an incomplete store looks exactly
    like a complete one.
    """
    with SessionLocal() as db:
        summary = pending.stats(db)
        query = select(PendingMemory).order_by(PendingMemory.created_at)
        if state:
            query = query.where(PendingMemory.state == state)
        rows = list(db.execute(query.limit(min(limit, 1000))).scalars())
        return {
            **summary,
            "returned": len(rows),
            "items": [
                {
                    "id": str(r.id),
                    "state": r.state,
                    "text": r.text,
                    "user_id": (r.payload or {}).get("user_id"),
                    "agent_id": (r.payload or {}).get("agent_id"),
                    "run_id": (r.payload or {}).get("run_id"),
                    "attempts": r.attempts,
                    "last_error": r.last_error,
                    "last_error_code": r.last_error_code,
                    "next_attempt_at": r.next_attempt_at.isoformat() if r.next_attempt_at else None,
                    "claimed_by": r.claimed_by,
                    # Exposed so the dashboard can tell "a worker is on it" from
                    # "a worker died holding it". Both read `embedding`, and the
                    # second one looks like progress if the lease is hidden.
                    "lease_until": r.lease_until.isoformat() if r.lease_until else None,
                    "content_hash": r.content_hash,
                    "idempotency_key": r.idempotency_key,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "source_created_at": r.source_created_at.isoformat() if r.source_created_at else None,
                }
                for r in rows
            ],
        }


@app.post("/memories/pending/retry", summary="Re-arm queued memories now")
def retry_pending(force: bool = False, _auth=Depends(require_admin)):
    """Move `error` and `dead` rows back to `pending`, ready for the next pass.

    The manual counterpart to the automatic sweep: for when a person has fixed
    the cause and does not want to wait out a backoff, or wants to give a
    dead-lettered write another go.

    **While the queue is parked this re-arms the rows and stops there.** It used
    to close the breaker and wake the worker unconditionally, which contradicted
    the banner the user was looking at: the page said "parked - next probe at
    20:30" and the button went straight back to the provider, pushed rows into
    `embedding`, and hit the same wall. Parked means parked.

    `force=true` is the explicit override, for a person who has actually fixed
    the cause - raised the quota, replaced the key - and wants a probe now
    rather than at the scheduled time.
    """
    with SessionLocal() as db:
        result = db.execute(
            update(PendingMemory)
            .where(PendingMemory.state.in_([pending.ERROR, pending.DEAD]))
            .values(state=pending.PENDING, next_attempt_at=datetime.now(timezone.utc), attempts=0)
        )
        db.commit()
        rearmed = int(result.rowcount or 0)
        summary = pending.stats(db)

    parked = pending.breaker.is_open
    probing = force or not parked
    if probing:
        pending.breaker.close()
        pending_worker.wake()

    return {
        **summary,
        "rearmed": rearmed,
        # Say what actually happened. "Re-armed 12" next to a parked banner is
        # ambiguous about whether anything will be tried.
        "attempting": probing,
        "note": (
            f"Re-armed {rearmed} row(s). The queue is parked, so nothing will be sent to the "
            f"provider until the next probe. Use force=true if the cause is fixed."
            if parked and not probing
            else f"Re-armed {rearmed} row(s); the worker is running now."
        ),
    }


@app.delete("/memories/pending/{pending_id}", summary="Drop one queued memory")
def delete_pending(pending_id: str, _auth=Depends(require_admin)):
    """Discard a queued write. The only way one is ever removed unstored."""
    with SessionLocal() as db:
        row = db.execute(select(PendingMemory).where(PendingMemory.id == pending_id)).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="No such queued memory.")
        db.delete(row)
        db.commit()
    return {"deleted": pending_id}


ALL_MEMORIES_LIMIT = 1000
_RESERVED_PAYLOAD_KEYS = {"data", "user_id", "agent_id", "run_id", "hash", "created_at", "updated_at", "expiration_date"}


def _serialize_memory(row: Any) -> Dict[str, Any]:
    payload = getattr(row, "payload", None) or {}
    return {
        "id": getattr(row, "id", None),
        "memory": payload.get("data"),
        "user_id": payload.get("user_id"),
        "agent_id": payload.get("agent_id"),
        "run_id": payload.get("run_id"),
        "hash": payload.get("hash"),
        "expiration_date": payload.get("expiration_date"),
        "metadata": {k: v for k, v in payload.items() if k not in _RESERVED_PAYLOAD_KEYS},
        "created_at": payload.get("created_at"),
        "updated_at": payload.get("updated_at"),
    }


def _list_all_memories(limit: int = ALL_MEMORIES_LIMIT) -> Dict[str, Any]:
    results = get_memory_instance().vector_store.list(top_k=limit)
    rows = results[0] if results and isinstance(results, list) and isinstance(results[0], list) else results or []
    return {"results": [_serialize_memory(row) for row in rows]}


@app.get("/memories", summary="Get memories")
def get_all_memories(
    request: Request,
    user_id: Optional[str] = None,
    run_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    top_k: Optional[int] = Query(None, ge=0, le=ALL_MEMORIES_LIMIT),
    show_expired: bool = Query(False),
    _auth=Depends(verify_auth),
):
    """Retrieve stored memories. Lists all memories when no identifier is provided (admin only)."""
    try:
        if not any([user_id, run_id, agent_id]):
            auth_type = getattr(request.state, "auth_type", "none")
            if _auth is not None and _auth.role != "admin" and auth_type not in {"admin_api_key", "disabled"}:
                raise HTTPException(status_code=403, detail="Admin role required to list all memories.")
            # Admin all-memory listing is intentionally raw; scoped get_all below applies expiry visibility.
            return _list_all_memories(limit=top_k if top_k is not None else ALL_MEMORIES_LIMIT)
        filters = {
            k: v for k, v in {"user_id": user_id, "run_id": run_id, "agent_id": agent_id}.items() if v
        }
        params = {"filters": filters}
        if top_k is not None:
            params["top_k"] = top_k
        params["show_expired"] = show_expired
        return get_memory_instance().get_all(**params)
    except HTTPException:
        raise
    except Exception:
        raise upstream_error()


@app.get("/memories/{memory_id}", summary="Get a memory")
def get_memory(memory_id: str, _auth=Depends(verify_auth)):
    """Retrieve a specific memory by ID."""
    try:
        return get_memory_instance().get(memory_id)
    except Exception:
        raise upstream_error()


@app.post("/search", summary="Search memories")
def search_memories(search_req: SearchRequest, _auth=Depends(verify_auth)):
    """Search for memories based on a query."""
    try:
        filters = search_req.filters or {}
        deprecated_keys = []
        for entity_key in ("user_id", "agent_id", "run_id"):
            entity_val = getattr(search_req, entity_key, None)
            if entity_val:
                filters[entity_key] = entity_val
                deprecated_keys.append(entity_key)
        if deprecated_keys:
            logging.warning(
                "Top-level %s in /search is deprecated. Use filters={%s} instead.",
                ", ".join(deprecated_keys),
                ", ".join(f'"{k}": "..."' for k in deprecated_keys),
            )
        params = {}
        if search_req.top_k is not None:
            params["top_k"] = search_req.top_k
        if search_req.threshold is not None:
            params["threshold"] = search_req.threshold
        if search_req.explain is not None:
            params["explain"] = search_req.explain
        if search_req.show_expired is not None:
            params["show_expired"] = search_req.show_expired
        return get_memory_instance().search(query=search_req.query, filters=filters, **params)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception:
        raise upstream_error()


@app.put("/memories/{memory_id}", summary="Update a memory")
def update_memory(memory_id: str, updated_memory: MemoryUpdate, _auth=Depends(verify_auth)):
    """Update an existing memory."""
    try:
        fields_set = getattr(updated_memory, "model_fields_set", getattr(updated_memory, "__fields_set__", set()))
        params = {"memory_id": memory_id}
        if "text" in fields_set:
            params["data"] = updated_memory.text
        if "metadata" in fields_set:
            params["metadata"] = updated_memory.metadata
        if "expiration_date" in fields_set:
            params["expiration_date"] = updated_memory.expiration_date
        return get_memory_instance().update(**params)
    except (ValueError, Mem0ValidationError) as e:
        raise _client_error(e)
    except Exception:
        raise upstream_error()


@app.get("/memories/{memory_id}/history", summary="Get memory history")
def memory_history(memory_id: str, _auth=Depends(verify_auth)):
    """Retrieve memory history."""
    try:
        return get_memory_instance().history(memory_id=memory_id)
    except Exception:
        raise upstream_error()


@app.delete("/memories/{memory_id}", summary="Delete a memory", response_model=MessageResponse)
def delete_memory(memory_id: str, _auth=Depends(verify_auth)):
    """Delete a specific memory by ID."""
    try:
        get_memory_instance().delete(memory_id=memory_id)
        return MessageResponse(message="Memory deleted successfully")
    except (ValueError, Mem0ValidationError) as e:
        raise _client_error(e)
    except Exception:
        raise upstream_error()


@app.delete("/memories", summary="Delete all memories", response_model=MessageResponse)
def delete_all_memories(
    user_id: Optional[str] = None,
    run_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    _auth=Depends(require_admin),
):
    """Delete all memories for a given identifier. Requires admin role."""
    if not any([user_id, run_id, agent_id]):
        raise HTTPException(status_code=400, detail="At least one identifier is required.")
    try:
        params = {
            k: v for k, v in {"user_id": user_id, "run_id": run_id, "agent_id": agent_id}.items() if v
        }
        get_memory_instance().delete_all(**params)
        return MessageResponse(message="All relevant memories deleted")
    except Exception:
        raise upstream_error()


@app.post("/reset", summary="Reset all memories")
def reset_memory(_auth=Depends(require_admin)):
    """Completely reset stored memories. Requires admin role."""
    try:
        get_memory_instance().reset()
        return {"message": "All memories reset"}
    except Exception:
        raise upstream_error()


@app.get("/", summary="Redirect to the OpenAPI documentation", include_in_schema=False)
def home():
    """Redirect to the OpenAPI documentation."""
    return RedirectResponse(url="/docs")
