"""Size limits for free text and JSON a client or the call bridge can store.

One place, so a limit is changed once and a test can name it. None of these is
an environment setting: they bound what a row may hold, they are not deployment
choices.

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
from typing import Annotated, Any

from pydantic import AfterValidator

TRANSCRIPT_MAX_CHARS = 200_000
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


def truncate_with_marker(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters, with a marker saying how many were dropped.

    The marker is appended after the limit, so the result can be a few dozen
    characters longer than ``limit``. Text within the limit is returned as is.
    """
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n{truncation_marker(len(text) - limit)}"


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
