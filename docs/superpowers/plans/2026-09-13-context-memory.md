# Context Memory (self-hosted Mem0 + Context Manager) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a self-hosted Mem0 service in this fork and build a Context Manager on top of it that selects — not maximises — the instructions, repository truth, current task state and long-term memories injected into an agent's context.

**Architecture:** Four context *layers* (instructions, repository, current state, memory) are produced by independent `ContextSource` adapters into a single `ContextItem` type. A pipeline then ranks (multi-signal, per-component observable), deduplicates, resolves precedence conflicts, applies a token budget with per-layer floors, and compiles a final markdown context with provenance. Mem0 is one source among four, reached through a `MemoryProvider` interface so it can be swapped or benchmarked against alternatives.

**Tech Stack:** Python 3.11, `httpx` (already a `mem0ai` dependency), pytest, self-hosted Mem0 FastAPI server + `pgvector/pgvector:pg17` via Docker Compose, Gemini for both LLM and embedder.

> **STATUS (2026-09-13):** Phases 1-4 implemented. 59 of 74 plan steps done;
> `174 passed` on `.venv/Scripts/python.exe -m pytest tests/context_memory -q` with
> `MEM0_API_URL`/`MEM0_API_KEY`/`MEM0_USER` set.
> Remaining unchecked: the 15 `Commit` steps (nothing has been committed - the repo
> is on `main` and awaiting the user's call), plus Step 1.5 and Step 4.5, which are
> the user supplied `GOOGLE_API_KEY`, so Step 1.5 and Step 4.5 now pass:
> `174 passed` with the integration tests running rather than skipping.
> See **Execution log** at the end
> for what execution turned up that the plan did not predict.

**Source spec:** `C:\Users\Witbor\Desktop\mem0_self_hosted_context_memory_handoff_v2.md`

**Scope of this plan:** Handoff phases 1-4. Phases 5 (memory extraction), 6 (evaluation harness) and section 19 (general `CLAUDE.md` refactor) are specified at the end as follow-on work, not executed here.

---

## Measured environment facts

Everything below was observed in this session, not assumed. Re-measure before trusting any of it after the revision changes.

| Fact | Evidence |
|---|---|
| Repo is a fork of the upstream `mem0` monorepo, branch `main`, clean | `git status` at session start |
| Docker 29.7.2, Compose v5.4.0 installed; daemon was **not** running | `docker --version`, `docker compose version`, `docker info` failed with `npipe:////./pipe/dockerDesktopLinuxEngine ... The system cannot find the file specified` |
| Docker Desktop lives at `C:\Users\Witbor\AppData\Local\Programs\DockerDesktop\Docker Desktop.exe`, not the default `Program Files` path | `Get-Command docker` plus path probe |
| Python 3.11.4, `httpx` 0.28.1 and `tiktoken` importable; **`pytest` is not installed** | `python -c "import ..."` |
| `pnpm` 11.0.8, node v24.11.0 | `pnpm --version`, `node --version` |
| Bundled providers | `server/main.py:62-63` - `BUNDLED_LLM_PROVIDERS = ("openai", "anthropic", "gemini")`, `BUNDLED_EMBEDDER_PROVIDERS = ("openai", "gemini")` |
| Default provider is hardcoded `openai`; only the *model* is env-driven | `server/main.py:117-118` (`MEM0_DEFAULT_LLM_MODEL`, `MEM0_DEFAULT_EMBEDDER_MODEL`), `server/main.py:134-137` |
| The compose stack is `mem0` + `postgres` (pgvector/pgvector:pg17) + `mem0-dashboard`. **There is no Neo4j service.** | `server/docker-compose.yaml`; `grep -i neo4j` returns nothing in compose or `server/requirements.txt` |
| `server/Makefile:9` `up` target uses `lsof`, which does not exist on Windows | `server/Makefile` |
| API surface, to be re-verified against the live `/openapi.json` in Task 1.4 | `server/main.py:322-557` and `server/routers/*.py` |
| `server/.env` and `.venv` are already gitignored | `.gitignore:9,131,132` |
| Root pytest is configured with `pythonpath = ["."]` | `pyproject.toml [tool.pytest.ini_options]` |

### Deviations from the handoff, and why

1. **No Neo4j.** Handoff section 9 draws PostgreSQL/pgvector *and* Neo4j under the Mem0 API. The self-hosted stack this repo actually ships has only pgvector. Graph memory would need a `neo4j` service, `mem0ai[graph]` deps, and an image rebuild. Phases 1-4 do not need it. **Not added.** Revisit if entity-relationship retrieval becomes a measured need.
2. **No `OPENAI_API_KEY`.** Gemini is used for both LLM and embedder - the only bundled provider supplying both, since Anthropic ships no embedding model. This requires a change to `server/main.py` so the *provider*, not just the model, is env-driven.
3. **`make up` is unusable on Windows** (`lsof`). `docker compose` is invoked directly and `scripts/stack.ps1` replaces the Makefile ergonomics.
4. **Plan code granularity.** Test code and every type/interface signature are given in full below - the tests are the contract. Implementation bodies are specified by behaviour plus algorithm where they are mechanical. Write them to satisfy the given tests, and do not add public surface the tests do not exercise.

---

## File structure

```
src/                                   new package root (name fixed by handoff section 17)
├── __init__.py
├── memory/
│   ├── __init__.py
│   ├── types.py            MemoryRecord, MemoryScope, Lifecycle, SearchQuery - dataclasses only
│   ├── base.py             MemoryProvider ABC: add/search/get/get_all/update/delete/reset/history
│   ├── envelope.py         metadata envelope encode/decode (scope, lifecycle, confidence, ...)
│   ├── scopes.py           scope vocabulary + Mem0 identifier mapping
│   ├── lifecycle.py        candidate -> durable -> stale -> superseded transitions
│   └── mem0_provider.py    Mem0Provider, httpx client against the self-hosted REST API
├── context/
│   ├── __init__.py
│   ├── types.py            Layer, Task, ContextItem, Signals, ScoreBreakdown, reports
│   ├── chunking.py         heading-aware markdown chunker, shared by the file sources
│   ├── lexical.py          BM25-lite scorer + tokenizer, no network, no embeddings
│   ├── tokens.py           TokenCounter protocol, heuristic and tiktoken implementations
│   ├── ranker.py           ContextRanker: Signals -> ScoreBreakdown, weights injectable
│   ├── dedupe.py           Deduplicator: shingle Jaccard, keeps the best-scoring item
│   ├── conflicts.py        precedence ladder + ConflictResolver rules
│   ├── budget.py           ContextBudget: per-layer floors, global cap, BudgetReport
│   ├── compiler.py         ContextCompiler: ordered markdown + provenance
│   ├── manager.py          ContextManager: wires sources -> pipeline -> CompiledContext
│   └── sources/
│       ├── __init__.py
│       ├── base.py         ContextSource ABC
│       ├── instructions.py CLAUDE.md / AGENTS.md / rules / SKILL.md, nearest-first
│       ├── repository.py   README, docs/, manifests, keyword-matched files
│       ├── current_state.py git branch/status/diff/log, env-scrubbed, NUL-delimited
│       └── memory.py       MemoryContextSource, wraps a MemoryProvider
├── scripts/
│   ├── context_build.py    CLI: build a context for a task, print it or its report
│   ├── memory_store.py     CLI: store a memory with scope + lifecycle
│   ├── memory_search.py    CLI: scoped search
│   └── memory_promote.py   CLI: lifecycle transitions
skills/context-memory/
├── SKILL.md
├── references/
│   ├── memory-policy.md
│   ├── retrieval-policy.md
│   ├── context-precedence.md
│   └── evaluation.md
└── scripts/                thin wrappers that shell into src/scripts/*
tests/context_memory/
├── conftest.py
├── test_envelope.py
├── test_scopes.py
├── test_lifecycle.py
├── test_mem0_provider.py
├── test_integration_mem0.py   marked `integration`, skipped without MEM0_API_URL
├── test_chunking.py
├── test_lexical.py
├── test_tokens.py
├── test_source_instructions.py
├── test_source_repository.py
├── test_source_current_state.py
├── test_source_memory.py
├── test_ranker.py
├── test_dedupe.py
├── test_conflicts.py
├── test_budget.py
├── test_compiler.py
└── test_manager.py
server/
├── main.py                 MODIFY: env-driven llm/embedder provider
└── .env                    CREATE (gitignored)
scripts/stack.ps1           CREATE: Windows-friendly up/down/health/logs
```

Modified upstream files are limited to `server/main.py` and `server/.env.example`. Everything else is additive, so rebasing on upstream `mem0` stays cheap.

---

# Phase 1 - Infrastructure

## Task 1: Make the server provider env-driven and bring the stack up on Gemini

**Files:**
- Modify: `server/main.py:115-138`
- Modify: `server/.env.example`
- Create: `server/.env` (gitignored)
- Create: `scripts/stack.ps1`

- [x] **Step 1.1: Add provider env vars to `server/main.py`**

Replace the block at `server/main.py:115-138` with:

```python
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
HISTORY_DB_PATH = os.environ.get("HISTORY_DB_PATH", "/app/history/history.db")
DEFAULT_LLM_PROVIDER = os.environ.get("MEM0_DEFAULT_LLM_PROVIDER", "openai")
DEFAULT_EMBEDDER_PROVIDER = os.environ.get("MEM0_DEFAULT_EMBEDDER_PROVIDER", "openai")
DEFAULT_LLM_MODEL = os.environ.get("MEM0_DEFAULT_LLM_MODEL", "gpt-5-mini")
DEFAULT_EMBEDDER_MODEL = os.environ.get("MEM0_DEFAULT_EMBEDDER_MODEL", "text-embedding-3-small")

_PROVIDER_KEYS = {
    "openai": OPENAI_API_KEY,
    "anthropic": ANTHROPIC_API_KEY,
    "gemini": GOOGLE_API_KEY,
}


def _provider_api_key(provider: str) -> str | None:
    """Fail loudly on an unknown provider name rather than silently sending no key."""
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
        },
    },
    "history_db_path": HISTORY_DB_PATH,
}
```

Rationale for the raise: an unknown provider name previously would have produced a config with no `api_key` and failed later, at first use, as an opaque upstream 500. This is the silent-failure direction; make it loud at boot.

- [x] **Step 1.2: Document the new vars in `server/.env.example`**

Under the existing model defaults comment, add:

```
# Provider for the default LLM and embedder. Must be in BUNDLED_*_PROVIDERS (server/main.py).
# Note: anthropic is LLM-only - it ships no embedding model. gemini covers both.
MEM0_DEFAULT_LLM_PROVIDER=openai
MEM0_DEFAULT_EMBEDDER_PROVIDER=openai
```

- [x] **Step 1.3: Create `server/.env`**

`POSTGRES_PASSWORD`, `JWT_SECRET` and `ADMIN_API_KEY` are generated locally; `GOOGLE_API_KEY` comes from the user.

```
GOOGLE_API_KEY=<user supplied>
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=postgres
POSTGRES_USER=postgres
POSTGRES_PASSWORD=<generated>
POSTGRES_COLLECTION_NAME=memories
ADMIN_API_KEY=<generated, >= 32 chars>
JWT_SECRET=<generated>
AUTH_DISABLED=false
DASHBOARD_URL=http://localhost:3000
APP_DB_NAME=mem0_app
MEM0_DEFAULT_LLM_PROVIDER=gemini
MEM0_DEFAULT_EMBEDDER_PROVIDER=gemini
MEM0_DEFAULT_LLM_MODEL=gemini-2.5-flash
MEM0_DEFAULT_EMBEDDER_MODEL=models/text-embedding-004
MEM0_TELEMETRY=false
REQUEST_LOG_RETENTION_DAYS=30
```

Model ids are a claim about the Gemini API, not a measurement. Step 1.5 verifies them by round-tripping a real memory; if either 404s, list the available models and pin a real one before continuing.

- [x] **Step 1.4: Bring the stack up and capture the real OpenAPI**

`server/Makefile`'s `up` target uses `lsof` and cannot run here. Run compose directly:

```bash
cd server && docker compose up -d --build
```

Then poll until the API answers and dump the spec:

```bash
curl -fsS http://localhost:8888/auth/setup-status
curl -fsS http://localhost:8888/openapi.json -o "$SCRATCH/openapi.json"
python -c "import json;d=json.load(open(r'$SCRATCH/openapi.json'));[print(m.upper(),p) for p,o in sorted(d['paths'].items()) for m in o]"
```

Expected: the printed path list contains `POST /memories`, `GET /memories`, `GET /memories/{memory_id}`, `POST /search`, `PUT /memories/{memory_id}`, `GET /memories/{memory_id}/history`, `DELETE /memories/{memory_id}`, `DELETE /memories`, `POST /reset`, `GET /configure`, `POST /configure`, `GET /configure/providers`.

Record the actual output in the task log. If it differs from `server/main.py:322-557`, the live spec wins and Task 4 is written against it.

- [x] **Step 1.5: Prove the Gemini config actually round-trips**

```bash
curl -fsS -X POST http://localhost:8888/memories \
  -H "X-API-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"The context manager stores memories under scope keys, not raw user ids."}],"user_id":"smoke","metadata":{"probe":"phase1"}}'
curl -fsS -X POST http://localhost:8888/search \
  -H "X-API-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"query":"how are memories scoped","filters":{"user_id":"smoke"},"top_k":5}'
```

Expected: the add returns a JSON body with a non-empty `results` array, and the search returns at least the memory just stored. An empty `results` on add means fact extraction produced nothing - that is a *silent* failure of the LLM leg, so treat an empty array as a failure of this step, not a pass. Exit code 0 is not the check here; the content is.

Clean up: `curl -X DELETE "http://localhost:8888/memories?user_id=smoke" -H "X-API-Key: $ADMIN_API_KEY"`.

- [x] **Step 1.6: Write `scripts/stack.ps1`**

Commands `up`, `down`, `logs`, `health`, `reset`. `health` must print the API status code, the dashboard status code, and `pg_isready`, mirroring `server/Makefile:47-51` without `lsof`.

- [ ] **Step 1.7: Commit**

```bash
git add server/main.py server/.env.example scripts/stack.ps1
git commit -m "feat(server): env-driven llm/embedder provider + windows stack script"
```

## Task 2: Dev environment and package skeleton

**Files:**
- Create: `.venv/` (gitignored), `src/__init__.py`, `src/memory/__init__.py`, `src/context/__init__.py`, `src/context/sources/__init__.py`, `tests/context_memory/conftest.py`
- Create: `requirements-context.txt`

- [x] **Step 2.1: Create the venv and install test deps**

`pytest` is not installed in the system Python (measured). Do not install into it.

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install --upgrade pip
.venv/Scripts/python.exe -m pip install -r requirements-context.txt
```

`requirements-context.txt`:

```
httpx>=0.28.0
pytest>=8.2.2
pytest-asyncio>=0.23.7
```

`tiktoken` is deliberately absent: `src/context/tokens.py` uses it only when importable and falls back to a heuristic counter otherwise. Making it a hard dependency would tie the budget to an OpenAI tokenizer while the stack runs on Gemini.

- [x] **Step 2.2: Register the test marker and path**

Append to `pyproject.toml` under `[tool.pytest.ini_options]`:

```toml
markers = [
    "integration: hits a live Mem0 server; skipped unless MEM0_API_URL is set",
]
```

- [x] **Step 2.3: Verify the harness runs**

```bash
.venv/Scripts/python.exe -m pytest tests/context_memory -q
```

Expected: `no tests ran` (exit code 5), not an import or config error.

- [ ] **Step 2.4: Commit**

```bash
git add src tests/context_memory requirements-context.txt pyproject.toml
git commit -m "chore(context): package skeleton and test harness"
```

---

# Phase 2 - Memory model and lifecycle

## Task 3: Memory types, scopes and the metadata envelope

**Files:**
- Create: `src/memory/types.py`, `src/memory/scopes.py`, `src/memory/envelope.py`
- Test: `tests/context_memory/test_scopes.py`, `tests/context_memory/test_envelope.py`

### Design

Mem0 gives three first-class identifiers (`user_id`, `agent_id`, `run_id`) plus free-form `metadata` which lands in the pgvector payload (`server/main.py:388-402`: everything outside the reserved key set is returned as `metadata`). The seven conceptual scopes from handoff section 8 map onto that as:

| Concept | Carrier | Example |
|---|---|---|
| user | `user_id` | `lautaro` |
| project / repository | `agent_id` | `repo:memory-optimization` |
| session / task / branch | `run_id` | `task:2026-09-13-context-memory` |
| scope level itself | `metadata["scope"]` | `repository` |
| scope key | `metadata["scope_key"]` | `memory-optimization` |

`user_id` is always set, because `POST /memories` rejects a payload with none of the three (`server/main.py:370-372`).

**Scope is a closed vocabulary.** An unrecognised scope must raise, not pass through - a typo'd scope silently writes memories that no query will ever match, which is the failure direction that looks exactly like "no memories yet".

- [x] **Step 3.1: Write the failing tests**

`tests/context_memory/test_scopes.py`:

```python
import pytest

from src.memory.scopes import SCOPES, Scope, ScopeSelector, scope_identifiers


def test_scope_vocabulary_is_exactly_the_seven_documented_levels():
    # Spelled out independently of the implementation's own constant on purpose:
    # iterating SCOPES to build this set would pass by construction.
    assert set(SCOPES) == {
        "global",
        "user",
        "project",
        "repository",
        "branch",
        "session",
        "task",
    }


def test_scope_is_ordered_from_broad_to_narrow():
    assert [s.value for s in Scope.ordered()] == [
        "global",
        "user",
        "project",
        "repository",
        "branch",
        "session",
        "task",
    ]


def test_unknown_scope_raises():
    with pytest.raises(ValueError, match="unknown scope"):
        Scope.parse("repositry")


def test_scope_identifiers_always_populate_user_id():
    # POST /memories rejects a body with no user_id/agent_id/run_id.
    ids = scope_identifiers(Scope.GLOBAL, key="engineering", user="lautaro")
    assert ids["user_id"] == "lautaro"


def test_repository_scope_lands_on_agent_id():
    ids = scope_identifiers(Scope.REPOSITORY, key="memory-optimization", user="lautaro")
    assert ids["agent_id"] == "repo:memory-optimization"
    assert "run_id" not in ids


def test_task_scope_lands_on_run_id_and_keeps_the_repository():
    ids = scope_identifiers(
        Scope.TASK, key="2026-09-13-context-memory", user="lautaro", repository="memory-optimization"
    )
    assert ids["run_id"] == "task:2026-09-13-context-memory"
    assert ids["agent_id"] == "repo:memory-optimization"


def test_selector_widens_from_narrow_to_broad():
    selector = ScopeSelector(user="lautaro", project="gestion", repository="memory-optimization", branch="main")
    assert selector.scopes_to_query() == [
        Scope.TASK,
        Scope.SESSION,
        Scope.BRANCH,
        Scope.REPOSITORY,
        Scope.PROJECT,
        Scope.USER,
        Scope.GLOBAL,
    ]


def test_selector_skips_scopes_with_no_key():
    selector = ScopeSelector(user="lautaro", repository="memory-optimization")
    assert Scope.PROJECT not in selector.scopes_to_query()
    assert Scope.GLOBAL in selector.scopes_to_query()
```

`tests/context_memory/test_envelope.py`:

```python
from datetime import datetime, timezone

import pytest

from src.memory.envelope import Envelope, decode_envelope, encode_envelope
from src.memory.scopes import Scope


def _envelope(**overrides) -> Envelope:
    base = dict(
        scope=Scope.REPOSITORY,
        scope_key="memory-optimization",
        kind="discovery",
        lifecycle="durable",
        confidence=0.8,
        importance=0.6,
        topic="server.default_provider",
        source="session:2026-09-13",
        created_at=datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return Envelope(**base)


def test_encode_produces_flat_json_safe_metadata():
    encoded = encode_envelope(_envelope())
    assert encoded["scope"] == "repository"
    assert encoded["lifecycle"] == "durable"
    assert encoded["confidence"] == 0.8
    assert encoded["created_at"] == "2026-09-13T12:00:00+00:00"
    assert all(isinstance(v, (str, int, float, bool)) for v in encoded.values())


def test_round_trip_is_lossless():
    original = _envelope(tags=("provider", "gemini"))
    assert decode_envelope(encode_envelope(original)) == original


def test_tags_survive_as_one_key_per_member_not_a_delimited_blob():
    # A delimited multi-value would make `tags contains gemini` match only rows whose
    # tag list was exactly "provider|gemini".
    encoded = encode_envelope(_envelope(tags=("provider", "gemini")))
    assert encoded["tag_provider"] is True
    assert encoded["tag_gemini"] is True
    assert "tags" not in encoded


def test_decode_tolerates_foreign_metadata():
    decoded = decode_envelope({"scope": "global", "scope_key": "eng", "written_by": "someone else"})
    assert decoded.scope is Scope.GLOBAL
    assert decoded.extra == {"written_by": "someone else"}


def test_decode_rejects_an_unknown_lifecycle():
    with pytest.raises(ValueError, match="unknown lifecycle"):
        decode_envelope({"scope": "global", "scope_key": "eng", "lifecycle": "archived"})


def test_confidence_and_importance_are_clamped_to_unit_interval():
    with pytest.raises(ValueError, match="confidence"):
        _envelope(confidence=1.4)
```

- [x] **Step 3.2: Run them and watch them fail**

```bash
.venv/Scripts/python.exe -m pytest tests/context_memory/test_scopes.py tests/context_memory/test_envelope.py -q
```

Expected: collection errors, `ModuleNotFoundError: No module named 'src.memory.scopes'`.

- [x] **Step 3.3: Implement `src/memory/scopes.py` and `src/memory/envelope.py`**

`Scope` is a `str`-valued `Enum` with `ordered()` broad-to-narrow and `parse()` raising `ValueError("unknown scope: ...")`. `scope_identifiers()` returns a dict containing `user_id` always, `agent_id` when a project/repository is known, `run_id` for branch/session/task scopes. `ScopeSelector` holds the optional keys and returns the narrow-to-broad list of scopes that actually have a key.

`Envelope` is a frozen dataclass with the fields exercised above plus `tags: tuple[str, ...] = ()`, `superseded_by: str | None = None` and `extra: dict = field(default_factory=dict)`. `__post_init__` validates the unit-interval fields. `encode_envelope` flattens tags to one boolean key each (`tag_<name>`) - the cardinality rule: a set stored as a delimited string cannot be queried by membership. `decode_envelope` puts unrecognised keys in `extra` rather than dropping them.

- [x] **Step 3.4: Run to green**

```bash
.venv/Scripts/python.exe -m pytest tests/context_memory/test_scopes.py tests/context_memory/test_envelope.py -q
```

Expected: `13 passed`.

- [ ] **Step 3.5: Commit**

```bash
git add src/memory tests/context_memory
git commit -m "feat(memory): scope vocabulary and metadata envelope"
```

## Task 4: MemoryProvider interface and Mem0Provider

**Files:**
- Create: `src/memory/base.py`, `src/memory/mem0_provider.py`
- Test: `tests/context_memory/test_mem0_provider.py`, `tests/context_memory/test_integration_mem0.py`

### Design

`MemoryProvider` is an ABC with `add`, `search`, `get`, `get_all`, `update`, `delete`, `delete_all`, `history`, `reset`. It speaks `MemoryRecord` / `SearchQuery`, never raw JSON, so a second provider can be dropped in for benchmarking (handoff section 10).

`Mem0Provider` wraps `httpx.Client`. Auth is `X-API-Key` (`server/auth.py`). Unit tests drive it through `httpx.MockTransport` so no server is needed; a separate `integration` test hits the real one.

- [x] **Step 4.1: Write the failing unit tests**

`tests/context_memory/test_mem0_provider.py`:

```python
import json

import httpx
import pytest

from src.memory.mem0_provider import Mem0Provider
from src.memory.scopes import Scope
from src.memory.types import MemoryRecord, SearchQuery


def make_provider(handler):
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="http://mem0.test")
    return Mem0Provider(client=client, api_key="k", user="lautaro")


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
        {"role": "user", "content": "Gemini is the only bundled provider that supplies both llm and embedder."}
    ]
    assert seen["body"]["user_id"] == "lautaro"
    assert seen["body"]["agent_id"] == "repo:memory-optimization"
    assert seen["body"]["metadata"]["scope"] == "repository"
    assert seen["body"]["metadata"]["lifecycle"] == "candidate"
    assert [r.id for r in records] == ["m1"]


def test_add_defaults_infer_to_false_for_verbatim_facts():
    # A curated memory must be stored as written. Letting the LLM re-extract it
    # can drop or reword the fact, and the loss is silent.
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"results": []})

    make_provider(handler).add("a verbatim fact", scope=Scope.GLOBAL, scope_key="eng", infer=False)
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


def test_get_all_pages_through_top_k_and_reports_the_denominator():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"id": f"m{i}", "memory": "x"} for i in range(3)]})

    page = make_provider(handler).get_all(scope=Scope.GLOBAL, scope_key="eng", top_k=3)
    assert page.returned == 3
    assert page.limit == 3
    assert page.truncated is True  # returned == limit, so there may be more


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
```

`tests/context_memory/test_integration_mem0.py`:

```python
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
        infer=False,
    )
    results = provider.search(SearchQuery(query="where does the offline harness read data from", scope=Scope.TASK, scope_key=key, top_k=5))
    try:
        assert results, "live server returned no results for a memory just written"
        assert "snapshot" in results[0].text.lower()
    finally:
        provider.delete_all(scope=Scope.TASK, scope_key=key)
```

- [x] **Step 4.2: Run and watch them fail**

```bash
.venv/Scripts/python.exe -m pytest tests/context_memory/test_mem0_provider.py -q
```

Expected: `ModuleNotFoundError: No module named 'src.memory.mem0_provider'`.

- [x] **Step 4.3: Implement `src/memory/types.py`, `src/memory/base.py`, `src/memory/mem0_provider.py`**

`types.py`: frozen dataclasses `MemoryRecord(id, text, envelope, score=None, created_at=None, updated_at=None, raw=None)`, `SearchQuery(query, scope, scope_key, top_k=10, threshold=None, lifecycles=("candidate","durable"), repository=None)`, `MemoryPage(records, limit, returned, truncated)`.

`base.py`: the ABC. Every method returns the dataclasses above.

`mem0_provider.py`: `Mem0Provider(client, api_key, user, repository=None)` plus `from_env()` reading `MEM0_API_URL`, `MEM0_API_KEY`, `MEM0_USER`, `MEM0_REPOSITORY`. One private `_request` that raises `RuntimeError` carrying `detail` from the JSON body on a non-2xx. `add()` refuses to return silently on `results == []` when `infer` is true.

- [x] **Step 4.4: Run to green**

```bash
.venv/Scripts/python.exe -m pytest tests/context_memory/test_mem0_provider.py -q
```

Expected: `9 passed`.

- [x] **Step 4.5: Run the integration test against the live stack**

```bash
MEM0_API_URL=http://localhost:8888 MEM0_API_KEY=<admin key> MEM0_USER=lautaro \
  .venv/Scripts/python.exe -m pytest tests/context_memory/test_integration_mem0.py -q -m integration
```

Expected: `1 passed`. A skip here is a failure of this step, not a pass - it means the env vars did not reach pytest.

- [ ] **Step 4.6: Commit**

```bash
git add src/memory tests/context_memory
git commit -m "feat(memory): MemoryProvider interface and Mem0 REST provider"
```

## Task 5: Lifecycle state machine

**Files:**
- Create: `src/memory/lifecycle.py`
- Test: `tests/context_memory/test_lifecycle.py`

### Design

Handoff section 18 phase 2: `candidate -> durable -> stale -> superseded`. Encoded as an explicit transition table, because an implicit one lets any state reach any other and the illegal transition then shows up as a data bug months later.

Legal transitions:

```
candidate  -> durable, stale, superseded
durable    -> stale, superseded
stale      -> durable        (revalidated), superseded
superseded -> (terminal)
```

- [x] **Step 5.1: Write the failing tests**

`tests/context_memory/test_lifecycle.py`:

```python
import pytest

from src.memory.lifecycle import LIFECYCLES, Lifecycle, can_transition, transition


def test_lifecycle_vocabulary():
    assert set(LIFECYCLES) == {"candidate", "durable", "stale", "superseded"}


@pytest.mark.parametrize(
    "src,dst",
    [
        ("candidate", "durable"),
        ("candidate", "stale"),
        ("candidate", "superseded"),
        ("durable", "stale"),
        ("durable", "superseded"),
        ("stale", "durable"),
        ("stale", "superseded"),
    ],
)
def test_legal_transitions(src, dst):
    assert can_transition(Lifecycle(src), Lifecycle(dst)) is True


@pytest.mark.parametrize(
    "src,dst",
    [
        ("superseded", "durable"),
        ("superseded", "stale"),
        ("superseded", "candidate"),
        ("durable", "candidate"),
        ("stale", "candidate"),
    ],
)
def test_illegal_transitions(src, dst):
    assert can_transition(Lifecycle(src), Lifecycle(dst)) is False


def test_transition_returns_the_metadata_patch_to_write():
    patch = transition(Lifecycle.DURABLE, Lifecycle.SUPERSEDED, superseded_by="m9", at="2026-09-13T12:00:00+00:00")
    assert patch == {"lifecycle": "superseded", "superseded_by": "m9", "lifecycle_changed_at": "2026-09-13T12:00:00+00:00"}


def test_superseding_without_a_successor_id_raises():
    # A superseded memory with no pointer to what replaced it is unrecoverable context.
    with pytest.raises(ValueError, match="superseded_by"):
        transition(Lifecycle.DURABLE, Lifecycle.SUPERSEDED, at="2026-09-13T12:00:00+00:00")


def test_illegal_transition_raises_rather_than_writing():
    with pytest.raises(ValueError, match="cannot transition"):
        transition(Lifecycle.SUPERSEDED, Lifecycle.DURABLE, at="2026-09-13T12:00:00+00:00")
```

- [x] **Step 5.2: Run and watch fail.** Expected `ModuleNotFoundError`.
- [x] **Step 5.3: Implement `src/memory/lifecycle.py`** - `Lifecycle` str enum, `_ALLOWED: dict[Lifecycle, frozenset[Lifecycle]]`, `can_transition`, `transition` returning the metadata patch.
- [x] **Step 5.4: Run to green.** Expected `15 passed`.
- [ ] **Step 5.5: Commit**

```bash
git add src/memory/lifecycle.py tests/context_memory/test_lifecycle.py
git commit -m "feat(memory): explicit lifecycle transition table"
```

---

# Phase 3 - Context sources

## Task 6: Context types

**Files:**
- Create: `src/context/types.py`
- Test: covered indirectly; no dedicated test file - these are data holders with validation exercised by the ranker/budget tests.

- [x] **Step 6.1: Write `src/context/types.py`**

```python
"""Types shared by every context source and every pipeline stage.

`Signals` is deliberately a flat record of independent components rather than a
single score: handoff section 12 requires each component to be observable on its
own, so retrieval failures can be told apart from ranking failures.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum


class Layer(str, Enum):
    INSTRUCTIONS = "instructions"
    REPOSITORY = "repository"
    CURRENT_STATE = "current_state"
    MEMORY = "memory"

    @classmethod
    def precedence(cls) -> list["Layer"]:
        """Handoff section 13, highest authority first."""
        return [cls.INSTRUCTIONS, cls.CURRENT_STATE, cls.REPOSITORY, cls.MEMORY]


@dataclass(frozen=True)
class Task:
    description: str
    files: tuple[str, ...] = ()
    repository: str | None = None
    branch: str | None = None
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class Signals:
    relevance: float = 0.0
    task_scope_match: float = 0.0
    project_scope_match: float = 0.0
    confidence: float = 0.0
    recency: float = 0.0
    importance: float = 0.0
    redundancy: float = 0.0
    staleness: float = 0.0

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1], got {value}")


@dataclass(frozen=True)
class ScoreBreakdown:
    total: float
    contributions: dict[str, float]


@dataclass
class ContextItem:
    id: str
    layer: Layer
    content: str
    source: str
    title: str = ""
    signals: Signals = field(default_factory=Signals)
    breakdown: ScoreBreakdown | None = None
    tokens: int = 0
    created_at: datetime | None = None
    metadata: dict = field(default_factory=dict)
    pinned: bool = False

    def with_signals(self, **changes) -> "ContextItem":
        return replace(self, signals=replace(self.signals, **changes))
```

- [ ] **Step 6.2: Commit**

```bash
git add src/context/types.py
git commit -m "feat(context): shared context item and signal types"
```

## Task 7: Chunking, lexical scoring, token counting

**Files:**
- Create: `src/context/chunking.py`, `src/context/lexical.py`, `src/context/tokens.py`
- Test: `tests/context_memory/test_chunking.py`, `test_lexical.py`, `test_tokens.py`

### Design

The whole point of the Context Manager is selecting *parts* of documents. A `CLAUDE.md` injected whole defeats the exercise, so every file source chunks on markdown headings, carrying the heading path so provenance survives.

Relevance without embeddings: BM25 over the candidate set. That keeps phases 1-4 free of an embedding call per file chunk, and handoff section 12 explicitly says not to rely on embedding similarity alone. Memory items already arrive with the server's embedding score, which the ranker uses in place of the lexical score for that layer.

- [x] **Step 7.1: Write the failing tests**

`tests/context_memory/test_chunking.py`:

```python
from src.context.chunking import chunk_markdown

DOC = """# Title

Intro line.

## Section A

Alpha body.

### Section A.1

Nested body.

## Section B

Beta body.
"""


def test_chunks_split_on_headings():
    chunks = chunk_markdown(DOC, source="CLAUDE.md")
    assert [c.title for c in chunks] == ["Title", "Title > Section A", "Title > Section A > Section A.1", "Title > Section B"]


def test_chunk_content_excludes_deeper_sections():
    chunks = {c.title: c.content for c in chunk_markdown(DOC, source="CLAUDE.md")}
    assert "Nested body" not in chunks["Title > Section A"]
    assert "Alpha body" in chunks["Title > Section A"]


def test_chunk_ids_are_stable_and_unique_per_heading_path():
    ids = [c.id for c in chunk_markdown(DOC, source="CLAUDE.md")]
    assert len(set(ids)) == len(ids)
    assert ids == [c.id for c in chunk_markdown(DOC, source="CLAUDE.md")]


def test_two_files_with_identical_headings_get_distinct_ids():
    # Ids that share a generated prefix and differ only at the tail collapse into
    # one label when truncated; keep the distinguishing part in the id itself.
    a = chunk_markdown(DOC, source="a/CLAUDE.md")
    b = chunk_markdown(DOC, source="b/CLAUDE.md")
    assert set(c.id for c in a).isdisjoint(c.id for c in b)


def test_code_fences_do_not_start_a_new_chunk():
    doc = "## S\n\n```python\n# not a heading\n```\n\ntail\n"
    chunks = chunk_markdown(doc, source="x.md")
    assert len(chunks) == 1
    assert "# not a heading" in chunks[0].content


def test_a_document_with_no_headings_yields_one_chunk():
    chunks = chunk_markdown("just prose\nmore prose\n", source="x.md")
    assert len(chunks) == 1
    assert chunks[0].title == "x.md"


def test_oversized_sections_are_split_with_an_ordinal_suffix():
    doc = "## Big\n\n" + "\n\n".join(f"paragraph {i}" for i in range(200))
    chunks = chunk_markdown(doc, source="x.md", max_chars=400)
    assert len(chunks) > 1
    assert chunks[0].title.endswith("(1/%d)" % len(chunks))
```

`tests/context_memory/test_lexical.py`:

```python
from src.context.lexical import Bm25, tokenize


def test_tokenize_lowercases_and_keeps_identifiers_intact():
    assert tokenize("Set MEM0_DEFAULT_LLM_PROVIDER in server/main.py") == [
        "set",
        "mem0_default_llm_provider",
        "in",
        "server",
        "main",
        "py",
    ]


def test_bm25_ranks_the_document_containing_the_rare_term_first():
    docs = {
        "a": "the server reads configuration from the environment",
        "b": "the embedder provider is gemini",
        "c": "the server starts and the server stops",
    }
    scores = Bm25(docs).score("gemini embedder")
    assert max(scores, key=scores.get) == "b"


def test_a_term_present_everywhere_carries_almost_no_signal():
    docs = {"a": "server server", "b": "server", "c": "server"}
    scores = Bm25(docs).score("server")
    assert max(scores.values()) < 0.05


def test_scores_are_normalised_to_the_unit_interval():
    docs = {"a": "gemini gemini gemini", "b": "unrelated"}
    scores = Bm25(docs).score("gemini")
    assert 0.0 <= min(scores.values()) <= max(scores.values()) <= 1.0
    assert scores["a"] == 1.0


def test_an_empty_query_scores_everything_zero_rather_than_raising():
    scores = Bm25({"a": "x"}).score("")
    assert scores == {"a": 0.0}
```

`tests/context_memory/test_tokens.py`:

```python
from src.context.tokens import HeuristicTokenCounter, get_token_counter


def test_heuristic_counter_is_monotonic_in_length():
    counter = HeuristicTokenCounter()
    assert counter.count("a" * 400) > counter.count("a" * 100)


def test_heuristic_counter_never_returns_zero_for_non_empty_text():
    # A zero-cost item would slip past every budget check.
    assert HeuristicTokenCounter().count("x") >= 1


def test_empty_text_costs_nothing():
    assert HeuristicTokenCounter().count("") == 0


def test_get_token_counter_falls_back_when_the_encoder_is_missing():
    counter = get_token_counter(encoding="definitely-not-an-encoding")
    assert counter.count("hello world") >= 1
    assert counter.name in {"heuristic", "tiktoken:definitely-not-an-encoding"}
```

- [x] **Step 7.2: Run and watch fail.** Expected three `ModuleNotFoundError` collection errors.
- [x] **Step 7.3: Implement the three modules.**

`chunking.py`: `Chunk(id, title, content, source, heading_level, start_line)`; `chunk_markdown(text, source, max_chars=4000)`. Tracks fenced code blocks so a `#` inside a fence is not a heading. Ids are `sha1(f"{source}::{heading_path}::{ordinal}")[:16]` prefixed by the source basename, so two files with identical headings never collide and a truncated id still names its file.

`lexical.py`: `tokenize` splits on non-alphanumeric except `_`; `Bm25(docs: dict[str, str], k1=1.5, b=0.75)` with `score(query) -> dict[str, float]` min-max normalised to `[0, 1]`, returning all-zero for an empty query or empty vocabulary intersection.

`tokens.py`: `TokenCounter` protocol with `count(text) -> int` and `name`. `HeuristicTokenCounter` = `ceil(len(text) / 4)`, zero only for empty input. `TiktokenCounter` used when `tiktoken` imports and the encoding resolves. `get_token_counter(encoding="cl100k_base")` returns the best available and never raises.

- [x] **Step 7.4: Run to green.** Expected `16 passed`.
- [ ] **Step 7.5: Commit**

```bash
git add src/context/chunking.py src/context/lexical.py src/context/tokens.py tests/context_memory
git commit -m "feat(context): chunking, bm25 relevance and token counting"
```

## Task 8: The four context sources

**Files:**
- Create: `src/context/sources/base.py`, `instructions.py`, `repository.py`, `current_state.py`, `memory.py`
- Test: `tests/context_memory/test_source_instructions.py`, `test_source_repository.py`, `test_source_current_state.py`, `test_source_memory.py`

### Design

`ContextSource` ABC: `name: str`, `layer: Layer`, `collect(task: Task) -> list[ContextItem]`. Sources never rank and never budget; they only produce candidates with the signals they alone can know (`confidence`, `recency`, `importance`, `staleness`). The ranker owns `relevance` and the scope matches.

`InstructionsSource` is special: every item it produces is `pinned=True`. Instructions are policy (handoff section 3) and must not be dropped by a budget - if they do not fit, that is an error the operator has to see, not a silent trim.

`CurrentStateSource` shells out to git. Two rules apply: scrub inherited `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE` which override `cwd`, and read status through `--porcelain=v1 -z` so paths containing spaces or non-ASCII survive intact.

- [x] **Step 8.1: Write the failing tests**

`tests/context_memory/test_source_instructions.py`:

```python
from pathlib import Path

from src.context.sources.instructions import InstructionsSource
from src.context.types import Layer, Task


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_collects_claude_and_agents_files(tmp_path):
    write(tmp_path, "CLAUDE.md", "# Root policy\n\nAlways verify numbers.\n")
    write(tmp_path, "AGENTS.md", "# Agents\n\nUse pnpm, never npm.\n")
    items = InstructionsSource(root=tmp_path).collect(Task(description="anything"))
    assert {i.layer for i in items} == {Layer.INSTRUCTIONS}
    assert any("verify numbers" in i.content for i in items)
    assert any("pnpm" in i.content for i in items)


def test_every_instruction_item_is_pinned():
    # Policy must never be dropped by the budget.
    items = InstructionsSource(root=Path(__file__).resolve().parents[2]).collect(Task(description="x"))
    assert all(i.pinned for i in items)


def test_nearest_instructions_outrank_root_ones(tmp_path):
    write(tmp_path, "AGENTS.md", "# Root\n\nRoot rule.\n")
    write(tmp_path, "server/AGENTS.md", "# Server\n\nServer rule.\n")
    task = Task(description="edit the server", files=("server/main.py",))
    items = InstructionsSource(root=tmp_path).collect(task)
    by_source = {i.source: i for i in items}
    nearest = by_source["server/AGENTS.md"]
    root = by_source["AGENTS.md"]
    assert nearest.signals.importance > root.signals.importance


def test_a_directory_with_no_instruction_files_contributes_nothing(tmp_path):
    assert InstructionsSource(root=tmp_path).collect(Task(description="x")) == []


def test_skill_files_are_collected_only_when_the_task_names_them(tmp_path):
    write(tmp_path, "skills/context-memory/SKILL.md", "# Skill\n\nHow to build a context.\n")
    unrelated = InstructionsSource(root=tmp_path).collect(Task(description="rename a column"))
    named = InstructionsSource(root=tmp_path).collect(Task(description="run the context-memory skill"))
    assert unrelated == []
    assert any("SKILL.md" in i.source for i in named)
```

`tests/context_memory/test_source_repository.py`:

```python
from src.context.sources.repository import RepositorySource
from src.context.types import Layer, Task


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_collects_readme_and_docs(tmp_path):
    write(tmp_path, "README.md", "# Project\n\nStores memories in pgvector.\n")
    write(tmp_path, "docs/architecture.md", "# Architecture\n\nThe API is FastAPI.\n")
    items = RepositorySource(root=tmp_path).collect(Task(description="how is data stored"))
    assert {i.layer for i in items} == {Layer.REPOSITORY}
    assert {i.source for i in items} == {"README.md", "docs/architecture.md"}


def test_files_named_by_the_task_are_always_collected(tmp_path):
    write(tmp_path, "README.md", "# P\n\nx\n")
    write(tmp_path, "server/main.py", "DEFAULT_LLM_PROVIDER = 'gemini'\n")
    items = RepositorySource(root=tmp_path).collect(Task(description="change the provider", files=("server/main.py",)))
    assert any(i.source == "server/main.py" for i in items)


def test_binary_and_oversized_files_are_skipped_and_counted(tmp_path):
    write(tmp_path, "README.md", "# P\n\nx\n")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02" * 100)
    source = RepositorySource(root=tmp_path, max_file_bytes=10)
    items = source.collect(Task(description="x", files=("blob.bin",)))
    assert all(i.source != "blob.bin" for i in items)
    assert source.last_report.skipped["blob.bin"]


def test_repository_items_carry_no_confidence_of_their_own(tmp_path):
    # Repository truth is current by definition; confidence is a memory concept.
    write(tmp_path, "README.md", "# P\n\nx\n")
    items = RepositorySource(root=tmp_path).collect(Task(description="x"))
    assert all(i.signals.staleness == 0.0 for i in items)
```

`tests/context_memory/test_source_current_state.py`:

```python
import subprocess

import pytest

from src.context.sources.current_state import CurrentStateSource, _scrubbed_env
from src.context.types import Layer, Task


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-b", "work")
    git("config", "user.email", "t@t.test")
    git("config", "user.name", "t")
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    git("add", "a.txt")
    git("commit", "-m", "initial")
    return tmp_path


def test_reports_the_branch(repo):
    items = CurrentStateSource(root=repo).collect(Task(description="x"))
    assert any("work" in i.content for i in items if i.title == "branch")
    assert {i.layer for i in items} == {Layer.CURRENT_STATE}


def test_reports_modified_files(repo):
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    items = {i.title: i.content for i in CurrentStateSource(root=repo).collect(Task(description="x"))}
    assert "a.txt" in items["working tree"]


def test_paths_with_spaces_survive_intact(repo):
    (repo / "a file with spaces.txt").write_text("x\n", encoding="utf-8")
    items = {i.title: i.content for i in CurrentStateSource(root=repo).collect(Task(description="x"))}
    assert "a file with spaces.txt" in items["working tree"]


def test_env_scrub_removes_the_vars_that_override_cwd():
    env = _scrubbed_env({"GIT_DIR": "/elsewhere/.git", "GIT_WORK_TREE": "/elsewhere", "PATH": "/usr/bin"})
    assert "GIT_DIR" not in env
    assert "GIT_WORK_TREE" not in env
    assert env["PATH"] == "/usr/bin"


def test_a_non_git_directory_yields_nothing_and_says_so(tmp_path):
    source = CurrentStateSource(root=tmp_path)
    assert source.collect(Task(description="x")) == []
    assert source.last_error is not None


def test_current_state_items_are_maximally_recent(repo):
    items = CurrentStateSource(root=repo).collect(Task(description="x"))
    assert all(i.signals.recency == 1.0 for i in items)
```

`tests/context_memory/test_source_memory.py`:

```python
from datetime import datetime, timedelta, timezone

from src.context.sources.memory import MemoryContextSource
from src.context.types import Layer, Task
from src.memory.base import MemoryProvider
from src.memory.envelope import Envelope
from src.memory.scopes import Scope, ScopeSelector
from src.memory.types import MemoryRecord


class FakeProvider(MemoryProvider):
    def __init__(self, by_scope):
        self.by_scope = by_scope
        self.queries = []

    def search(self, query):
        self.queries.append(query)
        return self.by_scope.get(query.scope, [])

    def add(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    def get(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    def get_all(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    def update(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    def delete(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    def delete_all(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    def history(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    def reset(self):  # pragma: no cover
        raise NotImplementedError


def record(id_, text, scope, *, age_days=1, confidence=0.8, importance=0.5, lifecycle="durable", score=0.5):
    return MemoryRecord(
        id=id_,
        text=text,
        score=score,
        created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
        envelope=Envelope(
            scope=scope,
            scope_key="k",
            kind="discovery",
            lifecycle=lifecycle,
            confidence=confidence,
            importance=importance,
            topic="t",
            source="s",
            created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
        ),
    )


def test_queries_every_scope_that_has_a_key():
    provider = FakeProvider({})
    selector = ScopeSelector(user="lautaro", repository="memory-optimization")
    MemoryContextSource(provider=provider, selector=selector).collect(Task(description="x"))
    assert [q.scope for q in provider.queries] == [Scope.REPOSITORY, Scope.USER, Scope.GLOBAL]


def test_maps_records_to_memory_layer_items_carrying_provider_score_as_relevance():
    provider = FakeProvider({Scope.GLOBAL: [record("m1", "a lesson", Scope.GLOBAL, score=0.66)]})
    items = MemoryContextSource(provider=provider, selector=ScopeSelector(user="u")).collect(Task(description="x"))
    assert [i.layer for i in items] == [Layer.MEMORY]
    assert items[0].signals.relevance == 0.66


def test_recency_decays_with_age():
    provider = FakeProvider(
        {Scope.GLOBAL: [record("new", "x", Scope.GLOBAL, age_days=1), record("old", "y", Scope.GLOBAL, age_days=400)]}
    )
    items = {i.id: i for i in MemoryContextSource(provider=provider, selector=ScopeSelector(user="u")).collect(Task(description="x"))}
    assert items["new"].signals.recency > items["old"].signals.recency


def test_stale_memories_are_returned_flagged_not_dropped():
    # Dropping them here would hide a contradiction the compiler needs to report.
    provider = FakeProvider({Scope.GLOBAL: [record("m1", "old truth", Scope.GLOBAL, lifecycle="stale")]})
    items = MemoryContextSource(provider=provider, selector=ScopeSelector(user="u")).collect(Task(description="x"))
    assert items[0].signals.staleness == 1.0


def test_superseded_memories_are_never_returned():
    provider = FakeProvider({Scope.GLOBAL: [record("m1", "replaced", Scope.GLOBAL, lifecycle="superseded")]})
    items = MemoryContextSource(provider=provider, selector=ScopeSelector(user="u")).collect(Task(description="x"))
    assert items == []


def test_the_same_memory_returned_from_two_scopes_appears_once():
    r = record("m1", "a lesson", Scope.GLOBAL)
    provider = FakeProvider({Scope.GLOBAL: [r], Scope.USER: [r]})
    items = MemoryContextSource(provider=provider, selector=ScopeSelector(user="u")).collect(Task(description="x"))
    assert [i.id for i in items] == ["m1"]


def test_a_provider_failure_is_surfaced_not_swallowed():
    class Broken(FakeProvider):
        def search(self, query):
            raise RuntimeError("upstream 503")

    source = MemoryContextSource(provider=Broken({}), selector=ScopeSelector(user="u"))
    items = source.collect(Task(description="x"))
    assert items == []
    assert "upstream 503" in str(source.last_error)
```

- [x] **Step 8.2: Run and watch fail.** Expected four collection errors.
- [x] **Step 8.3: Implement the four sources.**

`base.py`: the ABC plus a `last_error` attribute every source sets and the manager reports.

`instructions.py`: walks from the root and from each task file's directory upward, collecting `CLAUDE.md`, `AGENTS.md`, `.claude/rules/*.md`; `skills/*/SKILL.md` only when the skill's directory name appears in the task description. `importance = 1.0 - 0.1 * distance_in_directories_from_the_task`, floored at 0.5, so nearest wins. Every item `pinned=True`.

`repository.py`: collects `README.md`, `docs/**/*.md`, manifests (`pyproject.toml`, `package.json`, `docker-compose.yaml`), and every path in `task.files`. Skips files over `max_file_bytes` or containing a NUL byte in the first 8 KiB, recording each skip in `last_report.skipped` so the exclusion is visible rather than silent. `staleness = 0.0`, `confidence = 1.0`.

`current_state.py`: `_scrubbed_env` drops `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`, `GIT_COMMON_DIR`. Produces items titled `branch`, `working tree` (from `git status --porcelain=v1 -z`, split on NUL), `diff stat` (`git diff --stat HEAD`), `recent commits` (`git log -5 --format=%h %s`). All `recency = 1.0`, `confidence = 1.0`. On a non-repo, returns `[]` and sets `last_error`.

`memory.py`: iterates `selector.scopes_to_query()`, dedupes by record id keeping the first (narrowest scope) occurrence, drops `superseded`, flags `stale` with `staleness = 1.0`. `recency = 1 / (1 + age_days / 90)`. Catches provider errors into `last_error` and returns `[]` - the manager then reports memory as unavailable rather than pretending there were none.

- [x] **Step 8.4: Run to green.** Expected `25 passed`.
- [ ] **Step 8.5: Commit**

```bash
git add src/context/sources tests/context_memory
git commit -m "feat(context): instruction, repository, git-state and memory sources"
```

---

# Phase 4 - The context compiler

## Task 9: Ranker

**Files:**
- Create: `src/context/ranker.py`
- Test: `tests/context_memory/test_ranker.py`

### Design

`context_score = relevance + task_scope_match + project_scope_match + confidence + recency + importance - redundancy - staleness`, each term weighted. Handoff section 12: *do not invent final weights before measurement*. So `ScoringWeights` ships with all positive terms at 1.0 and both penalties at 1.0, documented in-code as **unmeasured defaults, pending phase 6**, and every call returns a `ScoreBreakdown` with the per-component contribution so a weight can be attributed later.

- [x] **Step 9.1: Write the failing tests**

`tests/context_memory/test_ranker.py`:

```python
import pytest

from src.context.lexical import Bm25
from src.context.ranker import ContextRanker, ScoringWeights
from src.context.types import ContextItem, Layer, Signals, Task


def item(id_, layer, content, **signals):
    return ContextItem(id=id_, layer=layer, content=content, source=f"{id_}.md", signals=Signals(**signals))


def test_breakdown_names_every_component():
    ranked = ContextRanker().rank([item("a", Layer.REPOSITORY, "gemini provider")], Task(description="gemini"))
    assert set(ranked[0].breakdown.contributions) == {
        "relevance",
        "task_scope_match",
        "project_scope_match",
        "confidence",
        "recency",
        "importance",
        "redundancy",
        "staleness",
    }


def test_contributions_sum_to_the_total():
    ranked = ContextRanker().rank([item("a", Layer.REPOSITORY, "gemini provider", confidence=0.5)], Task(description="gemini"))
    assert ranked[0].breakdown.total == pytest.approx(sum(ranked[0].breakdown.contributions.values()))


def test_penalties_are_negative_contributions():
    ranked = ContextRanker().rank([item("a", Layer.MEMORY, "old", staleness=1.0, redundancy=1.0)], Task(description="old"))
    assert ranked[0].breakdown.contributions["staleness"] < 0
    assert ranked[0].breakdown.contributions["redundancy"] < 0


def test_relevance_is_filled_in_for_non_memory_layers_from_bm25():
    items = [
        item("a", Layer.REPOSITORY, "the embedder provider is gemini"),
        item("b", Layer.REPOSITORY, "the dashboard runs on port three thousand"),
    ]
    ranked = ContextRanker().rank(items, Task(description="which embedder provider"))
    assert ranked[0].id == "a"
    assert ranked[0].signals.relevance > ranked[1].signals.relevance


def test_memory_relevance_from_the_provider_is_not_overwritten():
    # The server already scored these by embedding similarity; recomputing with
    # bm25 would silently discard the better signal.
    items = [item("m", Layer.MEMORY, "unrelated words entirely", relevance=0.9)]
    ranked = ContextRanker().rank(items, Task(description="gemini provider"))
    assert ranked[0].signals.relevance == 0.9


def test_pinned_items_sort_above_everything_regardless_of_score():
    pinned = ContextItem(id="p", layer=Layer.INSTRUCTIONS, content="zzz", source="CLAUDE.md", pinned=True)
    scored = item("a", Layer.REPOSITORY, "gemini gemini gemini", confidence=1.0, importance=1.0)
    ranked = ContextRanker().rank([scored, pinned], Task(description="gemini"))
    assert ranked[0].id == "p"


def test_weights_are_injectable_and_change_the_order():
    a = item("a", Layer.MEMORY, "x", relevance=0.9, importance=0.0)
    b = item("b", Layer.MEMORY, "x", relevance=0.0, importance=0.9)
    relevance_first = ContextRanker(weights=ScoringWeights(relevance=2.0, importance=0.1)).rank([a, b], Task(description="x"))
    importance_first = ContextRanker(weights=ScoringWeights(relevance=0.1, importance=2.0)).rank([a, b], Task(description="x"))
    assert relevance_first[0].id == "a"
    assert importance_first[0].id == "b"


def test_default_weights_are_documented_as_unmeasured():
    assert "unmeasured" in ScoringWeights.__doc__.lower()


def test_ranking_is_deterministic_for_tied_scores():
    a = item("a", Layer.MEMORY, "same", relevance=0.5)
    b = item("b", Layer.MEMORY, "same", relevance=0.5)
    first = [i.id for i in ContextRanker().rank([a, b], Task(description="same"))]
    second = [i.id for i in ContextRanker().rank([b, a], Task(description="same"))]
    assert first == second


def test_task_scope_match_rewards_an_item_whose_scope_key_is_the_task_repository():
    on_repo = ContextItem(
        id="a", layer=Layer.MEMORY, content="x", source="mem0",
        signals=Signals(), metadata={"scope": "repository", "scope_key": "memory-optimization"},
    )
    elsewhere = ContextItem(
        id="b", layer=Layer.MEMORY, content="x", source="mem0",
        signals=Signals(), metadata={"scope": "repository", "scope_key": "other-repo"},
    )
    ranked = ContextRanker().rank([elsewhere, on_repo], Task(description="x", repository="memory-optimization"))
    assert ranked[0].id == "a"
```

- [x] **Step 9.2: Run and watch fail.**
- [x] **Step 9.3: Implement `src/context/ranker.py`.** `ScoringWeights` frozen dataclass, docstring stating the defaults are unmeasured placeholders pending phase 6. `ContextRanker.rank(items, task)` fills `relevance` from `Bm25` for non-memory layers, computes `task_scope_match`/`project_scope_match` from item metadata against the task, builds a `ScoreBreakdown`, and sorts by `(not pinned, -total, id)` for determinism.
- [x] **Step 9.4: Run to green.** Expected `10 passed`.
- [ ] **Step 9.5: Commit**

```bash
git add src/context/ranker.py tests/context_memory/test_ranker.py
git commit -m "feat(context): multi-signal ranker with observable score components"
```

## Task 10: Deduplication

**Files:**
- Create: `src/context/dedupe.py`
- Test: `tests/context_memory/test_dedupe.py`

- [x] **Step 10.1: Write the failing tests**

`tests/context_memory/test_dedupe.py`:

```python
from src.context.dedupe import Deduplicator
from src.context.types import ContextItem, Layer, ScoreBreakdown


def item(id_, content, total=1.0, layer=Layer.MEMORY):
    return ContextItem(
        id=id_, layer=layer, content=content, source=f"{id_}.md",
        breakdown=ScoreBreakdown(total=total, contributions={}),
    )


def test_near_identical_items_collapse_to_one():
    kept, dropped = Deduplicator(threshold=0.8).run([
        item("a", "The embedder provider is gemini for this deployment.", total=1.0),
        item("b", "The embedder provider is gemini for this deployment", total=0.4),
    ])
    assert [i.id for i in kept] == ["a"]
    assert dropped[0].id == "b"


def test_the_higher_scoring_duplicate_survives():
    kept, _ = Deduplicator(threshold=0.8).run([
        item("low", "same text here entirely", total=0.2),
        item("high", "same text here entirely", total=0.9),
    ])
    assert [i.id for i in kept] == ["high"]


def test_distinct_items_both_survive():
    kept, dropped = Deduplicator(threshold=0.8).run([
        item("a", "the embedder provider is gemini"),
        item("b", "the dashboard listens on port three thousand"),
    ])
    assert len(kept) == 2
    assert dropped == []


def test_a_duplicate_across_layers_keeps_the_higher_precedence_layer():
    # Repository truth outranks a memory that merely restates it.
    kept, dropped = Deduplicator(threshold=0.8).run([
        item("m", "postgres is the vector store", total=0.9, layer=Layer.MEMORY),
        item("r", "postgres is the vector store", total=0.1, layer=Layer.REPOSITORY),
    ])
    assert [i.id for i in kept] == ["r"]
    assert dropped[0].id == "m"


def test_the_survivor_records_what_it_absorbed():
    kept, _ = Deduplicator(threshold=0.8).run([
        item("a", "same text here entirely", total=0.9),
        item("b", "same text here entirely", total=0.2),
    ])
    assert kept[0].metadata["absorbed"] == ["b"]


def test_duplicate_rate_is_reported():
    d = Deduplicator(threshold=0.8)
    d.run([item("a", "x y z w"), item("b", "x y z w"), item("c", "totally different content")])
    assert d.last_report.duplicate_rate == 1 / 3


def test_very_short_items_are_not_collapsed_by_accident():
    kept, _ = Deduplicator(threshold=0.8).run([item("a", "ok"), item("b", "no")])
    assert len(kept) == 2
```

- [x] **Step 10.2: Run and watch fail.**
- [x] **Step 10.3: Implement `src/context/dedupe.py`.** Word-level 3-shingles, Jaccard similarity; items under 5 tokens compare by exact normalised equality only. Survivor chosen by `(layer precedence index, -score)`. Records `absorbed` ids on the survivor and a `DedupeReport(total, kept, dropped, duplicate_rate)`.
- [x] **Step 10.4: Run to green.** Expected `7 passed`.
- [ ] **Step 10.5: Commit**

```bash
git add src/context/dedupe.py tests/context_memory/test_dedupe.py
git commit -m "feat(context): shingle-based deduplication with a duplicate-rate report"
```

## Task 11: Precedence and conflict resolution

**Files:**
- Create: `src/context/conflicts.py`
- Test: `tests/context_memory/test_conflicts.py`

### Design

Handoff section 13's ladder is already encoded in `Layer.precedence()`. What this module adds: when a *memory* asserts something a higher-precedence layer also addresses, the memory is marked stale rather than silently injected alongside.

Detecting free-text contradiction reliably is not something this module can do, and pretending otherwise is the exact failure this codebase is supposed to catch. So the rules are deterministic and their coverage is reported:

1. `TopicSupersessionRule` - items sharing a `metadata["topic"]` key. The higher-precedence layer wins; the memory is marked `staleness = 1.0` and annotated with what overruled it.
2. `ExplicitSupersessionRule` - a memory whose envelope carries `superseded_by`.

**Named failure direction:** a contradiction between a memory and repository truth that shares no `topic` key is *not* detected. Both items are injected, ordered by precedence, and the memory is rendered with its age and confidence so a reader can weigh it. `ConflictReport.unchecked` counts exactly these, so the gap is measured rather than assumed away. This is a known limitation with a visible failure mode, not a silent one.

- [x] **Step 11.1: Write the failing tests**

`tests/context_memory/test_conflicts.py`:

```python
from src.context.conflicts import ConflictResolver
from src.context.types import ContextItem, Layer


def item(id_, layer, content, topic=None, **metadata):
    md = dict(metadata)
    if topic:
        md["topic"] = topic
    return ContextItem(id=id_, layer=layer, content=content, source=f"{id_}", metadata=md)


def test_a_memory_contradicting_repository_truth_on_the_same_topic_is_marked_stale():
    repo = item("r", Layer.REPOSITORY, "PostgreSQL is the vector store.", topic="vector_store")
    memory = item("m", Layer.MEMORY, "Project uses Redis.", topic="vector_store")
    resolved, report = ConflictResolver().run([memory, repo])
    by_id = {i.id: i for i in resolved}
    assert by_id["m"].signals.staleness == 1.0
    assert by_id["r"].signals.staleness == 0.0
    assert report.conflicts[0].loser == "m"
    assert report.conflicts[0].winner == "r"


def test_the_stale_memory_is_kept_not_deleted():
    repo = item("r", Layer.REPOSITORY, "PostgreSQL.", topic="vector_store")
    memory = item("m", Layer.MEMORY, "Redis.", topic="vector_store")
    resolved, _ = ConflictResolver().run([memory, repo])
    assert {i.id for i in resolved} == {"m", "r"}


def test_the_loser_records_what_overruled_it():
    repo = item("r", Layer.REPOSITORY, "PostgreSQL.", topic="vector_store")
    memory = item("m", Layer.MEMORY, "Redis.", topic="vector_store")
    resolved, _ = ConflictResolver().run([memory, repo])
    loser = next(i for i in resolved if i.id == "m")
    assert loser.metadata["overruled_by"] == "r"


def test_two_memories_on_the_same_topic_leave_the_newer_one_authoritative():
    old = item("old", Layer.MEMORY, "a", topic="t", created_order=1)
    new = item("new", Layer.MEMORY, "b", topic="t", created_order=2)
    old.created_at = __import__("datetime").datetime(2025, 1, 1)
    new.created_at = __import__("datetime").datetime(2026, 1, 1)
    resolved, _ = ConflictResolver().run([old, new])
    by_id = {i.id: i for i in resolved}
    assert by_id["old"].signals.staleness == 1.0
    assert by_id["new"].signals.staleness == 0.0


def test_an_explicitly_superseded_memory_is_marked_regardless_of_topic():
    memory = item("m", Layer.MEMORY, "old approach", superseded_by="m9")
    resolved, report = ConflictResolver().run([memory])
    assert resolved[0].signals.staleness == 1.0
    assert report.conflicts[0].rule == "explicit_supersession"


def test_instructions_are_never_marked_stale_by_anything():
    rule = item("i", Layer.INSTRUCTIONS, "Always use pnpm.", topic="package_manager")
    memory = item("m", Layer.MEMORY, "npm was used here once.", topic="package_manager")
    resolved, _ = ConflictResolver().run([rule, memory])
    assert next(i for i in resolved if i.id == "i").signals.staleness == 0.0


def test_memories_no_rule_could_check_are_counted_not_ignored():
    # The silent direction: an undetected contradiction looks exactly like agreement.
    memory = item("m", Layer.MEMORY, "Something with no topic key at all.")
    repo = item("r", Layer.REPOSITORY, "Unrelated repository fact.")
    _, report = ConflictResolver().run([memory, repo])
    assert report.unchecked == 1
    assert report.checked == 0


def test_report_states_the_denominator():
    memory = item("m", Layer.MEMORY, "x", topic="t")
    repo = item("r", Layer.REPOSITORY, "y", topic="t")
    other = item("o", Layer.MEMORY, "z")
    _, report = ConflictResolver().run([memory, repo, other])
    assert report.summary() == "1 of 2 memories checked for conflicts, 1 conflict found, 1 unchecked"
```

- [x] **Step 11.2: Run and watch fail.**
- [x] **Step 11.3: Implement `src/context/conflicts.py`.** `Conflict(winner, loser, rule, topic)`, `ConflictReport(conflicts, checked, unchecked)` with `summary()` printing the denominator, `ConflictResolver(rules=None)` defaulting to both rules. Only `Layer.MEMORY` items can lose.
- [x] **Step 11.4: Run to green.** Expected `8 passed`.
- [ ] **Step 11.5: Commit**

```bash
git add src/context/conflicts.py tests/context_memory/test_conflicts.py
git commit -m "feat(context): precedence conflict rules with measured coverage"
```

## Task 12: Token budget

**Files:**
- Create: `src/context/budget.py`
- Test: `tests/context_memory/test_budget.py`

### Design

Instructions are pinned and always included. If pinned content alone exceeds the cap, raise `BudgetExceeded` - trimming policy silently is the failure this whole design exists to avoid. Everything else competes by score, subject to a per-layer floor so a flood of high-scoring memories cannot starve current state.

- [x] **Step 12.1: Write the failing tests**

`tests/context_memory/test_budget.py`:

```python
import pytest

from src.context.budget import BudgetExceeded, ContextBudget, LayerFloors
from src.context.tokens import HeuristicTokenCounter
from src.context.types import ContextItem, Layer, ScoreBreakdown


def item(id_, layer, chars, total=1.0, pinned=False):
    return ContextItem(
        id=id_, layer=layer, content="x" * chars, source=id_, pinned=pinned,
        breakdown=ScoreBreakdown(total=total, contributions={}),
    )


def budget(max_tokens, floors=None):
    return ContextBudget(max_tokens=max_tokens, counter=HeuristicTokenCounter(), floors=floors or LayerFloors())


def test_items_are_included_in_score_order_until_the_cap():
    result = budget(100).apply([
        item("low", Layer.MEMORY, 200, total=0.1),
        item("high", Layer.MEMORY, 200, total=0.9),
    ])
    assert [i.id for i in result.included] == ["high"]
    assert [i.id for i in result.excluded] == ["low"]


def test_pinned_items_are_always_included():
    result = budget(60).apply([
        item("pin", Layer.INSTRUCTIONS, 200, total=0.0, pinned=True),
        item("mem", Layer.MEMORY, 200, total=1.0),
    ])
    assert "pin" in [i.id for i in result.included]
    assert "mem" in [i.id for i in result.excluded]


def test_pinned_content_over_the_cap_raises_rather_than_trimming_policy():
    with pytest.raises(BudgetExceeded, match="pinned"):
        budget(10).apply([item("pin", Layer.INSTRUCTIONS, 400, pinned=True)])


def test_per_layer_floor_reserves_room_for_current_state():
    floors = LayerFloors(current_state=30)
    result = ContextBudget(max_tokens=60, counter=HeuristicTokenCounter(), floors=floors).apply([
        item("m1", Layer.MEMORY, 200, total=0.99),
        item("state", Layer.CURRENT_STATE, 80, total=0.01),
    ])
    assert "state" in [i.id for i in result.included]


def test_report_prints_the_denominator():
    result = budget(40).apply([item(f"m{i}", Layer.MEMORY, 100, total=1 - i / 10) for i in range(10)])
    assert result.report.summary().startswith(f"{len(result.included)} of 10 items included")


def test_report_breaks_down_tokens_by_layer():
    result = budget(1000).apply([
        item("a", Layer.MEMORY, 40),
        item("b", Layer.REPOSITORY, 80),
    ])
    assert result.report.tokens_by_layer[Layer.MEMORY] == 10
    assert result.report.tokens_by_layer[Layer.REPOSITORY] == 20


def test_excluded_items_are_returned_not_discarded():
    result = budget(20).apply([item("a", Layer.MEMORY, 400, total=0.9), item("b", Layer.MEMORY, 400, total=0.1)])
    assert {i.id for i in result.included} | {i.id for i in result.excluded} == {"a", "b"}


def test_an_empty_input_produces_an_empty_but_valid_result():
    result = budget(100).apply([])
    assert result.included == []
    assert result.report.total_tokens == 0
```

- [x] **Step 12.2: Run and watch fail.**
- [x] **Step 12.3: Implement `src/context/budget.py`.** `LayerFloors(instructions=None, current_state=0, repository=0, memory=0)`, `BudgetReport(total_tokens, tokens_by_layer, included_count, candidate_count)` with `summary()` printing `"N of M items included, T tokens"`, `BudgetResult(included, excluded, report)`, `BudgetExceeded`. Two passes: pinned first (raising if they alone exceed), then floors, then the free pool by score.
- [x] **Step 12.4: Run to green.** Expected `8 passed`.
- [ ] **Step 12.5: Commit**

```bash
git add src/context/budget.py tests/context_memory/test_budget.py
git commit -m "feat(context): token budget with layer floors and a printed denominator"
```

## Task 13: Compiler

**Files:**
- Create: `src/context/compiler.py`
- Test: `tests/context_memory/test_compiler.py`

- [x] **Step 13.1: Write the failing tests**

`tests/context_memory/test_compiler.py`:

```python
from src.context.compiler import ContextCompiler
from src.context.types import ContextItem, Layer, Signals


def item(id_, layer, content, source=None, **kw):
    return ContextItem(id=id_, layer=layer, content=content, source=source or f"{id_}.md", **kw)


def test_sections_appear_in_precedence_order():
    text = ContextCompiler().compile([
        item("m", Layer.MEMORY, "a memory"),
        item("r", Layer.REPOSITORY, "repo truth"),
        item("s", Layer.CURRENT_STATE, "git state"),
        item("i", Layer.INSTRUCTIONS, "a rule"),
    ]).text
    assert text.index("a rule") < text.index("git state") < text.index("repo truth") < text.index("a memory")


def test_every_item_carries_its_source():
    text = ContextCompiler().compile([item("r", Layer.REPOSITORY, "x", source="docs/a.md")]).text
    assert "docs/a.md" in text


def test_stale_memories_are_rendered_with_an_explicit_warning():
    text = ContextCompiler().compile([
        item("m", Layer.MEMORY, "Project uses Redis.", signals=Signals(staleness=1.0), metadata={"overruled_by": "r"})
    ]).text
    assert "STALE" in text
    assert "overruled by r" in text


def test_memories_are_rendered_with_confidence_and_age():
    text = ContextCompiler().compile([
        item("m", Layer.MEMORY, "a lesson", signals=Signals(confidence=0.4), metadata={"age_days": 120})
    ]).text
    assert "confidence 0.4" in text
    assert "120d" in text


def test_an_empty_layer_produces_no_heading():
    text = ContextCompiler().compile([item("i", Layer.INSTRUCTIONS, "a rule")]).text
    assert "Memory" not in text


def test_compiling_nothing_yields_an_explicit_empty_marker_not_a_blank_string():
    # A blank context is indistinguishable from a failed build otherwise.
    result = ContextCompiler().compile([])
    assert "no context selected" in result.text.lower()
```

- [x] **Step 13.2: Run and watch fail.**
- [x] **Step 13.3: Implement `src/context/compiler.py`.** `CompiledContext(text, items)`. One `##` heading per non-empty layer in `Layer.precedence()` order; each item rendered as a `###` with its source, and, for memory items, `confidence <x>`, `<n>d` age, and a `STALE - overruled by <id>` marker when `staleness == 1.0`.
- [x] **Step 13.4: Run to green.** Expected `6 passed`.
- [ ] **Step 13.5: Commit**

```bash
git add src/context/compiler.py tests/context_memory/test_compiler.py
git commit -m "feat(context): precedence-ordered compiler with provenance and staleness markers"
```

## Task 14: ContextManager end to end

**Files:**
- Create: `src/context/manager.py`
- Test: `tests/context_memory/test_manager.py`

- [x] **Step 14.1: Write the failing tests**

`tests/context_memory/test_manager.py`:

```python
import pytest

from src.context.manager import ContextManager, ContextRequest
from src.context.sources.base import ContextSource
from src.context.types import ContextItem, Layer, Task


class StubSource(ContextSource):
    def __init__(self, name, layer, items, error=None):
        self._name = name
        self._layer = layer
        self._items = items
        self.last_error = error

    @property
    def name(self):
        return self._name

    @property
    def layer(self):
        return self._layer

    def collect(self, task):
        if self.last_error:
            return []
        return list(self._items)


def item(id_, layer, content, pinned=False):
    return ContextItem(id=id_, layer=layer, content=content, source=f"{id_}.md", pinned=pinned)


def manager(sources, max_tokens=10_000):
    return ContextManager(sources=sources, max_tokens=max_tokens)


def test_builds_a_context_from_every_source():
    result = manager([
        StubSource("i", Layer.INSTRUCTIONS, [item("i1", Layer.INSTRUCTIONS, "Always verify numbers.", pinned=True)]),
        StubSource("r", Layer.REPOSITORY, [item("r1", Layer.REPOSITORY, "pgvector is the store.")]),
        StubSource("s", Layer.CURRENT_STATE, [item("s1", Layer.CURRENT_STATE, "branch: main")]),
        StubSource("m", Layer.MEMORY, [item("m1", Layer.MEMORY, "gemini covers both llm and embedder.")]),
    ]).build(ContextRequest(task=Task(description="which provider do we use")))
    assert "Always verify numbers." in result.text
    assert "gemini covers both" in result.text


def test_the_report_accounts_for_every_candidate():
    result = manager([
        StubSource("m", Layer.MEMORY, [item(f"m{i}", Layer.MEMORY, f"fact {i}") for i in range(5)]),
    ]).build(ContextRequest(task=Task(description="fact")))
    r = result.report
    assert r.candidates == 5
    assert r.injected + r.dropped_duplicate + r.dropped_budget == r.candidates


def test_a_failing_source_is_reported_not_hidden():
    result = manager([
        StubSource("m", Layer.MEMORY, [], error=RuntimeError("upstream 503")),
        StubSource("r", Layer.REPOSITORY, [item("r1", Layer.REPOSITORY, "x")]),
    ]).build(ContextRequest(task=Task(description="x")))
    assert "upstream 503" in result.report.source_errors["m"]
    assert result.report.degraded is True


def test_a_context_built_with_no_memory_source_is_not_marked_degraded():
    result = manager([StubSource("r", Layer.REPOSITORY, [item("r1", Layer.REPOSITORY, "x")])]).build(
        ContextRequest(task=Task(description="x"))
    )
    assert result.report.degraded is False


def test_instructions_survive_a_budget_that_cannot_fit_anything_else():
    result = manager(
        [
            StubSource("i", Layer.INSTRUCTIONS, [item("i1", Layer.INSTRUCTIONS, "Rule.", pinned=True)]),
            StubSource("m", Layer.MEMORY, [item("m1", Layer.MEMORY, "x" * 4000)]),
        ],
        max_tokens=30,
    ).build(ContextRequest(task=Task(description="x")))
    assert "Rule." in result.text
    assert result.report.dropped_budget == 1


def test_the_pipeline_stages_are_all_reported():
    result = manager([StubSource("m", Layer.MEMORY, [item("m1", Layer.MEMORY, "x")])]).build(
        ContextRequest(task=Task(description="x"))
    )
    assert set(result.report.stages) == {"collect", "rank", "dedupe", "conflicts", "budget", "compile"}
    assert all(v >= 0 for v in result.report.stages.values())


def test_build_is_deterministic():
    sources = [
        StubSource("r", Layer.REPOSITORY, [item("r1", Layer.REPOSITORY, "a"), item("r2", Layer.REPOSITORY, "b")]),
    ]
    first = manager(sources).build(ContextRequest(task=Task(description="a b")))
    second = manager(sources).build(ContextRequest(task=Task(description="a b")))
    assert first.text == second.text
```

- [x] **Step 14.2: Run and watch fail.**
- [x] **Step 14.3: Implement `src/context/manager.py`.** `ContextRequest(task, max_tokens=None, weights=None)`. `ContextManager.build` runs collect -> rank -> dedupe -> conflicts -> re-rank (staleness changed) -> budget -> compile, timing each stage into `report.stages` (milliseconds). `BuildReport(candidates, injected, dropped_duplicate, dropped_budget, source_errors, degraded, stages, duplicate_rate, conflict_report, budget_report)`; `degraded` is true when any source reported an error. `ContextManager.from_repo(root, provider, selector)` builds the four real sources.
- [x] **Step 14.4: Run to green.** Expected `7 passed`.
- [x] **Step 14.5: Run the whole suite**

```bash
.venv/Scripts/python.exe -m pytest tests/context_memory -q
```

Expected: all tests pass, integration tests skipped without `MEM0_API_URL`.

- [ ] **Step 14.6: Commit**

```bash
git add src/context/manager.py tests/context_memory/test_manager.py
git commit -m "feat(context): ContextManager pipeline with a per-stage build report"
```

## Task 15: CLI scripts and the `context-memory` skill

**Files:**
- Create: `src/scripts/context_build.py`, `memory_store.py`, `memory_search.py`, `memory_promote.py`
- Create: `skills/context-memory/SKILL.md` and its four `references/*.md`

- [x] **Step 15.1: Write the four CLIs**

Each uses `argparse`, reads connection settings from the environment via `Mem0Provider.from_env()`, and prints JSON with `--json` for machine use. `context_build.py --task "<text>" --files a.py b.py --max-tokens 8000 [--report-only]` prints the compiled context or just the report.

- [x] **Step 15.2: Verify each CLI against the live stack**

```bash
.venv/Scripts/python.exe -m src.scripts.memory_store --scope repository --key memory-optimization \
  --kind discovery --topic server.default_provider \
  --text "The self-hosted stack ships pgvector only; there is no Neo4j service in server/docker-compose.yaml."
.venv/Scripts/python.exe -m src.scripts.memory_search --scope repository --key memory-optimization --query "graph database"
.venv/Scripts/python.exe -m src.scripts.context_build --task "add graph memory to the self-hosted stack" --report-only
```

Expected: the stored memory comes back from the search, and the context build report shows a non-zero candidate count for all four layers.

- [x] **Step 15.3: Write `skills/context-memory/SKILL.md`**

Under 500 lines per `skills/AGENTS.md`. Frontmatter `name: context-memory` with explicit TRIGGER / DO NOT TRIGGER. Content: the four layers, the precedence ladder, when to store versus when not to, the CLI invocations, and links into `references/`. Everything else goes in the four reference files: `memory-policy.md` (handoff sections 3-7: rule versus evidence, promotion model, scopes), `retrieval-policy.md` (section 12 signals and the unmeasured-weights caveat), `context-precedence.md` (section 13 plus the named limits of conflict detection), `evaluation.md` (sections 15-16 metrics and the retrieval-versus-answer-quality distinction).

- [ ] **Step 15.4: Commit**

```bash
git add src/scripts skills/context-memory
git commit -m "feat(context): cli entry points and the context-memory skill"
```

---

# Follow-on work (specified, not executed in this plan)

## Phase 5 - Memory extraction

Build `src/memory/extraction.py`: given a session transcript or a task summary, propose candidate memories. Store nothing automatically. Every candidate enters at `lifecycle=candidate` with the extracting session recorded in `source`. Only decisions, discoveries, lessons, stable conventions, validated tool behaviour and important failures qualify (handoff section 18 phase 5). Gate: a human or a reviewing agent promotes `candidate -> durable`.

## Phase 6 - Evaluation

`evaluation/context-manager/` harness comparing the five configurations of handoff section 18 phase 6: full context, static docs only, Mem0 only, static + Mem0, static + Mem0 + current state. Metrics from section 15, reported with the section 16 methodology block (dataset, model, retrieval config, top-K, reranker, token budget, latency, run count, self-reported flag). Retrieval Recall@K must never be reported as an end-to-end score. **This phase is what turns the ranker's placeholder weights into measured ones**; until it runs, they stay documented as unmeasured.

## Section 19 - General CLAUDE.md refactor

Mandatory final step of the handoff, deliberately deferred until phases 5-6 exist, because 19.6 requires measuring whether task success regressed and there is no harness to measure it with yet. Input: `C:\Users\Witbor\.claude\CLAUDE.md`. Classify every rule A-F, keep policy, move NetSuite/Graphify rules to project rule files, extract the embedded concrete measurements (the three-files-one-label truncation incident, the 2-of-96 multi-value query, the 10-of-53 truncated report, the 833-of-1217 no-op rebuild, the 1:1 fan-in miscount, the desynced generated artifacts) into Mem0 as `scope=global, kind=incident` memories, then measure the token reduction and re-run representative tasks against both versions.

---

## Self-review

**Spec coverage.** Handoff sections 1-2 -> Tasks 6-8 (the four layers as separate sources). Section 3-7 -> `skills/context-memory/references/memory-policy.md` (Task 15) and the lifecycle in Task 5. Section 8 -> Task 3. Section 9 -> Task 1, with the Neo4j deviation stated. Section 10 -> Task 4. Section 11 -> Task 14. Section 12 -> Task 9. Section 13 -> Task 11. Section 14 -> Task 8 `RepositorySource`. Sections 15-16 -> follow-on phase 6, plus `references/evaluation.md`. Section 17 -> the file structure. Section 18 phases 1-4 -> Tasks 1-15; phases 5-6 -> follow-on. Section 19 -> follow-on, with its deferral justified.

**Placeholder scan.** No TBD, no "add error handling", no "similar to Task N". Implementation bodies are described by behaviour and algorithm with the tests as the contract; that is the stated granularity in Deviation 4, not an omission.

**Type consistency.** `ContextItem`, `Signals`, `ScoreBreakdown`, `Layer`, `Task` are defined once in Task 6 and used unchanged in Tasks 9-14. `Envelope`, `Scope`, `Lifecycle`, `MemoryRecord`, `SearchQuery` are defined in Tasks 3-5 and used unchanged in Tasks 4 and 8. `last_error` appears on `ContextSource` (Task 8) and is read by `ContextManager` (Task 14). `last_report` appears on `RepositorySource` and `Deduplicator`. Budget's `LayerFloors` field names match `Layer` member names.

---

# Execution log - what the plan did not predict

Recorded because each of these cost a round, and each is a planning miss rather
than an implementation bug.

## 1. The gemini provider was missing its SDK

`mem0/llms/gemini.py:4-8` and `mem0/embeddings/gemini.py:4` do `from google import
genai`, which lives in the **`google-genai`** package. `server/requirements.txt`
pinned only `google-generativeai`. The failure would have been an ImportError on
the first request, well after the stack looked healthy.

Fixed by adding `google-genai>=1.0.0` to `server/requirements.txt`.

## 2. Embedding width mismatch, silent until insert

`mem0/configs/vector_stores/pgvector.py:9` defaults `embedding_model_dims` to
**1536**. `mem0/embeddings/gemini.py:16-17` defaults to **768**. The pgvector
column is created at the former width, and the first insert of a 768-dim vector
fails - after the table already exists.

Fixed by making the width env-driven (`MEM0_EMBEDDING_DIMS`), set on both the
vector store and the embedder from one value so they cannot drift apart.

## 3. TLS interception broke every dependency download

Norton antivirus re-signs HTTPS on this machine. Measured: the cert presented for
`pypi.org` is issued by `CN=Norton Web/Mail Shield Root`. Consequences:

- Host `pip` failed with `CERTIFICATE_VERIFY_FAILED`. Fixed by exporting the
  Norton root from the Windows store to `%LOCALAPPDATA%\pip\ca-bundle.pem` and
  pointing `.venv/pip.ini` at it. Verification stays ON.
- The same failure inside the Docker build. Fixed by `server/certs/` plus an
  `update-ca-certificates` step in `server/dev.Dockerfile`, a no-op when the
  directory is empty.
- A non-ASCII repo path (`Gestión`) made this worse: pip 23.1.2 misreads a
  non-ASCII path from its own config file, so the CA bundle had to live at an
  ASCII path.

## 4. `init-db.sh` is checked out CRLF on Windows

`core.autocrlf=true` and no `.gitattributes` meant `server/init-db.sh` arrived
with CRLF endings. The kernel then looks for an interpreter named `/bin/bash
`
and postgres logs `cannot execute: required file not found`.

This failed in the silent direction: postgres reported **healthy**, and the only
symptom appeared much later as `database "mem0_app" does not exist` from the API.

Fixed with a `.gitattributes` (`*.sh text eol=lf`) and by converting four files
that were already CRLF: `server/init-db.sh`, `server/dashboard/entrypoint.sh`,
`server/scripts/seed.sh`, `scripts/oss-to-platform-migrate.sh`.

## 5. The dashboard does not build, and does not need to

`server/dashboard`'s `pnpm i` fails (same TLS interception, in a node image with
no CA fix applied). The dashboard is a Next.js UI the Context Manager never
touches, so only `mem0` and `postgres` are started. `scripts/stack.ps1` codifies
that.

## 6. Docker Desktop's engine died repeatedly under build load

Three builds ended with `rpc error: code = Unavailable desc = error reading from
server: EOF`, twice leaving the Docker API returning 500. Not resource pressure -
measured 15.7 GB RAM with 6.1 GB free and 257 GB free on C:. Restarting Docker
Desktop cleared it each time; the build succeeded on the retry with cached layers.

Also worth recording: the first build reported `exit code 0` because the command
was piped into `tail`, so the exit status came from `tail`, not from docker. The
build had failed. Every later build captured `$?` before any pipe.

## 7. Min-max normalisation was the wrong basis for lexical relevance

The plan's `Bm25.score` was specified as min-max normalised. That forces the best
candidate to 1.0 even when nothing matched, and the ranker then sums it against
the memory layer's absolute embedding similarity. Two components on different
bases in one weighted sum give a total that is right only by luck.

Changed to `1 - exp(-raw)`: bounded, absolute, comparable across corpora. Two
tests that had encoded the min-max behaviour were rewritten to assert the
property that actually matters.

## 8. CLI output died on the Windows console

Printing a compiled context raised `UnicodeEncodeError` under cp1252. A command
that only works on some machines is not a working command; fixed at the stream
(`configure_stdout()`), not by avoiding non-ASCII content.

## 9. An upstream doc is already stale - and the system caught it

`server/AGENTS.md` documents Neo4j in the dev stack on ports 8474/8687.
`server/docker-compose.yaml` has no Neo4j service. This surfaced in the very
first real `context_build` run. It is exactly the instructions-vs-repository
conflict class the Context Manager exists to expose, and it is worth keeping as
the first Mem0 memory once the server is reachable.

## 10. gemini-2.0-flash is retired

The embedder leg worked immediately, but `infer=True` returned a 502. The real
error was in the container log:

```
google.genai.errors.ClientError: 404 NOT_FOUND. This model models/gemini-2.0-flash
is no longer available. Please update your code to use models/gemini-3.6-flash
```

`gemini-2.0-flash` was taken from `mem0/llms/gemini.py:37`, the SDK's own default,
which is now stale. `MEM0_DEFAULT_LLM_MODEL` is pinned to `gemini-3.6-flash`,
confirmed present via `client.models.list()` rather than trusting the error text.

Two things worth keeping from this:

- The embedder and the LLM are **separate legs**. The integration tests use
  `infer=False` and exercise only the embedder, so they passed while extraction
  was completely broken. A green suite said nothing about the LLM path.
- A model id in a doc, an SDK default, or even an API error message is a claim,
  not a measurement. `models.list()` is the measurement.
