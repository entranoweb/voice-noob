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


def write_new_json(path: Path, payload: Any) -> None:
    """Write a file that must not exist yet. A run already stored is never replaced."""
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(to_text(payload))


def write_run(path: Path, payload: dict[str, Any]) -> None:
    """Write a stored run or chain file, stamped with a digest of what is being written.

    The digest lets a later command see that the file was changed by something
    other than the rig. It catches an accidental edit; it is not tamper-proofing
    (see ``completeness``).
    """
    from synthiq_caller.completeness import stamp

    write_json(path, stamp(payload))


def write_new_run(path: Path, payload: dict[str, Any]) -> None:
    """``write_run`` for a file that must not exist yet."""
    from synthiq_caller.completeness import stamp

    write_new_json(path, stamp(payload))


def next_index(directory: Path, prefix: str) -> int:
    """One more than the highest number among ``<prefix>-NNN.json`` in ``directory``.

    Counting the files would hand out a number already in use as soon as one
    file was missing from the sequence.
    """
    highest = 0
    for path in directory.glob(f"{prefix}-*.json"):
        digits = path.stem[len(prefix) + 1 :]
        if digits.isdigit():
            highest = max(highest, int(digits))
    return highest


def utf8_console() -> None:
    """Make printed reports UTF-8 even when redirected to a file on Windows.

    There, a redirected stdout takes the legacy code page, which either mangles
    or refuses the same characters.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
