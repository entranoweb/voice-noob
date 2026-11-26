"""Safe ids for vendor URL paths.

Voice-agent tool arguments come from the model, and so from whatever a caller
manages to say to it. An id such as ``../calendars/events/abc`` placed in an
f-string URL is normalised by httpx and reaches a different endpoint on the same
vendor, carrying the workspace owner's credentials. Every tool that puts an
argument into a URL path goes through ``encode_path_id`` first.
"""

from typing import Any
from urllib.parse import quote

_FORBIDDEN_CHARS = frozenset("/\\?#%")
_MAX_LENGTH = 256


def encode_path_id(value: Any) -> str | None:
    """Return ``value`` as one percent-encoded URL path segment, or None if unsafe.

    Rejects anything that is not a string or an integer, empty or whitespace-only
    values, ``.`` and ``..``, control characters, and ``/ \\ ? # %``. Real vendor ids
    are alphanumeric with the odd ``-`` or ``_``, so nothing legitimate is lost.
    """
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    text = str(value).strip()
    if not text or len(text) > _MAX_LENGTH or text in {".", ".."}:
        return None
    if any(ch in _FORBIDDEN_CHARS or not ch.isprintable() for ch in text):
        return None
    return quote(text, safe="")


def invalid_id_result(name: str) -> dict[str, Any]:
    """The tools' normal failure shape, for an id that failed ``encode_path_id``."""
    return {"success": False, "error": f"Invalid {name}: not a valid identifier"}
