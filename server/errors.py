import logging
import sys
import uuid
from contextvars import ContextVar

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class UpstreamError(HTTPException):
    def __init__(self, code: str, detail: str, request_id: str) -> None:
        super().__init__(status_code=502, detail=detail)
        self.code = code
        self.request_id = request_id


_AUTH_NAMES = {"AuthenticationError", "PermissionDeniedError"}
_RATE_NAMES = {"RateLimitError"}
_TIMEOUT_NAMES = {"APITimeoutError"}
_CONN_NAMES = {"APIConnectionError", "ConnectionError"}
_BAD_REQUEST_NAMES = {"BadRequestError", "UnprocessableEntityError"}
_DB_NAMES = {"OperationalError", "DBAPIError", "DisconnectionError"}
_VECTOR_NAMES = {"UnexpectedResponse", "ResponseHandlingException"}


def _http_status(exc: BaseException) -> int | None:
    """The provider's HTTP status, whatever the SDK calls the attribute.

    OpenAI puts it on `status_code`; google-genai's APIError uses `code` and
    reserves `status` for the string enum ("RESOURCE_EXHAUSTED"). Reading only
    `status_code` classified every Gemini failure as `unknown`, which is how a
    daily quota exhaustion reached the caller as a bare "Upstream provider
    error." with the real reason visible only in the container log.
    """
    for attribute in ("status_code", "code"):
        value = getattr(exc, attribute, None)
        # `code` carries other things on other libraries - a str on the OpenAI
        # SDK ("rate_limit_exceeded"), a str on SQLAlchemy ("e3q8"), an errno on
        # OSError. Require an int in the HTTP range so those cannot be mistaken
        # for a status and misclassified into a confident wrong message.
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if 100 <= value <= 599:
            return value
    return None


def _quota_violations(exc: BaseException) -> list[dict]:
    """The QuotaFailure violations google-genai attaches to a 429, if any.

    Defensive throughout: this runs inside an exception handler, and a payload
    shape that surprises it must degrade to "no facts" rather than raise a
    second error on top of the first.
    """
    details = getattr(exc, "details", None)
    if isinstance(details, list) and len(details) == 1:
        details = details[0]
    if not isinstance(details, dict):
        return []
    inner = details.get("error", details)
    if not isinstance(inner, dict):
        return []
    found: list[dict] = []
    for entry in inner.get("details", []) or []:
        if not isinstance(entry, dict):
            continue
        if not str(entry.get("@type", "")).endswith("QuotaFailure"):
            continue
        for violation in entry.get("violations", []) or []:
            if isinstance(violation, dict):
                found.append(violation)
    return found


def _describe_quota(violations: list[dict]) -> tuple[str, str]:
    """Turn the violations into a code and a message that names the real limit.

    The window matters more than the number. Google returns `retryDelay: 2s` on
    a *daily* exhaustion too, so "retry shortly" - which is what a plain 429
    maps to - tells the caller to do the one thing that cannot work for the next
    several hours. Only a per-day quotaId justifies saying the quota is spent.
    """
    first = violations[0]
    metric = str(first.get("quotaMetric") or "unknown metric")
    limit = first.get("quotaValue")
    quota_id = str(first.get("quotaId") or "")
    dimensions = first.get("quotaDimensions")
    model = dimensions.get("model") if isinstance(dimensions, dict) else None

    where = f"{metric}, limit {limit}" if limit is not None else metric
    if model:
        where += f" (model {model})"

    if "perday" in quota_id.replace("_", "").lower():
        return (
            "provider_quota_exhausted",
            f"Provider quota exhausted for the day: {where}. Retrying will not help "
            f"until the quota resets. Raise the limit or switch provider.",
        )
    # The window is not stated. Say so rather than picking one: guessing "daily"
    # stalls a caller who could have retried, and guessing "shortly" sends a
    # caller into a retry loop that cannot succeed.
    return (
        "provider_rate_limited",
        f"Provider quota exceeded: {where}. The response did not say over what "
        f"window, so retry once before assuming the allowance is spent.",
    )


def _classify_one(exc: BaseException) -> tuple[str, str]:
    name = type(exc).__name__
    module = getattr(type(exc), "__module__", "") or ""
    status = _http_status(exc)

    if name in _AUTH_NAMES or status in (401, 403):
        return (
            "provider_auth_failed",
            "Provider rejected the request (authentication). "
            "Check your LLM provider API key on the Configuration page.",
        )
    if name in _RATE_NAMES or status == 429:
        violations = _quota_violations(exc)
        if violations:
            return _describe_quota(violations)
        return ("provider_rate_limited", "Provider rate limit hit. Retry shortly.")
    if name in _TIMEOUT_NAMES or isinstance(exc, TimeoutError):
        return ("provider_timeout", "Provider timed out. Retry shortly.")
    if name in _CONN_NAMES or (isinstance(status, int) and status >= 500):
        return ("provider_unavailable", "Provider is unreachable or returned a server error.")
    if name in _BAD_REQUEST_NAMES or status in (400, 422):
        return ("provider_bad_request", "Provider rejected the request as malformed.")
    if name in _DB_NAMES:
        return ("datastore_unavailable", "The memory database is unreachable.")
    if name in _VECTOR_NAMES or module.startswith("qdrant_client"):
        return ("vector_store_unavailable", "The vector store is unreachable or returned an error.")
    return ("unknown", "Upstream provider error.")


def _classify(exc: BaseException | None) -> tuple[str, str]:
    # Walk the cause/context chain so wrapped provider errors still classify correctly.
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        result = _classify_one(current)
        if result[0] != "unknown":
            return result
        current = current.__cause__ or current.__context__
    return ("unknown", "Upstream provider error.")


def new_request_id() -> str:
    return uuid.uuid4().hex[:8]


def upstream_error() -> UpstreamError:
    exc = sys.exc_info()[1]
    code, message = _classify(exc)
    rid = request_id_var.get()
    logging.exception("Upstream provider error (code=%s)", code)
    return UpstreamError(code=code, detail=message, request_id=rid)


async def upstream_error_handler(_: Request, exc: UpstreamError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.code, "request_id": exc.request_id},
        headers={"X-Request-ID": exc.request_id},
    )


def install_request_id_logging() -> None:
    old_factory = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        record.request_id = request_id_var.get()
        return record

    logging.setLogRecordFactory(factory)
