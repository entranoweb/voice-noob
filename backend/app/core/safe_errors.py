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

TIMEOUT = "timeout"
UNAVAILABLE = "unavailable"
UPSTREAM_ERROR = "upstream_error"
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


def upstream_failure(service: str) -> dict[str, Any]:
    """Tool result for a provider reply that was not a success (its body is not passed on)."""
    return {
        "success": False,
        "error": f"The {service} could not complete that request.",
        "error_code": UPSTREAM_ERROR,
    }
