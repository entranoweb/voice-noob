"""Sentry set-up that keeps call content out of error reports.

Sentry's defaults send more than an operator expects from a voice product: the
local variables of every frame (a phone number, a transcript, an API key), the
request body, the cookies and the signature headers of the carrier webhooks. This
module turns those off at the SDK and, because a message or a tag can still echo
a number, runs every event through the same masking the logs use
(``app.core.log_redaction``) before it leaves the process.

Sentry documents ``before_send`` as the place to edit or drop an event, and
``send_default_pii``, ``include_local_variables`` and ``max_request_body_size`` as
the switches this uses: https://docs.sentry.io/platforms/python/configuration/options/
and https://docs.sentry.io/platforms/python/data-management/sensitive-data/
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from app.core.log_redaction import scrub_text

if TYPE_CHECKING:
    from app.core.config import Settings

# Request headers kept in a report. Everything else (Authorization, Cookie,
# X-Twilio-Signature, telnyx-signature-ed25519, telnyx-timestamp, API keys,
# forwarded addresses) is dropped: an allowlist cannot miss a header added later.
_KEPT_HEADERS = frozenset(
    {"content-type", "content-length", "user-agent", "host", "accept", "accept-encoding"}
)
# Request fields that carry what the caller or client sent.
_DROPPED_REQUEST_KEYS = ("data", "cookies", "query_string", "env")
_DROPPED_USER_KEYS = ("ip_address", "email", "username")
# Strings Sentry itself generates; masking could only damage them.
_VERBATIM_KEYS = frozenset(
    {"event_id", "trace_id", "span_id", "parent_span_id", "timestamp", "start_timestamp"}
)
_MAX_DEPTH = 40


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return "[too deep]"
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, Mapping):
        return {
            key: item if key in _VERBATIM_KEYS else _scrub(item, depth + 1)
            for key, item in value.items()
            if not (key == "vars" and isinstance(item, Mapping))
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(item, depth + 1) for item in value]
    return value


def _scrub_request(request: Any) -> Any:
    if not isinstance(request, dict):
        return request
    kept = {k: v for k, v in request.items() if k not in _DROPPED_REQUEST_KEYS}
    headers = kept.get("headers")
    if isinstance(headers, Mapping):
        kept["headers"] = {k: v for k, v in headers.items() if str(k).lower() in _KEPT_HEADERS}
    elif headers is not None:
        kept.pop("headers")
    return kept


def scrub_event(event: Any, hint: Any) -> Any:
    """``before_send`` / ``before_send_transaction``: strip the event, never drop it.

    Removes request bodies, cookies, query strings and every header outside a
    short allowlist, the user's address, email and name, and any frame
    ``vars``, then masks phone numbers and email addresses in every remaining
    string. If scrubbing itself fails the event is replaced by a bare marker
    rather than sent unscrubbed.
    """
    try:
        if "request" in event:
            event["request"] = _scrub_request(event["request"])
        user = event.get("user")
        if isinstance(user, dict):
            event["user"] = {k: v for k, v in user.items() if k not in _DROPPED_USER_KEYS}
        for key in list(event):
            if key not in _VERBATIM_KEYS:
                event[key] = _scrub(event[key])
    except Exception:
        return {"message": "event dropped: scrubbing failed", "level": "error"}
    return event


def sentry_init_kwargs(settings: "Settings") -> dict[str, Any]:
    """The arguments ``sentry_sdk.init`` is called with."""
    return {
        "dsn": settings.SENTRY_DSN,
        "environment": settings.SENTRY_ENVIRONMENT,
        "traces_sample_rate": settings.SENTRY_TRACES_SAMPLE_RATE,
        "send_default_pii": False,
        "include_local_variables": False,
        "max_request_body_size": "never",
        "before_send": scrub_event,
        "before_send_transaction": scrub_event,
    }


def init_sentry(settings: "Settings") -> bool:
    """Start Sentry when a DSN is set. Returns whether it was started."""
    if not settings.SENTRY_DSN:
        return False
    import sentry_sdk

    sentry_sdk.init(**sentry_init_kwargs(settings))
    return True
