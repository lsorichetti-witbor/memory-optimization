"""Client-side error types for the Mem0 transport.

The server already classifies its failures and returns a typed `code` in the
response body (`server/errors.py`): `provider_quota_exhausted`,
`provider_rate_limited`, `provider_unavailable`, `provider_bad_request`, and so
on. The client used to format that into a message and then grep the message it
had just built, so the one piece of structured information in the response was
thrown away and re-derived badly.

`Mem0Error` keeps it. Everything downstream - whether to queue a write, what to
tell the user, whether retrying can possibly help - reads the code.
"""

from __future__ import annotations

from typing import Optional

# The write itself is malformed: the server will reject it identically forever,
# so queueing it would retry a guaranteed failure until someone investigates.
#
# Deliberately short. Every code NOT in this set - including one that does not
# exist yet - is treated as worth keeping, because the two mistakes are not
# symmetric. Wrongly keeping a write costs a file somebody deletes. Wrongly
# dropping one costs an observation that existed nowhere else.
#
# Note what is absent: `provider_auth_failed` is a configuration problem, not a
# bad write. Fix the key and the same write succeeds, so it queues.
PERMANENT_CODES = frozenset({"provider_bad_request"})

# Same reasoning at the HTTP level, for a response that carries no code at all.
PERMANENT_STATUSES = frozenset({400, 422})


class Mem0Error(RuntimeError):
    """A request to the Mem0 server failed.

    Carries the server's own classification so callers do not have to infer it
    from the message text.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "unknown",
        status: Optional[int] = None,
        request_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.request_id = request_id

    @property
    def permanent(self) -> bool:
        """Would this same request fail the same way no matter when it is retried?

        Only true for a request the server judged malformed. Anything else -
        quota, rate limit, outage, timeout, an unrecognised code - is treated as
        temporary, because that is the direction where being wrong is cheap.
        """
        return self.code in PERMANENT_CODES or self.status in PERMANENT_STATUSES

    def __str__(self) -> str:
        base = super().__str__()
        if self.request_id:
            return f"{base} (request_id={self.request_id})"
        return base


class InvalidWrite(ValueError):
    """The write is unusable before it ever reaches the server.

    Raised for things the client can see are wrong - no text, no scope key.
    Distinct from `Mem0Error` because there is nothing to queue: the data itself
    is the problem, and no amount of waiting fixes it.
    """
