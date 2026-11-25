"""A scenario: who is calling, what they say to which question, and what must be true after.

Scenarios are plain JSON (YAML too, when PyYAML is installed). Nothing here knows
about any voice engine.

    {
      "name": "new patient gives a date of birth",
      "persona": {"facts": {"name": "Alice Wonderland", "date_of_birth": "2002-02-20"}},
      "steps": [
        {"when": "how can i help", "clips": ["opener.wav"], "intended": "Can you book ..."},
        {"when": "date of birth", "clips": ["dob.wav"], "field": "date_of_birth"}
      ],
      "hangup_after": "mobile number",
      "max_seconds": 110,
      "expect": {"tools": [{"name_contains": "new_patient_check"}], "fields": ["date_of_birth"]}
    }
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KNOWN_FIELDS = ("name", "date_of_birth", "postcode", "phone")
# What a scenario can change in the system the agent is connected to, in rising
# order. "unknown" is the default for a scenario that does not say, and is
# treated as the most dangerous.
MUTATION_LEVELS = ("none", "registers", "books", "moves", "unknown")
# Levels at which a call writes an appointment. A scenario at one of these must
# say what the appointment should be, or its call cannot be graded.
APPOINTMENT_LEVELS = ("books", "moves")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


@dataclass(frozen=True)
class Step:
    """What the caller does when the agent says something matching ``when``."""

    when: re.Pattern[str]
    clips: tuple[str, ...] = ()
    dtmf: str = ""
    # What the clips are meant to say, for the record and for word-level checks.
    intended: str = ""
    # Which persona fact this answer carries (one of KNOWN_FIELDS), if any.
    field: str = ""
    gap_s: float = 0.25
    # Extra wait after the agent finishes before answering.
    delay_s: float = 0.0
    # Start speaking as soon as the agent's line begins, without waiting for it to end.
    talk_over: bool = False
    # Say nothing at all in answer to this line.
    stay_silent: bool = False
    # How many times this step may fire; 0 means no limit.
    max_uses: int = 0


@dataclass(frozen=True)
class ToolOutcome:
    """Where one tool's reply says whether it did the thing, and what "yes" looks like.

    A tool can answer "success" at the transport level and still have refused:
    an identity check that ran and did not match is a successful request. The
    outcome is read from the reply's own body, never from that flag.
    """

    # Dotted path into the tool's reply, for example ``verified`` or ``booking.outcome``.
    path: str
    # Values at that path that mean the tool did what was asked (compared as
    # trimmed text without regard to case). ``*`` accepts any value at all.
    success: tuple[str, ...] = ("true",)


@dataclass(frozen=True)
class ToolExpectation:
    # A fragment of the tool's name. With ``name`` set, the whole name instead.
    name_contains: str
    name: str = ""
    ok: bool = True
    # False means the tool must NOT have been called.
    called: bool = True
    # Argument values the call must have carried, compared as trimmed text
    # without regard to case. Empty means any arguments will do.
    arguments: dict[str, str] = field(default_factory=dict)
    # Named values the tool must have reported back (``ToolCall.outputs``): for a
    # lookup, ``{"status": "not_found"}``. Compared like ``arguments``.
    outputs: dict[str, str] = field(default_factory=dict)
    # Only read with ``ok`` false. By default "is refused" is about how the
    # matter ended: a refusal the agent then got past does not count. Set this
    # for a scenario that only needs the tool to have refused once on the way.
    at_least_once: bool = False


@dataclass(frozen=True)
class BookingExpectation:
    """What the appointment the call writes must be: the caller's intent, as data.

    A booking tool that answers "success" says only that something was booked.
    These say what: each stated part is compared with the appointment as the
    system under test holds it (read-back first, then what the agent's tools
    carried). A part that cannot be found is never taken as right.
    """

    # The appointment must fall on one of these days (names from WEEKDAYS).
    weekdays: tuple[str, ...] = ()
    # ISO dates, inclusive at both ends.
    date_from: str = ""
    date_to: str = ""
    # Local start time, ``HH:MM``, inclusive at both ends.
    time_from: str = ""
    time_to: str = ""
    appointment_type: str = ""
    clinician: str = ""
    # For a cancellation, or any state with no slot: the status the system must hold.
    status: str = ""
    # The time zone the day and time are stated in.
    timezone: str = "Europe/London"
    # The zone the system under test writes a start time in when it gives no
    # offset ("2026-07-01 08:30:00"). A database that stores UTC without saying
    # so is "UTC". Empty means the same zone as ``timezone``.
    naive_times: str = ""
    # The tool that writes the appointment (its whole name, or a fragment). The
    # booked values are read from that tool's calls only, never from a lookup
    # that happens to carry a start time. Not used when a read-back holds them.
    tool: str = ""
    # Names the appointment's parts travel under, when the defaults do not fit.
    start_keys: tuple[str, ...] = ()
    type_keys: tuple[str, ...] = ()
    clinician_keys: tuple[str, ...] = ()
    status_keys: tuple[str, ...] = ()

    def stated(self) -> bool:
        return bool(
            self.weekdays
            or self.date_from
            or self.date_to
            or self.time_from
            or self.time_to
            or self.appointment_type
            or self.clinician
            or self.status
        )


@dataclass(frozen=True)
class Expectations:
    tools: tuple[ToolExpectation, ...] = ()
    # Persona fields whose stored value (what a tool received) must equal the fact.
    fields: tuple[str, ...] = ()
    # Lines the agent must, or must never, speak. Plain patterns over the agent's
    # own words: for outcomes no tool records, such as "told to call 999".
    agent_says: tuple[re.Pattern[str], ...] = ()
    agent_never_says: tuple[re.Pattern[str], ...] = ()
    # Values the system under test must hold after the call, as fetched by the
    # batch's read-back (``readback.py``). With no read-back these cannot be
    # checked and the call is left ungraded rather than passed on the agent's word.
    read_back: dict[str, str] = field(default_factory=dict)
    # What the appointment this call writes must be. Required, with at least one
    # part stated, for a scenario whose ``mutates`` is in APPOINTMENT_LEVELS.
    booking: BookingExpectation = field(default_factory=BookingExpectation)


@dataclass(frozen=True)
class Scenario:
    name: str
    steps: tuple[Step, ...]
    facts: dict[str, str] = field(default_factory=dict)
    hangup_after: re.Pattern[str] | None = None
    max_seconds: float = 120.0
    expect: Expectations = field(default_factory=Expectations)
    # Tool-argument names that carry each field, when the defaults do not fit.
    stored_keys: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # What running this can change on the system under test (MUTATION_LEVELS).
    mutates: str = "unknown"
    # What must already be true on the system under test (free text, for people).
    requires: str = ""
    # Utterances the scenario needs that no recorded clip exists for.
    missing_clips: tuple[str, ...] = ()
    description: str = ""
    # The system under test must be reset before this call (see ``reset.py``).
    clean_state: bool = False
    # What the caller says to a question no step matches, instead of silence.
    fallback: Step | None = None
    # How many identity checks the system under test allows per caller in one
    # window before it locks that caller out. 0 leaves it to the batch's setting.
    identity_budget: int = 0
    # How many of those checks this one call spends (a wrong answer then a right
    # one spends two).
    identity_attempts: int = 1
    # How to read each tool's outcome from its reply, by tool name (a whole
    # name, or failing that a fragment). A tool not listed is read by the
    # platform's own success flag.
    tool_outcomes: dict[str, ToolOutcome] = field(default_factory=dict)
    # The agent ending the call before ``hangup_after`` is a proper end for this
    # scenario (it transfers the caller, say). Without it, a call the agent
    # ends early has not reached its end and cannot pass.
    agent_may_end: bool = False
    # Texts in a tool's reply that mean the system under test was not in the
    # state this scenario needs, by tool name or fragment:
    # ``{"register_patient": ["DUPLICATE_FOUND"]}``. A call that goes wrong after
    # one of these is void with fault "fixture", not a failure of the agent.
    fixture_faults: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Parts of the appointment this scenario leaves to the target's profile
    # (``target.py``) and which the profile in use did not give. A scenario with
    # any is not run: its booking could not be judged.
    missing_target: tuple[str, ...] = ()

    def states_expectations(self) -> bool:
        """True when the scenario asks anything of the call at all.

        One that does not can only ever be "ungraded" (``grading.grade``): a
        call is never passed for having happened. Scenarios written from a
        captured call start this way, on purpose.
        """
        expect = self.expect
        return bool(
            expect.tools
            or expect.fields
            or expect.agent_says
            or expect.agent_never_says
            or expect.read_back
            or expect.booking.stated()
        )


class ScenarioError(ValueError):
    pass


def _pattern(text: str) -> re.Pattern[str]:
    try:
        return re.compile(text, re.IGNORECASE)
    except re.error as error:
        raise ScenarioError(f"bad pattern {text!r}: {error}") from error


_HH_MM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _keys(raw: dict[str, Any], name: str) -> tuple[str, ...]:
    return tuple(str(k) for k in raw.get(name, ()))


def _zone(value: Any, name: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    from synthiq_caller.zones import UnknownZone, time_zone

    try:
        time_zone(text)
    except UnknownZone as error:
        raise ScenarioError(f"expect.booking.{name}: {error}") from error
    # A machine with no zone database at all raises NoZoneDatabase, which says
    # what to install. That is not a fault in the scenario, so it is not caught.
    return text


def parse_booking(raw: Any) -> BookingExpectation:
    if raw is None:
        return BookingExpectation()
    if not isinstance(raw, dict):
        raise ScenarioError("expect.booking must be an object")
    known = set(BookingExpectation.__dataclass_fields__) | {"type"}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ScenarioError(f"expect.booking has unknown parts: {unknown}")
    weekdays = tuple(str(d).lower() for d in raw.get("weekdays", ()))
    bad = [d for d in weekdays if d not in WEEKDAYS]
    if bad:
        raise ScenarioError(f"expect.booking.weekdays: not days of the week: {bad}")
    for name in ("time_from", "time_to"):
        if raw.get(name) and not _HH_MM.match(str(raw[name])):
            raise ScenarioError(f"expect.booking.{name} must be HH:MM, not {raw[name]!r}")
    for name in ("date_from", "date_to"):
        if raw.get(name) and not _ISO_DATE.match(str(raw[name])):
            raise ScenarioError(f"expect.booking.{name} must be YYYY-MM-DD, not {raw[name]!r}")
    return BookingExpectation(
        weekdays=weekdays,
        date_from=str(raw.get("date_from", "")),
        date_to=str(raw.get("date_to", "")),
        time_from=str(raw.get("time_from", "")),
        time_to=str(raw.get("time_to", "")),
        appointment_type=str(raw.get("appointment_type", raw.get("type", ""))),
        clinician=str(raw.get("clinician", "")),
        status=str(raw.get("status", "")),
        timezone=_zone(raw.get("timezone", "Europe/London"), "timezone"),
        naive_times=_zone(raw.get("naive_times", ""), "naive_times"),
        tool=str(raw.get("tool", "")),
        start_keys=_keys(raw, "start_keys"),
        type_keys=_keys(raw, "type_keys"),
        clinician_keys=_keys(raw, "clinician_keys"),
        status_keys=_keys(raw, "status_keys"),
    )


def _fallback(raw: Any) -> Step | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not (raw.get("clips") or raw.get("dtmf")):
        raise ScenarioError("fallback needs clips or dtmf")
    return Step(
        when=_pattern(""),
        clips=tuple(str(c) for c in raw.get("clips", ())),
        dtmf=str(raw.get("dtmf", "")),
        intended=str(raw.get("intended", "")),
        gap_s=float(raw.get("gap_s", 0.25)),
        max_uses=int(raw.get("max_uses", 2)),
    )


def parse_scenario(
    data: dict[str, Any], target: dict[str, Any] | None = None, key: str = ""
) -> Scenario:
    """Make a scenario of ``data``, filled in from the target's profile when one is given.

    ``key`` is the scenario's file name without its ending: the name its
    appointment goes under in the profile's ``bookings``.
    """
    if not isinstance(data.get("name"), str) or not data["name"]:
        raise ScenarioError("a scenario needs a name")
    target = target or {}
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ScenarioError("a scenario needs at least one step")

    steps: list[Step] = []
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict) or not raw.get("when"):
            raise ScenarioError(f"step {index} needs a 'when' pattern")
        gap = raw.get("gap_s", raw.get("gap"))
        step = Step(
            when=_pattern(str(raw["when"])),
            clips=tuple(str(c) for c in raw.get("clips", ())),
            dtmf=str(raw.get("dtmf", "")),
            intended=str(raw.get("intended", "")),
            field=str(raw.get("field", "")),
            gap_s=0.25 if gap is None else float(gap),
            delay_s=float(raw.get("delay_s", 0.0)),
            talk_over=bool(raw.get("talk_over", False)),
            stay_silent=bool(raw.get("stay_silent", False)),
            max_uses=int(raw.get("max_uses", 0)),
        )
        if step.field and step.field not in KNOWN_FIELDS:
            raise ScenarioError(f"step {index}: unknown field {step.field!r}")
        if not (step.clips or step.dtmf or step.stay_silent):
            raise ScenarioError(f"step {index} needs clips, dtmf or stay_silent")
        steps.append(step)

    persona = data.get("persona") or {}
    facts = {str(k): str(v) for k, v in (persona.get("facts") or {}).items()}
    unknown = [k for k in facts if k not in KNOWN_FIELDS]
    if unknown:
        raise ScenarioError(f"unknown persona facts: {unknown}")

    raw_expect = data.get("expect") or {}
    # Parts of the appointment that belong to the system under test, not to
    # the caller's intent, come from the target's profile (``target.py``).
    left_to_target = [str(part) for part in raw_expect.get("booking_from_target", ())]
    unknown_parts = sorted(set(left_to_target) - set(BookingExpectation.__dataclass_fields__))
    if unknown_parts:
        raise ScenarioError(f"expect.booking_from_target names unknown parts: {unknown_parts}")
    raw_booking = raw_expect.get("booking")
    if left_to_target or (target and raw_booking is not None):
        if raw_booking is not None and not isinstance(raw_booking, dict):
            raise ScenarioError("expect.booking must be an object")
        common = {k: target[k] for k in ("timezone", "naive_times") if target.get(k)}
        raw_booking = {
            **common,
            **(raw_booking or {}),
            **((target.get("bookings") or {}).get(key) or {}),
        }
    missing_target = tuple(part for part in left_to_target if not (raw_booking or {}).get(part))
    expect = Expectations(
        tools=tuple(
            ToolExpectation(
                name_contains=str(t.get("name_contains") or t.get("name") or ""),
                name=str(t.get("name", "")),
                ok=bool(t.get("ok", True)),
                called=bool(t.get("called", True)),
                arguments={str(k): str(v) for k, v in (t.get("arguments") or {}).items()},
                outputs={str(k): str(v) for k, v in (t.get("outputs") or {}).items()},
                at_least_once=bool(t.get("at_least_once", False)),
            )
            for t in raw_expect.get("tools", ())
        ),
        fields=tuple(str(f) for f in raw_expect.get("fields", ())),
        agent_says=tuple(_pattern(str(p)) for p in raw_expect.get("agent_says", ())),
        agent_never_says=tuple(_pattern(str(p)) for p in raw_expect.get("agent_never_says", ())),
        read_back={str(k): str(v) for k, v in (raw_expect.get("read_back") or {}).items()},
        booking=parse_booking(raw_booking),
    )
    if any(not t.name_contains for t in expect.tools):
        raise ScenarioError("a tool expectation needs 'name' or 'name_contains'")
    outcomes: dict[str, ToolOutcome] = {}
    raw_outcomes = {**(target.get("tool_outcomes") or {}), **(data.get("tool_outcomes") or {})}
    for tool, raw_outcome in raw_outcomes.items():
        if not isinstance(raw_outcome, dict) or not raw_outcome.get("path"):
            raise ScenarioError(f"tool_outcomes[{tool!r}] needs a 'path' into the tool's reply")
        outcomes[str(tool)] = ToolOutcome(
            path=str(raw_outcome["path"]),
            success=tuple(str(v) for v in raw_outcome.get("success", ("true",))),
        )
    for name in expect.fields:
        if name not in facts:
            raise ScenarioError(f"expect.fields names {name!r} but the persona has no such fact")

    mutates = str(data.get("mutates", "unknown"))
    if mutates not in MUTATION_LEVELS:
        raise ScenarioError(f"mutates must be one of {MUTATION_LEVELS}, not {mutates!r}")

    hangup = data.get("hangup_after")
    return Scenario(
        name=data["name"],
        steps=tuple(steps),
        facts=facts,
        hangup_after=_pattern(str(hangup)) if hangup else None,
        max_seconds=float(data.get("max_seconds", 120.0)),
        expect=expect,
        stored_keys={
            str(k): tuple(str(x) for x in v) for k, v in (data.get("stored_keys") or {}).items()
        },
        mutates=mutates,
        requires=str(data.get("requires", "")),
        missing_clips=tuple(str(m) for m in data.get("missing_clips", ())),
        description=str(data.get("description", "")),
        clean_state=bool(data.get("clean_state", False)),
        fallback=_fallback(data.get("fallback")),
        identity_budget=int(data.get("identity_budget") or target.get("identity_budget") or 0),
        identity_attempts=max(1, int(data.get("identity_attempts", 1))),
        tool_outcomes=outcomes,
        agent_may_end=bool(data.get("agent_may_end", False)),
        fixture_faults={
            str(tool): tuple(str(text) for text in texts)
            for tool, texts in {
                **(target.get("fixture_faults") or {}),
                **(data.get("fixture_faults") or {}),
            }.items()
        },
        missing_target=missing_target,
    )


def read_scenario_file(path: str | Path) -> dict[str, Any]:
    """The object a scenario file holds, before anything is made of it."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-untyped,unused-ignore]
        except ImportError as error:  # pragma: no cover - depends on the environment
            raise ScenarioError("YAML scenarios need PyYAML installed") from error
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ScenarioError("a scenario file must hold one object")
    return data


def load_scenario(path: str | Path, target: dict[str, Any] | None = None) -> Scenario:
    data = read_scenario_file(path)
    if "chain" in data:
        raise ScenarioError("this file declares a chain of calls; load it with load_chain")
    return parse_scenario(data, target, Path(path).stem)
