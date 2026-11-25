"""A chain: several calls, in order, that share what the earlier ones left behind.

Book an appointment, then ring back and cancel it. The second call only makes
sense with something the first one produced, so each call may **capture** values
from its own trace and the next calls may use them as ``{{name}}`` anywhere in
their definition: in what they expect, in the caller's facts, in the name of the
clip to play.

    {
      "name": "book then cancel",
      "clean_state": true,
      "chain": [
        {"id": "book", "mutates": "books", "steps": [...],
         "expect": {"tools": [{"name_contains": "book"}]},
         "capture": {"appointment": {"tool": "book", "result": "appointment.id"},
                     "day": {"tool": "book", "argument": "day"}}},
        {"id": "cancel", "mutates": "moves",
         "steps": [{"when": "which day", "clips": ["days/{{day|slug}}.wav"]}],
         "expect": {"tools": [{"name_contains": "cancel",
                               "arguments": {"appointmentId": "{{appointment}}"}}]}}
      ]
    }

A chain is graded as one unit and call by call. Once a call does not pass, the
calls after it are not run: they would be testing a state that never came about.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from synthiq_caller.director import RunRecord
from synthiq_caller.grading import Grade
from synthiq_caller.scenario import (
    KNOWN_FIELDS,
    MUTATION_LEVELS,
    Scenario,
    ScenarioError,
    parse_scenario,
    read_scenario_file,
)
from synthiq_caller.traces.base import Trace

_VARIABLE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\|\s*([a-z]+)\s*)?\}\}")
_FIELD_SOURCES = ("said", "heard", "stored")


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


FILTERS: dict[str, Callable[[str], str]] = {
    "": lambda value: value,
    "lower": lambda value: value.lower(),
    # Safe inside a file name: "Monday 3rd" becomes "monday-3rd".
    "slug": _slug,
    # Safe inside a pattern: the value is matched as plain text.
    "re": re.escape,
}


class MissingVariable(KeyError):
    """A call uses a value no earlier call produced."""


def variables_in(value: Any) -> set[str]:
    """Every ``{{name}}`` used anywhere inside ``value``."""
    if isinstance(value, str):
        return {match[1] for match in _VARIABLE.finditer(value)}
    if isinstance(value, dict):
        return set().union(*(variables_in(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(variables_in(v) for v in value)) if value else set()
    return set()


def render(value: Any, variables: dict[str, str]) -> Any:
    """``value`` with every ``{{name}}`` and ``{{name|filter}}`` filled in."""
    if isinstance(value, str):

        def fill(match: re.Match[str]) -> str:
            name, how = match[1], match[2] or ""
            if name not in variables:
                raise MissingVariable(name)
            if how not in FILTERS:
                raise ScenarioError(f"unknown filter {how!r} on {{{{{name}}}}}")
            return FILTERS[how](variables[name])

        return _VARIABLE.sub(fill, value)
    if isinstance(value, dict):
        return {key: render(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, variables) for item in value]
    return value


@dataclass(frozen=True)
class Capture:
    """Where one variable comes from. Exactly one of the sources is set."""

    name: str
    # A tool call whose name contains this; with ``argument`` or ``result``.
    tool: str = ""
    argument: str = ""
    # A dotted path into the tool's result: ``appointment.id``, ``slots.0.start``.
    result: str = ""
    # A caller fact, as it was ``said``, ``heard`` or ``stored``.
    field: str = ""
    source: str = "stored"
    # A pattern over the agent's lines; group 1 when it has one, else the match.
    agent_says: re.Pattern[str] | None = None
    # A key of the batch's read-back: what the system itself held after the call.
    read_back: str = ""


@dataclass(frozen=True)
class ChainCall:
    id: str
    # The call's scenario, as written, with its ``{{variables}}`` still in place.
    raw: dict[str, Any]
    captures: tuple[Capture, ...]
    mutates: str
    uses: frozenset[str]
    missing_clips: tuple[str, ...] = ()
    # The profile of the system under test (``target.py``) and the name this
    # call's appointment goes under in its ``bookings``: "<chain file>/<call id>".
    target: dict[str, Any] | None = None
    key: str = ""
    # Parts of this call's appointment left to the profile that it does not give.
    missing_target: tuple[str, ...] = ()

    def scenario(self, variables: dict[str, str]) -> Scenario:
        """This call's scenario with the values earlier calls produced."""
        return parse_scenario(render(self.raw, variables), self.target, self.key)


@dataclass(frozen=True)
class Chain:
    name: str
    calls: tuple[ChainCall, ...]
    clean_state: bool = False
    description: str = ""
    requires: str = ""

    @property
    def mutates(self) -> str:
        """The most a run of this chain can change: its most dangerous call."""
        return max((call.mutates for call in self.calls), key=MUTATION_LEVELS.index)


def _parse_capture(name: str, raw: Any) -> Capture:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ScenarioError(f"capture name {name!r} must be letters, digits and underscores")
    if not isinstance(raw, dict):
        raise ScenarioError(f"capture {name!r} must be an object")
    if raw.get("tool"):
        argument, result = str(raw.get("argument", "")), str(raw.get("result", ""))
        if bool(argument) == bool(result):
            raise ScenarioError(f"capture {name!r}: give one of 'argument' or 'result'")
        return Capture(name, tool=str(raw["tool"]), argument=argument, result=result)
    if raw.get("field"):
        source = str(raw.get("from", "stored"))
        if raw["field"] not in KNOWN_FIELDS or source not in _FIELD_SOURCES:
            raise ScenarioError(
                f"capture {name!r}: field must be one of {KNOWN_FIELDS}, "
                f"from one of {_FIELD_SOURCES}"
            )
        return Capture(name, field=str(raw["field"]), source=source)
    if raw.get("agent_says"):
        try:
            pattern = re.compile(str(raw["agent_says"]), re.IGNORECASE)
        except re.error as error:
            raise ScenarioError(f"capture {name!r}: bad pattern: {error}") from error
        return Capture(name, agent_says=pattern)
    if raw.get("read_back"):
        return Capture(name, read_back=str(raw["read_back"]))
    raise ScenarioError(f"capture {name!r} needs 'tool', 'field', 'agent_says' or 'read_back'")


def parse_chain(data: dict[str, Any], target: dict[str, Any] | None = None, key: str = "") -> Chain:
    """Make a chain of ``data``, each call filled in from the target's profile.

    ``key`` is the chain's file name without its ending. A call's appointment is
    looked up in the profile's ``bookings`` under ``<key>/<call id>``.
    """
    if not isinstance(data.get("name"), str) or not data["name"]:
        raise ScenarioError("a chain needs a name")
    raw_calls = data.get("chain")
    if not isinstance(raw_calls, list) or len(raw_calls) < 2:
        raise ScenarioError("a chain needs at least two calls")

    calls: list[ChainCall] = []
    known: set[str] = set()
    for index, entry in enumerate(raw_calls):
        if not isinstance(entry, dict):
            raise ScenarioError(f"chain call {index} must be an object")
        call_id = str(entry.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", call_id):
            raise ScenarioError(f"chain call {index} needs an 'id' of letters, digits, - or _")
        if any(call.id == call_id for call in calls):
            raise ScenarioError(f"chain call id {call_id!r} is used twice")
        if "clean_state" in entry:
            raise ScenarioError(
                f"chain call {call_id!r}: clean_state belongs on the chain; a reset between "
                "its calls would wipe what they share"
            )
        raw = {k: v for k, v in entry.items() if k not in ("id", "capture")}
        raw.setdefault("name", f"{data['name']} / {call_id}")
        uses = variables_in(raw)
        unknown = sorted(uses - known)
        if unknown:
            raise ScenarioError(
                f"chain call {call_id!r} uses {unknown}, which no earlier call captures"
            )
        call_key = f"{key}/{call_id}" if key else call_id
        try:
            shape = parse_scenario(render(raw, dict.fromkeys(uses, "x")), target, call_key)
        except ScenarioError as error:
            raise ScenarioError(f"chain call {call_id!r}: {error}") from error
        expect = shape.expect
        if not (expect.tools or expect.fields or expect.agent_says or expect.agent_never_says):
            raise ScenarioError(
                f"chain call {call_id!r} expects nothing, so the chain could not tell "
                "whether to go on"
            )
        captures = tuple(_parse_capture(str(k), v) for k, v in (entry.get("capture") or {}).items())
        for capture in captures:
            if capture.field and capture.field not in shape.facts:
                raise ScenarioError(
                    f"chain call {call_id!r}: capture {capture.name!r} reads "
                    f"{capture.field!r} but the caller has no such fact"
                )
        calls.append(
            ChainCall(
                id=call_id,
                raw=raw,
                captures=captures,
                mutates=shape.mutates,
                uses=frozenset(uses),
                missing_clips=shape.missing_clips,
                target=target,
                key=call_key,
                missing_target=shape.missing_target,
            )
        )
        known |= {capture.name for capture in captures}

    return Chain(
        name=data["name"],
        calls=tuple(calls),
        clean_state=bool(data.get("clean_state", False)),
        description=str(data.get("description", "")),
        requires=str(data.get("requires", "")),
    )


def load_chain(path: str | Path, target: dict[str, Any] | None = None) -> Chain:
    return parse_chain(read_scenario_file(path), target, Path(path).stem)


def is_chain_file(path: str | Path) -> bool:
    return "chain" in read_scenario_file(path)


def _walk(value: Any, path: str) -> Any:
    """Follow ``a.b.0.c`` into nested objects and lists; None when it leads nowhere."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.lstrip("-").isdigit():
            index = int(part)
            value = value[index] if -len(value) <= index < len(value) else None
        else:
            return None
    return value


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, dict | list):
        return None
    text = str(value).strip()
    return text or None


def capture_values(
    captures: tuple[Capture, ...],
    record: RunRecord,
    trace: Trace | None,
    graded: Grade,
    read_back: dict[str, str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """The values a finished call produced, and the names that could not be read."""
    found: dict[str, str] = {}
    missing: list[str] = []
    for capture in captures:
        value: str | None = None
        if capture.tool:
            if trace is not None:
                for call in trace.tool_calls:
                    if capture.tool.lower() not in call.name.lower() or not call.ok:
                        continue
                    got = (
                        _text(call.arguments.get(capture.argument))
                        if capture.argument
                        else _text(_walk(call.result, capture.result))
                    )
                    value = got if got is not None else value  # the last one wins
        elif capture.field:
            row = next((r for r in graded.fields if r.field == capture.field), None)
            value = _text(getattr(row, capture.source)) if row is not None else None
        elif capture.read_back:
            value = _text((read_back or {}).get(capture.read_back))
        elif capture.agent_says is not None:
            for event in record.timeline:
                if event.get("kind") != "agent":
                    continue
                match = capture.agent_says.search(str(event.get("text", "")))
                if match:
                    value = _text(match[1] if match.groups() else match[0]) or value
        if value is None:
            missing.append(capture.name)
        else:
            found[capture.name] = value
    return found, missing


def chain_outcome(call_outcomes: list[str]) -> str:
    """One verdict for the whole chain from its calls' verdicts.

    ``passed`` needs every call to pass. ``failed`` means the agent failed a
    call. The rest say the chain proved nothing: ``not run`` (no call was
    placed), ``ungraded`` (a call could not be graded) and ``incomplete`` (the
    rig stopped part-way for a reason of its own).
    """
    if "failed" in call_outcomes:
        return "failed"
    if call_outcomes and all(o == "passed" for o in call_outcomes):
        return "passed"
    if all(o == "not run" for o in call_outcomes):
        return "not run"
    if "ungraded" in call_outcomes or "ungradeable" in call_outcomes:
        return "ungraded"
    return "incomplete"
