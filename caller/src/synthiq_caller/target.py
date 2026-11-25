"""A target's profile: what is true of one system under test and of no other.

A scenario says what the caller wants ("a consultation, the first slot
offered"). When that slot is, which days the clinic works, what zone its
database writes times in: those belong to the system the agent is connected to.
Kept in the scenario file they made every scenario a test of one clinic, and
run against another it expected the first clinic's 16:30.

A profile is one JSON file:

    {
      "name": "original-clinic",
      "timezone": "Europe/London",
      "naive_times": "UTC",
      "identity_budget": 2,
      "fixture_faults": {"register_patient": ["DUPLICATE_FOUND"]},
      "bookings": {
        "01-consultation-self-pay-shoulder": {"time_from": "16:30", "time_to": "16:30"}
      }
    }

``bookings`` is keyed by scenario file name without its ending. A scenario lists
the parts it leaves to the profile in ``expect.booking_from_target``; run with a
profile that does not give them, or with none, it is held back and says which
parts are missing. It is never run with a weaker check instead.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

KNOWN = {
    "name", "notes", "timezone", "naive_times", "identity_budget", "fixture_faults",
    "tool_outcomes", "bookings",
}  # fmt: skip


class TargetError(ValueError):
    pass


def parse_target(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or not data.get("name"):
        raise TargetError("a target profile is an object with a 'name'")
    unknown = sorted(set(data) - KNOWN)
    if unknown:
        raise TargetError(f"target profile has unknown parts: {unknown}")
    bookings = data.get("bookings") or {}
    if not isinstance(bookings, dict) or not all(isinstance(v, dict) for v in bookings.values()):
        raise TargetError("'bookings' maps a scenario file name to the parts of its appointment")
    for name in ("fixture_faults", "tool_outcomes"):
        if not isinstance(data.get(name) or {}, dict):
            raise TargetError(f"'{name}' must be an object")
    return dict(data)


def load_target(path: str | Path) -> dict[str, Any]:
    return parse_target(json.loads(Path(path).read_text(encoding="utf-8")))
