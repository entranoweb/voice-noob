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

import logging
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from app.core.log_redaction import scrub_text

if TYPE_CHECKING:
    from app.core.config import Settings

logger = logging.getLogger(__name__)

# Request headers kept in a report. Everything else (Authorization, Cookie,
# X-Twilio-Signature, telnyx-signature-ed25519, telnyx-timestamp, API keys,
# forwarded addresses) is dropped: an allowlist cannot miss a header added later.
_KEPT_HEADERS = frozenset(
    {"content-type", "content-length", "user-agent", "host", "accept", "accept-encoding"}
)
# Request fields that carry what the caller or client sent.
_DROPPED_REQUEST_KEYS = ("data", "cookies", "query_string", "env")
_DROPPED_USER_KEYS = ("ip_address", "email", "username")
# Fields that only ever hold a query string or fragment. The stream token of a
# media WebSocket travels in the URL query (?token=), so these never leave.
_DROPPED_KEYS = frozenset(
    {"query_string", "http.query", "http.fragment", "url.query", "url.fragment", "fragment"}
)
# Fields whose value is a URL or a request target, possibly with "METHOD " in front.
_URL_KEYS = frozenset({"url", "http.url", "url.full", "http.target", "description", "from"})
# Strings Sentry itself generates; masking could only damage them.
_VERBATIM_KEYS = frozenset(
    {"event_id", "trace_id", "span_id", "parent_span_id", "timestamp", "start_timestamp"}
)
# A breadcrumb keeps only these fields, and only these entries of its data. Its
# message and any other data (a log line, an utterance, a transcript) are dropped:
# an allowlist cannot miss a field added later, and a regex cannot find free text.
_CRUMB_KEYS = frozenset({"type", "category", "level", "timestamp"})
_CRUMB_DATA_KEYS = frozenset({"method", "status_code", "reason", "url", "http.method"})
_MAX_DEPTH = 40

_URL_IN_TEXT = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://[^\s\"'<>?#]*)[?#][^\s\"'<>]*")
_PATH_QUERY = re.compile(r"[?#][^\s\"'<>]*")
# 13 to 19 digits, spaces or dashes between them: a card number. All of it goes.
_CARD_RUN = re.compile(r"(?<![\d])\d(?:[ -]?\d){12,18}(?![\d])")


def _scrub_string(value: str, key: str | None) -> str:
    value = _URL_IN_TEXT.sub(r"\1", value)
    if key in _URL_KEYS:
        value = _PATH_QUERY.sub("", value)
    value = _CARD_RUN.sub("[masked]", value)
    return scrub_text(value)


def _scrub(value: Any, depth: int = 0, key: str | None = None) -> Any:
    if depth > _MAX_DEPTH:
        return "[too deep]"
    if isinstance(value, str):
        return _scrub_string(value, key)
    if isinstance(value, Mapping):
        return {
            k: item if k in _VERBATIM_KEYS else _scrub(item, depth + 1, str(k))
            for k, item in value.items()
            if k not in _DROPPED_KEYS and not (k == "vars" and isinstance(item, Mapping))
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(item, depth + 1, key) for item in value]
    return value


def scrub_breadcrumb(crumb: Any, hint: Any) -> Any:
    """``before_breadcrumb``: keep a breadcrumb's type, category, level and time, and
    the method, status and URL (without its query) of a request. Nothing else."""
    out = {k: v for k, v in crumb.items() if k in _CRUMB_KEYS}
    data = crumb.get("data")
    if isinstance(data, Mapping):
        kept = {k: v for k, v in data.items() if k in _CRUMB_DATA_KEYS}
        if kept:
            out["data"] = _scrub(kept)
    return out


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
    short allowlist, query strings and fragments from every URL (the stream token
    is one), the user's address, email and name, and any frame ``vars``; keeps
    only allowlisted breadcrumb fields; masks card-like digit runs, phone numbers
    and email addresses in every remaining string. If scrubbing itself fails the
    event is dropped (returns None) and a line is logged locally.
    """
    try:
        if "request" in event:
            event["request"] = _scrub_request(event["request"])
        user = event.get("user")
        if isinstance(user, dict):
            event["user"] = {k: v for k, v in user.items() if k not in _DROPPED_USER_KEYS}
        crumbs = event.get("breadcrumbs")
        if isinstance(crumbs, Mapping) and isinstance(crumbs.get("values"), list):
            event["breadcrumbs"] = {
                **crumbs,
                "values": [
                    scrub_breadcrumb(c, hint) for c in crumbs["values"] if isinstance(c, Mapping)
                ],
            }
        elif isinstance(crumbs, list):
            event["breadcrumbs"] = [
                scrub_breadcrumb(c, hint) for c in crumbs if isinstance(c, Mapping)
            ]
        for key in list(event):
            if key not in _VERBATIM_KEYS:
                event[key] = _scrub(event[key])
    except Exception as exc:
        # Returning None makes the SDK drop an event or a transaction; a stand-in
        # would lose its type and still say something happened.
        # https://docs.sentry.io/platforms/python/configuration/filtering/
        logger.warning("sentry_event_dropped_scrub_failed error=%s", type(exc).__name__)
        return None
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
        "before_breadcrumb": scrub_breadcrumb,
    }


def init_sentry(settings: "Settings") -> bool:
    """Start Sentry when a DSN is set. Returns whether it was started."""
    if not settings.SENTRY_DSN:
        return False
    import sentry_sdk

    sentry_sdk.init(**sentry_init_kwargs(settings))
    return True
