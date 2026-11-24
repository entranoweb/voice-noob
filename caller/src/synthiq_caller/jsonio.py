"""Writing run files so that a person can read them.

``json.dumps`` escapes every character outside ASCII by default, so an agent's
"I'm sorry" with a typographic apostrophe was saved as ``I\\u2019m sorry``.
That is valid JSON, and unreadable in the file and in anything that greps it.
Run files are UTF-8 text with the characters the agent actually used.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def to_text(payload: Any) -> str:
    return json.dumps(payload, indent=1, ensure_ascii=False)


def write_json(path: Path, payload: Any) -> None:
    """UTF-8, real characters, and the same line endings on every platform."""
    path.write_text(to_text(payload), encoding="utf-8", newline="\n")


def utf8_console() -> None:
    """Make printed reports UTF-8 even when redirected to a file on Windows.

    There, a redirected stdout takes the legacy code page, which either mangles
    or refuses the same characters.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
