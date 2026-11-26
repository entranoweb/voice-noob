"""Error text that is safe to put in a response, a websocket message or a tool result.

Exception text carries what the failing call carried: SQL with bound
parameters, request URLs with keys, response bodies that echo a phone number
or an email. None of that belongs in a reply a visitor, a dashboard user or
the voice model (which may read it aloud) receives. The detail goes to the
scrubbed logger; the response gets a fixed message and, where it helps, the
exception *type*.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog
from fastapi import HTTPException

logger = structlog.get_logger()

TIMEOUT = "timeout"
UNAVAILABLE = "unavailable"
UPSTREAM_ERROR = "upstream_error"
NOT_FOUND = "not_found"
INVALID_NUMBER = "invalid_number"
INTERNAL_ERROR = "internal_error"


def public_error(exc: BaseException, message: str) -> str:
    """``message (ExcType)``: a fixed sentence plus the exception class name only."""
    return f"{message} ({type(exc).__name__})"


def classify_failure(exc: BaseException) -> str:
    """Stable failure class for an exception: timeout, unavailable or internal_error."""
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return TIMEOUT
    if isinstance(exc, (httpx.TransportError, httpx.HTTPStatusError, ConnectionError)):
        return UNAVAILABLE
    return INTERNAL_ERROR


def tool_failure(exc: BaseException, service: str) -> dict[str, Any]:
    """Tool result for a raised exception. ``success`` stays False.

    ``service`` is a speakable noun phrase ("booking system"). The message is
    fixed per failure class so the agent can say it to a caller as written.
    """
    code = classify_failure(exc)
    if code == TIMEOUT:
        message = f"The {service} took too long to respond. Please try again in a moment."
    elif code == UNAVAILABLE:
        message = f"The {service} is unavailable right now."
    else:
        message = f"Something went wrong with the {service}. The request was not completed."
    return {"success": False, "error": message, "error_code": code}


def is_unavailable_status(status_code: int | None) -> bool:
    """5xx and 429 are the provider being down or busy, not our request being wrong."""
    return status_code is not None and (status_code >= 500 or status_code == 429)  # noqa: PLR2004


def upstream_failure(
    service: str,
    status_code: int | None = None,
    *,
    message: str | None = None,
    not_found: str | None = None,
) -> dict[str, Any]:
    """Tool result for a provider reply that was not a success.

    The body is never passed on. Service and status are logged (no body) so an
    operator can tell a 401 from a 500. Only a 404 is "not found" (when the
    caller says what was not found); 5xx and 429 are "unavailable"; anything
    else is a request the provider refused.
    """
    logger.warning("tool_upstream_non_success", service=service, status_code=status_code)
    if not_found is not None and status_code == 404:  # noqa: PLR2004
        return {"success": False, "error": not_found, "error_code": NOT_FOUND}
    if is_unavailable_status(status_code):
        return {
            "success": False,
            "error": f"The {service} is unavailable right now.",
            "error_code": UNAVAILABLE,
        }
    return {
        "success": False,
        "error": message or f"The {service} could not complete that request.",
        "error_code": UPSTREAM_ERROR,
    }


def provider_refusal(status_code: int) -> HTTPException:
    """502 for a refused provider call, with a fixed message.

    The provider's status is never forwarded: a 401 from the provider means the
    workspace's key was refused, and the dashboard client signs the user out on
    any 401 of ours. The body is not forwarded either (it can echo the key).
    """
    if status_code in (401, 403):
        detail = "The voice provider refused this workspace's API key"
    elif is_unavailable_status(status_code):
        detail = "The voice provider is unavailable right now"
    else:
        detail = "The voice provider refused the request"
    return HTTPException(status_code=502, detail=detail)
