"""Size limits for free text and JSON a client or the call bridge can store.

One place, so a limit is changed once and a test can name it. Only
``TRANSCRIPT_MAX_CHARS`` is an environment setting (``settings.TRANSCRIPT_MAX_CHARS``,
default 200,000, refused at start-up outside 10,000 to 400,000). The others bound
what a row may hold and are not deployment choices.

How the transcript limit was chosen. The longest call the bridge allows is
``CALL_MAX_DURATION_S`` = 1800 s (30 minutes); the model provider's own ceiling
is 60 minutes. Fast speech is about 250 words a minute, at about 6 characters a
word with the space, so 1,500 characters a minute for one speaker. Both sides
talking without a pause for the whole 30 minutes is 2 x 1,500 x 30 = 90,000
characters, and for 60 minutes 180,000. Speaker labels and blank lines add about
2% on top. ``TRANSCRIPT_MAX_CHARS`` is 200,000: above the 60-minute worst case,
and more than 4 times a typical 30-minute call (about 2 x 900 x 30 = 54,000).
"""

import json
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import HTTPException
from pydantic import AfterValidator, TypeAdapter, ValidationError

from app.core.config import settings

TRANSCRIPT_MAX_CHARS = settings.TRANSCRIPT_MAX_CHARS
# One caller turn, agent turn or scenario line. Far above any spoken turn.
TURN_TEXT_MAX_CHARS = 4_000
# Turns in a scripted scenario or an inline check (a 30-minute call is a few hundred).
MAX_TURNS = 500
# Notes, descriptions, dispositions: free text a person types.
NOTES_MAX_CHARS = 10_000
DESCRIPTION_MAX_CHARS = 5_000
# An agent's instructions are long by design.
PROMPT_MAX_CHARS = 50_000
SHORT_TEXT_MAX_CHARS = 200
# A reported call length: a day, far beyond CALL_MAX_DURATION_S, near no real call.
MAX_DURATION_SECONDS = 86_400
# Contacts added to a campaign in one request.
MAX_CONTACT_IDS = 10_000
# Items in a list of strings or small objects.
MAX_LIST_ITEMS = 500
# A JSON object a client sends (scenario fixtures, tool arguments, settings),
# measured as compact JSON text.
JSON_MAX_CHARS = 100_000
# Every request body except the caller routes, which cap themselves
# (``MAX_BUNDLE_BYTES``, ``CALLER_AUDIO_MAX_BYTES``). The largest legitimate
# body is a transcript: 200,000 characters, at most 4 bytes each in UTF-8, plus
# JSON escaping, is under 1.6 MB; 2 MiB leaves room.
MAX_JSON_BODY_BYTES = 2 * 1024 * 1024
# One call's payload inside a caller bundle. The bundle as a whole is capped
# separately; this stops a single call taking all of it.
MAX_CALL_PAYLOAD_BYTES = 4 * 1024 * 1024


def truncation_marker(omitted: int) -> str:
    return f"[truncated: {omitted} characters omitted]"


# Room kept for the marker line, so a cut text with its marker still fits the
# limit. "\n[truncated: " + up to 12 digits + " characters omitted]" is 46 characters.
MARKER_RESERVE = 64


def truncate_with_marker(text: str, limit: int) -> str:
    """``text`` cut so that it, with a marker saying how many characters went, is at most ``limit``.

    Text within the limit is returned as is, which also makes this safe to apply
    twice: a text it already cut is within the limit and is left alone, so the
    marker is never replaced by a second, smaller count.
    """
    if len(text) <= limit:
        return text
    keep = max(limit - MARKER_RESERVE, 0)
    return f"{text[:keep]}\n{truncation_marker(len(text) - keep)}"


def _bounded_json_size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), default=str))


def _bounded_json(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return value
    size = len(json.dumps(value, separators=(",", ":"), default=str))
    if size > JSON_MAX_CHARS:
        raise ValueError(f"must be at most {JSON_MAX_CHARS} characters as JSON, got {size}")
    return value


def _bounded_json_list(value: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if value is None:
        return value
    size = len(json.dumps(value, separators=(",", ":"), default=str))
    if size > JSON_MAX_CHARS:
        raise ValueError(f"must be at most {JSON_MAX_CHARS} characters as JSON, got {size}")
    return value


BoundedDict = Annotated[dict[str, Any], AfterValidator(_bounded_json)]
BoundedDictList = Annotated[list[dict[str, Any]], AfterValidator(_bounded_json_list)]


class OverCapError(HTTPException):
    """A changed or new value is over its limit (422, the status a request model gives)."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__(status_code=422, detail=errors)


def enforce_caps_on_update(
    new_values: Mapping[str, Any],
    stored: object,
    capped_types: Mapping[str, Any],
    *,
    stored_attrs: Mapping[str, str] | None = None,
) -> None:
    """Refuse a changed value that is over its cap; accept one sent back unchanged.

    A row saved before a limit existed may already hold a value over it. Saving
    that row again, with the field as it is, must not start failing, and the
    stored value is never cut. Only a value that differs from the stored one is
    held to the cap. ``capped_types`` maps a field to the annotated type that
    carries its cap (the same type the create request uses), so a create and an
    update refuse the same things with the same message.

    ``new_values`` is the request's fields (None means not sent) and
    ``stored_attrs`` renames a field to the attribute that holds it on ``stored``.
    """
    for name, cap_type in capped_types.items():
        value = new_values.get(name)
        if value is None:
            continue
        attr = (stored_attrs or {}).get(name, name)
        if value == getattr(stored, attr, None):
            continue
        try:
            TypeAdapter(cap_type).validate_python(value)
        except ValidationError as exc:
            errors = [
                {"loc": [name, *err["loc"]], "msg": err["msg"], "type": err["type"]}
                for err in exc.errors(include_url=False)
            ]
            raise OverCapError(errors) from exc


def enforce_merge_cap_on_update(
    name: str, delta: Mapping[str, Any] | None, stored: Mapping[str, Any] | None
) -> None:
    """The same rule for a JSON object the request is merged into.

    Only the entries the request changes are held to the cap, and the merged
    object may not grow past the cap (or past what was stored, when that was
    already over). A request that sends back what is stored passes.
    """
    if not delta:
        return
    current = dict(stored or {})
    changed = {key: val for key, val in delta.items() if key not in current or current[key] != val}
    if not changed:
        return
    enforce_caps_on_update({name: changed}, object(), {name: BoundedDict})
    merged = {**current, **delta}
    if _bounded_json_size(merged) > JSON_MAX_CHARS and _bounded_json_size(merged) > (
        _bounded_json_size(current)
    ):
        raise OverCapError(
            [
                {
                    "loc": [name],
                    "msg": f"the merged value would be over {JSON_MAX_CHARS} characters as JSON",
                    "type": "too_long",
                }
            ]
        )
