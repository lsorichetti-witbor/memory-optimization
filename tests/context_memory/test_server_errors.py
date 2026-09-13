"""The 502 the server returns must name the reason the provider gave.

Measured incident: every write and search failed with
`502: Upstream provider error.` while the container log held
`429 RESOURCE_EXHAUSTED ... embed_content_free_tier_requests, limit: 1000`.
The classifier in server/errors.py read `status_code`, and google-genai puts the
HTTP status on `code` and reserves `status` for the string enum - so every
Gemini failure, of any kind, fell through to `unknown`. Nothing was broken
loudly; the reason was simply not in the response, and the eval harness read the
resulting empty retrievals as a ranking regression.

These tests build the real `google.genai.errors.ClientError` with the payload
captured from that incident, rather than a stand-in. A hand-rolled double cannot
notice an SDK moving the attribute the classifier reads, which is the whole
defect.
"""

from __future__ import annotations

from google.genai import errors as genai_errors

from server.errors import _classify, _http_status

# Captured verbatim from the container log during the incident, trimmed only of
# the Help links. Keeping the real shape matters: the nesting under
# error.details[].violations[] is the part the extraction has to walk.
DAILY_QUOTA_PAYLOAD = {
    "error": {
        "code": 429,
        "message": "You exceeded your current quota, please check your plan and billing details.",
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [
                    {
                        "quotaMetric": "generativelanguage.googleapis.com/embed_content_free_tier_requests",
                        "quotaId": "EmbedContentRequestsPerDayPerUserPerProjectPerModel-FreeTier",
                        "quotaDimensions": {"location": "global", "model": "gemini-embedding-1.0"},
                        "quotaValue": "1000",
                    }
                ],
            },
            # Present on a daily exhaustion too, and it lies: two seconds will
            # not bring the allowance back. The classifier must not key on it.
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "2s"},
        ],
    }
}


def daily_quota_error() -> genai_errors.ClientError:
    return genai_errors.ClientError(429, DAILY_QUOTA_PAYLOAD)


# --------------------------------------------------- the fixture still reproduces


def test_the_gemini_error_still_hides_its_status_where_the_old_code_looked():
    # If google-genai ever grows a `status_code`, the original bug stops
    # reproducing and these tests would pass without proving anything. Assert
    # the precondition rather than trusting it.
    error = daily_quota_error()
    assert not hasattr(error, "status_code"), (
        "google-genai now exposes status_code; this suite no longer reproduces the "
        "attribute-name mismatch it was written for"
    )
    assert error.code == 429
    assert error.status == "RESOURCE_EXHAUSTED"


# ------------------------------------------------------------------ status reading


def test_the_http_status_is_found_on_code_when_there_is_no_status_code():
    assert _http_status(daily_quota_error()) == 429


def test_a_non_http_code_attribute_is_not_mistaken_for_a_status():
    # The OpenAI SDK puts a slug on `code`; SQLAlchemy puts a docs shortcode
    # there. Reading either as a status would produce a confident wrong message.
    class SlugCode(Exception):
        code = "rate_limit_exceeded"

    class Errno(Exception):
        code = 13  # EACCES, not an HTTP status

    assert _http_status(SlugCode()) is None
    assert _http_status(Errno()) is None


def test_status_code_still_wins_when_both_are_present():
    class Both(Exception):
        status_code = 401
        code = 429

    assert _http_status(Both()) == 401


# -------------------------------------------------------------------- the message


def test_a_daily_quota_exhaustion_names_the_metric_the_limit_and_the_model():
    code, message = _classify(daily_quota_error())
    assert code == "provider_quota_exhausted"
    assert "embed_content_free_tier_requests" in message
    assert "1000" in message
    assert "gemini-embedding-1.0" in message


def test_a_daily_quota_exhaustion_does_not_tell_the_caller_to_retry_shortly():
    # The failure direction that costs the most: "Retry shortly" sends a caller
    # into a loop that cannot succeed for hours, and hides that the allowance is
    # spent. This is what a bare 429 used to map to.
    _, message = _classify(daily_quota_error())
    assert "Retry shortly" not in message
    assert "will not help" in message


def test_a_429_with_no_quota_detail_stays_the_plain_rate_limit_message():
    plain = genai_errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}})
    code, message = _classify(plain)
    assert code == "provider_rate_limited"
    assert "Retry shortly" in message


def test_a_quota_violation_with_no_stated_window_says_so_rather_than_guessing():
    # Guessing "daily" stalls a caller who could have retried; guessing
    # "shortly" starts a loop that cannot succeed. Neither is free, so the
    # message admits the gap.
    payload = {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [
                        {"quotaMetric": "some.metric/requests", "quotaId": "SomethingPerMinute", "quotaValue": "60"}
                    ],
                }
            ],
        }
    }
    code, message = _classify(genai_errors.ClientError(429, payload))
    assert code == "provider_rate_limited"
    assert "some.metric/requests" in message
    assert "did not say over what window" in message


# ------------------------------------------------------- the rest of the family


def test_a_gemini_auth_failure_now_classifies_instead_of_falling_through():
    # Same root cause as the quota case: without reading `code`, a bad API key
    # also reached the caller as "Upstream provider error."
    error = genai_errors.ClientError(403, {"error": {"code": 403, "status": "PERMISSION_DENIED"}})
    code, message = _classify(error)
    assert code == "provider_auth_failed"
    assert "API key" in message


def test_a_gemini_server_error_classifies_as_provider_unavailable():
    error = genai_errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE"}})
    code, _ = _classify(error)
    assert code == "provider_unavailable"


def test_an_unrecognised_exception_is_still_reported_as_unknown():
    # The fallback has to survive: a classifier that labels everything is worse
    # than one that admits it does not know.
    code, message = _classify(ValueError("something else entirely"))
    assert code == "unknown"
    assert message == "Upstream provider error."


def test_a_wrapped_provider_error_is_found_through_the_cause_chain():
    # mem0 re-raises through its own layers; the quota reason has to survive.
    try:
        try:
            raise daily_quota_error()
        except genai_errors.ClientError as inner:
            raise RuntimeError("embedding failed") from inner
    except RuntimeError as outer:
        code, message = _classify(outer)
    assert code == "provider_quota_exhausted"
    assert "1000" in message


def test_a_malformed_payload_degrades_to_a_message_instead_of_raising():
    # This code runs inside an exception handler. A second error here replaces a
    # useful 502 with a 500 and no reason at all.
    for payload in ({"error": {"details": "not-a-list"}}, {"error": {"details": [{"@type": "x/QuotaFailure"}]}}, {}):
        code, message = _classify(genai_errors.ClientError(429, payload))
        assert code == "provider_rate_limited"
        assert message
