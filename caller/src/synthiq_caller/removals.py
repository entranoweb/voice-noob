"""The record that a clip was removed: an append-only log beside the manifest.

``removals.jsonl`` has one JSON object per line and is only ever appended to.
A record says what was removed (clip ids, and the speaker ref when the request
named one), when, why (one of ``REASONS``), by which operator (an opaque id),
and what the removal changed. It holds no name, no transcript and no audio.

A bank reads this file, so a clip it names stays ruled out. A line that cannot
be read stops the bank from loading: skipping it would quietly bring a removed
clip back.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from synthiq_caller.clipid import clip_id

LOG_FILE = "removals.jsonl"

REASONS = ("erasure_request", "consent_withdrawn", "retention_expired", "other")


@dataclass(frozen=True)
class Removed:
    clip: str
    # The day it was removed, YYYY-MM-DD.
    date: str
    # The id of the record that removed it.
    removal: str


def new_id() -> str:
    return f"rm_{datetime.now(UTC):%Y%m%dT%H%M%SZ}_{secrets.token_hex(3)}"


def read_log(path: Path) -> list[dict[str, Any]]:
    """Every record in the log at ``path``. No file means none."""
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"{path}: line {number} is not JSON; the log is append-only"
            ) from error
        if not isinstance(record, dict) or not isinstance(record.get("clips"), list):
            raise ValueError(f"{path}: line {number} is not a removal record")
        records.append(record)
    return records


def read_removed(directories: Iterable[Path]) -> dict[str, Removed]:
    """Each clip any directory's log says was removed, with the record that removed it."""
    removed: dict[str, Removed] = {}
    for directory in directories:
        for record in read_log(directory / LOG_FILE):
            when = str(record.get("removed_at", ""))[:10]
            for clip in record["clips"]:
                stem = clip_id(str(clip))
                removed.setdefault(stem, Removed(stem, when, str(record.get("removal", ""))))
    return removed


def append(path: Path, record: dict[str, Any]) -> None:
    """Add one record to the log, flushed to disk. Nothing already there is touched."""
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
