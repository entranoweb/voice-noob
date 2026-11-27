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

``bookings`` is keyed by scenario file name without its ending; a call inside a
chain goes under ``<chain file name>/<call id>``. A scenario lists the parts it
leaves to the profile in ``expect.booking_from_target``; run with a profile that
does not give them, or with none, it is held back and says which parts are
missing. It is never run with a weaker check instead.

A profile may also say how the rig reaches this system around a call:

    {
      "allow_mutating": "books",
      "reset":       {"command": ["node", "tools/fixture.js", "restore"], "timeout_s": 180},
      "before_call": {"command": ["node", "tools/fixture.js", "check"]},
      "after_call":  {"command": ["node", "tools/fixture.js", "adopt", "{call_ref}"],
                      "only_when_mutating": true},
      "read_back":   {"command": ["python", "scripts/read_back_sql.py", "--queries", "q.json"]}
    }

Each command is a list: a program and its arguments. It is never a single
string and never goes through a shell (``external.py``). ``allow_mutating`` is
the most this system may be changed by a batch; it does not switch mutation on
(the batch's own ``--allow-mutating`` does that), it refuses a batch that asks
for more. ``only_when_mutating`` on ``after_call`` skips it for a call whose
scenario states it changes nothing. What the batch is given on its command line
wins over the profile.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

KNOWN = {
    "name", "notes", "timezone", "naive_times", "identity_budget", "fixture_faults",
    "tool_outcomes", "bookings", "allow_mutating", "reset", "read_back", "before_call",
    "after_call", "write_tools",
}  # fmt: skip

# How the rig reaches the system around a call, in the order they happen.
COMMANDS = ("reset", "before_call", "read_back", "after_call")
# From least to most; the same words as the batch's --allow-mutating.
ALLOWANCES = ("none", "registers", "books", "all")


class TargetError(ValueError):
    pass


def _check_command(name: str, raw: Any) -> None:
    allowed = {"command", "timeout_s"} | ({"only_when_mutating"} if name == "after_call" else set())
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise TargetError(f"'{name}' is an object with a 'command' and, if wanted, a 'timeout_s'")
    if not isinstance(raw.get("only_when_mutating", False), bool):
        raise TargetError(f"'{name}.only_when_mutating' is true or false")
    command = raw.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and part for part in command)
    ):
        raise TargetError(
            f"'{name}.command' is a list, the program and then each argument; a single "
            "string would have to be split the way a shell does, and no shell is used"
        )
    timeout = raw.get("timeout_s", 1)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
        raise TargetError(f"'{name}.timeout_s' must be a number of seconds above 0")


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
    tools = data.get("write_tools", [])
    if not isinstance(tools, list) or not all(isinstance(t, str) and t for t in tools):
        raise TargetError("'write_tools' is a list of tool names that change the system")
    if "allow_mutating" in data and data["allow_mutating"] not in ALLOWANCES:
        raise TargetError(f"'allow_mutating' must be one of {list(ALLOWANCES)}")
    for name in COMMANDS:
        if name in data:
            _check_command(name, data[name])
    return dict(data)


def command_of(target: dict[str, Any] | None, name: str) -> tuple[list[str], float | None]:
    """The command the profile gives for ``name`` and its time limit; ``([], None)`` if none."""
    raw = (target or {}).get(name)
    if not raw:
        return [], None
    timeout = raw.get("timeout_s")
    return [str(part) for part in raw["command"]], float(timeout) if timeout else None


def exceeds_allowance(target: dict[str, Any] | None, asked: str) -> bool:
    """Whether a batch asking to change ``asked`` wants more than this target permits."""
    ceiling = (target or {}).get("allow_mutating")
    if ceiling is None or asked not in ALLOWANCES:
        return False
    return ALLOWANCES.index(asked) > ALLOWANCES.index(ceiling)


def load_target(path: str | Path) -> dict[str, Any]:
    return parse_target(json.loads(Path(path).read_text(encoding="utf-8")))
