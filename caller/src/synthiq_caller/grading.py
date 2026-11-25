"""Grading a call from what the agent did, not from what a transcript sounds like.

Two outputs:

* The **said / heard / stored** table. For each persona fact the caller gave:
  what was meant (the scenario), what the agent's recogniser reported hearing
  (the leg), and what the agent's tools actually received (the trace). A call
  can sound perfect and still store the wrong date of birth; this table is where
  that shows.
* The **outcome**: every expectation in the scenario checked against the trace.
  No model's opinion of the transcript is ever consulted.

Outcomes, and who is charged with each (``fault``):

* ``passed``: every check held, the call ended normally, nothing was unknown.
  How the call ended is part of the verdict whether or not a trace exists: a
  call the agent hung up on half-way, or whose line dropped, never passes.
* ``failed``: a check did not hold (fault ``agent``), or the call never
  connected, died before its end, or ran to the time limit (fault ``unknown``:
  the agent, the platform or the line; counted as a failure, not dropped).
* ``void``: the test rig spoiled the call (fault ``rig``): it had no answer to
  a question, or it broke. Not a mark against the agent; re-run and reported.
* ``ungradeable``: the call cannot be judged as the scenario stands: it writes
  an appointment and says nothing of what the appointment should be, or a
  result it depends on was withheld by the platform.
* ``ungraded``: there was no trace, or nothing was asked of the call.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

from synthiq_caller.director import RunRecord
from synthiq_caller.readback import READ_BACK, TOOL_CALL, held_nothing
from synthiq_caller.scenario import (
    APPOINTMENT_LEVELS,
    WEEKDAYS,
    BookingExpectation,
    Scenario,
    ToolExpectation,
    ToolOutcome,
)
from synthiq_caller.traces.base import ToolCall, Trace
from synthiq_caller.traces.common import is_unavailable
from synthiq_caller.zones import time_zone

# Marks a check that could not be made. Such a check is never a pass.
NOT_CHECKED = "NOT CHECKED"

# Endings the director writes (``director.py``).
ENDED_NORMALLY = "caller hung up: scenario end"
ENDED_AT_LIMIT = "caller hung up: time limit"
NOT_ANSWERED = "not answered"
RIG_ERROR = "rig error"
# The line went down under the call: the far end's doing, or the network's.
LINE_DROPPED = "line dropped"
# What the director wrote for any crash of its own before the two were told
# apart. Read as the rig's, so that an old stored run is re-run, not charged.
LEGACY_ERROR = "error:"

# What one tool call came to. ``ERRORED`` is a request that failed on the way
# (a timeout, a 500): the tool gave no answer of its own, so it neither did the
# thing nor refused it.
DID, REFUSED, ERRORED, UNKNOWN = "did", "refused", "errored", "unknown"
_TRANSPORT = re.compile(
    r"\b(?:http\s*)?5\d\d\b|\b4(?:08|29)\b|time[d ]*out|timeout|connection|unreachable|"
    r"network|\bdns\b|socket|gateway",
    re.IGNORECASE,
)

DEFAULT_START_KEYS = (
    "appointment_start", "appointmentStart", "startTime", "start_time", "startsAt",
    "starts_at", "slotStart", "slot_start", "newStartTime", "newStart", "start",
)  # fmt: skip
DEFAULT_TYPE_KEYS = ("appointment_type", "appointmentType")
DEFAULT_CLINICIAN_KEYS = ("clinician", "practitionerName", "practitioner_name", "practitioner")
DEFAULT_STATUS_KEYS = ("appointment_status", "appointmentStatus", "status")

DEFAULT_STORED_KEYS: dict[str, tuple[str, ...]] = {
    "name": ("fullName", "full_name", "name", "patientName"),
    "date_of_birth": ("dateOfBirth", "date_of_birth", "dob"),
    "postcode": ("postcode", "postalCode", "postal_code"),
    "phone": ("phone", "phoneNumber", "phone_number", "mobile"),
}

_MONTHS = {
    name: index
    for index, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}
_UNITS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
    "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13,
    "fourteenth": 14, "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
    "nineteenth": 19, "oh": 0, "zero": 0,
}  # fmt: skip
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90, "twentieth": 20, "thirtieth": 30,
}  # fmt: skip
_FILLER = re.compile(
    r"^(?:(?:uh|um|er|it'?s|it is|my name is|the postcode is|postcode is|is)\b[\s,]*)+", re.I
)


def _number_words(tokens: list[str]) -> list[int | str]:
    """Collapse runs like ``twenty``, ``first`` into 21; leave other tokens alone."""
    out: list[int | str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.isdigit():
            out.append(int(tok))
        elif tok in _TENS:
            value = _TENS[tok]
            if i + 1 < len(tokens) and tokens[i + 1] in _UNITS and 0 < _UNITS[tokens[i + 1]] < 10:
                value += _UNITS[tokens[i + 1]]
                i += 1
            out.append(value)
        elif tok in _UNITS:
            out.append(_UNITS[tok])
        else:
            out.append(tok)
        i += 1
    return out


def _spoken_year(items: list[int | str]) -> int | None:
    """Years said as words: ``nineteen eighty-seven``, ``two thousand and two``."""
    for i, item in enumerate(items):
        nxt = items[i + 1] if i + 1 < len(items) else None
        if item in (19, 20) and isinstance(nxt, int) and 0 <= nxt <= 99:
            return int(item) * 100 + nxt
        if item == 2 and nxt == "thousand":
            rest = [x for x in items[i + 2 : i + 4] if isinstance(x, int)]
            return 2000 + (rest[0] if rest else 0)
    return None


def normalise_date(text: str) -> str | None:
    """A spoken or written date as ``YYYY-MM-DD``, day first; None when unclear."""
    if not text:
        return None
    lowered = text.lower().strip()
    iso = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", lowered)
    if iso:
        return _build(int(iso[1]), int(iso[2]), int(iso[3]))
    numeric = re.search(r"\b(\d{1,2})[/.\s-](\d{1,2})[/.\s-](\d{4})\b", lowered)
    if numeric:
        return _build(int(numeric[3]), int(numeric[2]), int(numeric[1]))

    cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", lowered)
    tokens = re.findall(r"[a-z]+|\d+", cleaned.replace("-", " "))
    month = next((_MONTHS[t] for t in tokens if t in _MONTHS), None)
    if month is None:
        return None
    month_at = next(i for i, t in enumerate(tokens) if t in _MONTHS)
    before = _number_words(tokens[:month_at])
    after = _number_words(tokens[month_at + 1 :])
    items = before + after
    year = next((x for x in items if isinstance(x, int) and 1900 <= x <= 2100), None)
    if year is None:
        year = _spoken_year(after) or _spoken_year(before)
    day = next((x for x in before if isinstance(x, int) and 1 <= x <= 31), None)
    if day is None:
        day = next((x for x in after if isinstance(x, int) and 1 <= x <= 31), None)
    if year is None or day is None:
        return None
    return _build(year, month, day)


def _build(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def normalise_postcode(text: str) -> str | None:
    stripped = _FILLER.sub("", text.strip())
    value = re.sub(r"[^A-Za-z0-9]", "", stripped).upper()
    return value or None


def normalise_phone(text: str) -> str | None:
    digits = re.sub(r"\D", "", text)
    if digits.startswith("44") and len(digits) > 10:
        digits = "0" + digits[2:]
    return digits or None


def normalise_name(text: str) -> str | None:
    stripped = _FILLER.sub("", text.strip())
    value = " ".join(re.findall(r"[a-z']+", stripped.lower()))
    return value or None


NORMALISERS = {
    "name": normalise_name,
    "date_of_birth": normalise_date,
    "postcode": normalise_postcode,
    "phone": normalise_phone,
}


def same(field_name: str, a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    norm = NORMALISERS[field_name]
    left, right = norm(a), norm(b)
    return left is not None and left == right


@dataclass
class FieldRow:
    field: str
    said: str
    heard: str | None
    stored: str | None
    heard_matches: bool | None
    stored_matches: bool | None
    verdict: str
    # Where ``stored`` came from: "read back" (fetched from the system under test
    # after the call) or "tool call" (what the agent's tool was sent). Empty when
    # nothing was stored.
    stored_source: str = ""
    # The clip that carried this answer has no label a person has confirmed, so
    # "said" is another recogniser's reading of it, not a known fact.
    labels_unverified: bool = False
    # A synthetic voice gave this answer, so "heard" says nothing about how the
    # agent hears a real caller.
    synthetic: bool = False


@dataclass
class Check:
    what: str
    passed: bool


@dataclass
class Grade:
    scenario: str
    call_ref: str | None
    outcome: str  # "passed", "failed", "void", "ungradeable" or "ungraded"
    checks: list[Check] = field(default_factory=list)
    fields: list[FieldRow] = field(default_factory=list)
    # Who the outcome is charged to: "agent", "rig", "fixture" or "unknown".
    # Empty for a pass and for a call nobody is charged with.
    fault: str = ""
    # Why, in a sentence, whenever the outcome is not a plain pass or fail.
    reason: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _stored_value(trace: Trace, keys: tuple[str, ...]) -> str | None:
    """The last value any tool received under one of ``keys``."""
    found: str | None = None
    for call in trace.tool_calls:
        for key in keys:
            value = call.arguments.get(key)
            if isinstance(value, str | int) and str(value).strip():
                found = str(value)
    return found


def _carries(arguments: dict[str, Any], wanted: dict[str, str]) -> bool:
    """Whether a tool call's arguments hold every wanted value."""
    for key, value in wanted.items():
        got = arguments.get(key)
        if got is None or str(got).strip().casefold() != value.strip().casefold():
            return False
    return True


def field_rows(
    scenario: Scenario,
    record: RunRecord,
    trace: Trace | None,
    read_back: dict[str, str] | None = None,
) -> list[FieldRow]:
    rows: list[FieldRow] = []
    for name, fact in scenario.facts.items():
        answers = [said for said in record.said if said.field == name]
        if not answers:
            continue
        heard_all = [text for said in answers for text in said.heard]
        heard = heard_all[-1] if heard_all else None
        keys = scenario.stored_keys.get(name) or DEFAULT_STORED_KEYS[name]
        # The system's own record wins; what the tool was sent is the fallback,
        # and only when the read-back did not speak of this field at all. A
        # read-back that names the field and holds nothing is the answer.
        emptied = held_nothing(read_back, name)
        stored: str | None = None
        source = ""
        if read_back is not None and name in read_back:
            stored = read_back[name].strip() or None
            source = READ_BACK
        elif trace is not None:
            stored = _stored_value(trace, keys)
            source = TOOL_CALL if stored is not None else ""
        heard_ok = same(name, fact, heard) if heard is not None else None
        stored_ok = same(name, fact, stored) if stored is not None else None
        if emptied:
            stored_ok = False
            verdict = "not stored: the system holds nothing (read back)"
        elif stored_ok and source == TOOL_CALL:
            # The agent's tool was sent the right value. Whether the system kept
            # it is not known without a read-back.
            verdict = "sent correctly, not confirmed stored"
        elif stored_ok:
            verdict = "stored correctly"
        elif stored is None:
            verdict = "not stored" if (trace is not None or read_back is not None) else "no trace"
        elif heard_ok:
            verdict = "heard correctly, stored wrongly"
        elif heard is None:
            verdict = "stored wrongly"
        else:
            verdict = "misheard, stored wrongly"
        unverified = any(said.labels_unverified for said in answers)
        if unverified:
            verdict += " (labels unverified)"
        synthetic = any(said.synthetic_clips for said in answers)
        if synthetic:
            verdict += " (synthetic voice)"
        rows.append(
            FieldRow(
                name,
                fact,
                heard,
                stored,
                heard_ok,
                stored_ok,
                verdict,
                source,
                unverified,
                synthetic,
            )  # fmt: skip
        )
    return rows


def _find(value: Any, keys: tuple[str, ...]) -> str | None:
    """The value under one of ``keys`` in an object, looking inside nested objects.

    Lists are not searched: a list in a tool's reply is a set of candidates (the
    slots on offer, the clinicians who work there), not the one that was chosen.
    """
    if not isinstance(value, dict):
        return None
    wanted = {k.casefold() for k in keys}
    for key, item in value.items():
        if str(key).casefold() in wanted and isinstance(item, str | int | float):
            text = str(item).strip()
            if text:
                return text
    for item in value.values():
        found = _find(item, keys)
        if found is not None:
            return found
    return None


def _named(call: ToolCall, tool: str) -> bool:
    """Whether ``call`` is the tool ``tool`` names: its whole name, or a fragment of it."""
    return tool.casefold() in call.name.casefold()


def _held(
    keys: tuple[str, ...],
    trace: Trace,
    read_back: dict[str, str] | None,
    rules: dict[str, ToolOutcome] | None = None,
    tool: str = "",
) -> tuple[str | None, str, str]:
    """A value of the written appointment, where it came from, and why not if not.

    The system's own record wins, and a record that names the value and holds
    nothing is an answer ("nothing was written"), not a gap to fill from the
    agent's account. Failing a read-back, the value comes from the call that
    wrote the appointment: ``tool`` when the scenario names it. A later lookup
    that happens to carry a start time is not the booking. A value the writing
    tool does not carry at all (the appointment type, when an earlier tool was
    given it) is taken from the other tools only when they agree on it; and
    with no tool named, a value is taken only when every tool that did its job
    agrees on it. Tools that disagree leave the value unknown, never guessed.
    """
    if read_back:
        wanted = {k.casefold() for k in keys}
        named = [k for k in read_back if k.casefold() in wanted]
        if named:
            value = next((read_back[k].strip() for k in named if read_back[k].strip()), "")
            if not value:
                return None, READ_BACK, "the system holds nothing (read back)"
            return value, READ_BACK, ""
        nested = _find(read_back, keys)
        if nested is not None:
            return nested, READ_BACK, ""
    written: list[str] = []  # carried by the tool that writes the appointment
    carried: list[tuple[str, str]] = []  # carried by any other tool
    for call in trace.tool_calls:
        if tool_outcome(call, rules or {})[0] != DID:
            continue
        got = _find(call.arguments, keys) or _find(call.outputs, keys) or _find(call.result, keys)
        if got is None:
            continue
        if tool and _named(call, tool):
            written.append(got)
        else:
            carried.append((call.name.casefold(), got))
    if written:
        return written[-1], TOOL_CALL, ""
    if not carried:
        return None, "", ""
    tools = {name for name, _ in carried}
    if len(tools) > 1 and len({text.casefold() for _, text in carried}) > 1:
        return (
            None,
            "",
            f"tools {sorted(tools)} carried different values; name the one that writes the "
            "appointment (expect.booking.tool)",
        )
    return carried[-1][1], TOOL_CALL, ""


_SPOKEN_TIME = re.compile(
    r"\b(\d{1,2})[:.](\d{2})\s*(am|pm)?\b|\b(\d{1,2})\s*(am|pm)\b", re.IGNORECASE
)


def parse_when(
    text: str, timezone: str, naive_zone: str = ""
) -> tuple[date | None, str | None, int | None]:
    """A start time as (date, day of the week, minutes past midnight), each when known.

    An ISO date-time gives all three, in ``timezone``. One that carries no
    offset is taken to be in ``naive_zone``, the zone the system under test
    writes such times in (a database that stores UTC without saying so is
    ``UTC``); left empty, that is ``timezone`` itself. Anything else is read as
    words: a day name and a clock time.
    """
    cleaned = text.strip()
    moment: datetime | None
    try:
        moment = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        moment = None
    if moment is not None and ("T" in cleaned or " " in cleaned):
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=time_zone(naive_zone or timezone))
        moment = moment.astimezone(time_zone(timezone))
        return moment.date(), WEEKDAYS[moment.weekday()], moment.hour * 60 + moment.minute
    if moment is not None:
        return moment.date(), WEEKDAYS[moment.weekday()], None
    lowered = cleaned.lower()
    day = next((d for d in WEEKDAYS if d in lowered), None)
    minutes: int | None = None
    spoken = _SPOKEN_TIME.search(lowered)
    if spoken:
        hour = int(spoken[1] or spoken[4])
        minute = int(spoken[2] or 0)
        half = (spoken[3] or spoken[5] or "").lower()
        if half == "pm" and hour < 12:
            hour += 12
        if half == "am" and hour == 12:
            hour = 0
        if hour < 24 and minute < 60:
            minutes = hour * 60 + minute
    if minutes is None:
        minutes = _time_in_words(lowered)
    return None, day, minutes


_HOURS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
          "eleven", "twelve")  # fmt: skip
_WORD_TIME = re.compile(
    r"(?:(half past|quarter past|quarter to|[a-z\- ]+? minutes? (?:past|to)) )?"
    r"\b(" + "|".join(_HOURS) + r"|midday|noon|midnight)\b(?: o'?clock)?"
    r"(?: in the (morning|afternoon|evening)| ?(a\.?m|p\.?m)\b)?"
)


def _time_in_words(text: str) -> int | None:
    """Minutes past midnight for a time said in words; None unless it is unambiguous.

    "at ten in the morning", "half past four in the afternoon", "quarter to
    five in the evening", "midday". An hour with no morning or afternoon is not
    guessed.
    """
    tail = text.rsplit(" at ", 1)[-1]
    for found in _WORD_TIME.finditer(tail):
        lead, hour_word, period, half = found.groups()
        if hour_word in ("midday", "noon"):
            hour = 12
        elif hour_word == "midnight":
            hour = 0
        else:
            period = period or {"a": "morning", "p": "afternoon"}.get((half or " ")[0])
            if period is None:
                continue
            hour = _HOURS.index(hour_word) + 1
            if period == "morning":
                hour = 0 if hour == 12 else hour
            elif hour < 12:
                hour += 12
        minutes = hour * 60
        if lead == "half past":
            minutes += 30
        elif lead == "quarter past":
            minutes += 15
        elif lead == "quarter to":
            minutes -= 15
        elif lead:
            count = [
                x for x in _number_words(re.findall(r"[a-z]+|\d+", lead)) if isinstance(x, int)
            ]
            if not count:
                return None
            # "twenty" then "three" arrive as one number already; a stray pair is summed.
            amount = sum(count)
            minutes += amount if lead.endswith("past") else -amount
        return minutes % (24 * 60)
    return None


def _minutes(clock: str) -> int:
    hour, minute = clock.split(":")
    return int(hour) * 60 + int(minute)


def booking_checks(
    booking: BookingExpectation,
    trace: Trace,
    read_back: dict[str, str] | None,
    rules: dict[str, ToolOutcome] | None = None,
) -> list[Check]:
    """One check per stated part of the appointment. A part not found is not checked.

    A part the system was asked for and holds nothing under is found: it is
    wrong, and the check fails.
    """
    checks: list[Check] = []

    def missing(what: str, source: str = "", why: str = "") -> None:
        if source == READ_BACK:
            checks.append(Check(f"{what} ({why})", False))
            return
        reason = why or "the booked value is not available"
        checks.append(Check(f"{what} ({NOT_CHECKED}: {reason})", False))

    wants_start = (
        booking.weekdays
        or booking.date_from
        or booking.date_to
        or booking.time_from
        or booking.time_to
    )
    if wants_start:
        start, source, why = _held(
            booking.start_keys or DEFAULT_START_KEYS, trace, read_back, rules, booking.tool
        )
        on_date, weekday, minutes = (
            parse_when(start, booking.timezone, booking.naive_times)
            if start
            else (None, None, None)
        )
        shown = f" (booked: {start}, from {source})" if start else ""
        if booking.weekdays:
            what = f"the appointment is on {' or '.join(booking.weekdays)}"
            if weekday is None:
                missing(what, source, why)
            else:
                checks.append(Check(what + shown, weekday in booking.weekdays))
        if booking.date_from or booking.date_to:
            what = (
                f"the appointment date is within {booking.date_from or 'any'} to "
                f"{booking.date_to or 'any'}"
            )
            if on_date is None:
                missing(what, source, why)
            else:
                after = not booking.date_from or on_date >= date.fromisoformat(booking.date_from)
                before = not booking.date_to or on_date <= date.fromisoformat(booking.date_to)
                checks.append(Check(what + shown, after and before))
        if booking.time_from or booking.time_to:
            what = (
                f"the appointment starts within {booking.time_from or '00:00'} to "
                f"{booking.time_to or '23:59'}"
            )
            if minutes is None:
                missing(what, source, why)
            else:
                low = _minutes(booking.time_from) if booking.time_from else 0
                high = _minutes(booking.time_to) if booking.time_to else 24 * 60
                # 22:00 to 02:00 is a window across midnight, not an empty one.
                if low <= high:
                    inside = low <= minutes <= high
                else:
                    inside = minutes >= low or minutes <= high
                checks.append(Check(what + shown, inside))

    for wanted, keys, what in (
        (
            booking.appointment_type,
            booking.type_keys or DEFAULT_TYPE_KEYS,
            f"the appointment type is {booking.appointment_type}",
        ),
        (
            booking.clinician,
            booking.clinician_keys or DEFAULT_CLINICIAN_KEYS,
            f"the appointment is with {booking.clinician}",
        ),
        (
            booking.status,
            booking.status_keys or DEFAULT_STATUS_KEYS,
            f"the appointment status is {booking.status}",
        ),
    ):
        if not wanted:
            continue
        got, source, why = _held(keys, trace, read_back, rules, booking.tool)
        if got is None:
            missing(what, source, why)
        else:
            held = wanted.strip().casefold() in got.strip().casefold()
            checks.append(Check(f"{what} (booked: {got}, from {source})", held))
    return checks


def _at_path(value: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def outcome_rule(name: str, rules: dict[str, ToolOutcome]) -> ToolOutcome | None:
    """The rule for a tool: its whole name first, else the longest fragment of it."""
    lowered = name.casefold()
    for key, rule in rules.items():
        if key.casefold() == lowered:
            return rule
    fragments = [key for key in rules if key.casefold() in lowered]
    return rules[max(fragments, key=len)] if fragments else None


def tool_outcome(call: ToolCall, rules: dict[str, ToolOutcome]) -> tuple[str, str]:
    """What one tool call came to: ``DID``, ``REFUSED``, ``ERRORED`` or ``UNKNOWN``, and why.

    With a rule for the tool the answer is read from the tool's own reply; the
    platform's success flag then only says the request went through. A request
    that failed on the way is ``ERRORED``: the tool did not do the thing, and it
    did not refuse it either, because it never answered.
    """
    if is_unavailable(call):
        return UNKNOWN, "the platform withheld the tool's result"
    rule = outcome_rule(call.name, rules)
    if not call.ok:
        if rule is not None or _TRANSPORT.search(call.error or ""):
            detail = f" ({call.error})" if call.error else ""
            return ERRORED, f"the request failed before the tool could answer{detail}"
        return REFUSED, ""  # no rule: the platform's flag is all there is to go on
    if rule is None:
        return DID, ""
    # Some engines keep a tool's answer in ``outputs`` and only a bare
    # acknowledgement in ``result``; the answer is looked for in both.
    value = _at_path(call.outputs, rule.path)
    if value is None:
        value = _at_path(call.result, rule.path)
    if value is None:
        return UNKNOWN, f"the tool's reply does not carry '{rule.path}'"
    said = str(value).strip().casefold()
    if "*" in rule.success:
        return (DID if said else REFUSED), ""  # any value at all: a reference was handed back
    return (DID if said in {v.strip().casefold() for v in rule.success} else REFUSED), ""


def tool_state(call: ToolCall, rules: dict[str, ToolOutcome]) -> tuple[bool | None, str]:
    """Whether a tool did what it was asked: True, False, or None for "not known"."""
    outcome, why = tool_outcome(call, rules)
    if outcome == UNKNOWN:
        return None, why
    return outcome == DID, why


def _tool_check(
    expected: ToolExpectation,
    matching: list[ToolCall],
    label: str,
    rules: dict[str, ToolOutcome],
) -> Check:
    """Whether the tool did what was expected: yes, no, or not known.

    "Succeeds" is met by any call that did the thing. "Is refused" is about how
    the matter ended: it is met when the last call was refused by the tool
    itself. A refusal the agent then got past is not a refusal, unless the
    scenario asks for one ``at_least_once``. A request that failed on the way,
    or a result the platform withheld, satisfies neither.
    """
    outcomes = [tool_outcome(call, rules) for call in matching]
    if expected.ok:
        what = f"{label} is called and succeeds"
        if any(outcome == DID for outcome, _ in outcomes):
            return Check(what, True)
        not_known = [why for outcome, why in outcomes if outcome == UNKNOWN]
        if not_known:
            return Check(f"{what} ({NOT_CHECKED}: {not_known[-1]})", False)
        return Check(what, False)
    what = f"{label} is called and is refused" + (
        " at least once" if expected.at_least_once else ""
    )
    considered = outcomes if expected.at_least_once else outcomes[-1:]
    if any(outcome == REFUSED for outcome, _ in considered):
        return Check(what, True)
    not_known = [why for outcome, why in considered if outcome in (UNKNOWN, ERRORED)]
    if not_known:
        return Check(f"{what} ({NOT_CHECKED}: {not_known[-1]})", False)
    return Check(what, False)


def grade(
    scenario: Scenario,
    record: RunRecord,
    trace: Trace | None,
    read_back: dict[str, str] | None = None,
) -> Grade:
    """Grade one call. ``read_back`` is what the system under test held afterwards.

    None means no read-back was made (none configured, or it failed); "stored"
    then comes from what the agent's tools were sent, and says so.
    """
    rows = field_rows(scenario, record, trace, read_back)
    result = Grade(
        scenario=scenario.name, call_ref=record.call_ref, outcome="ungraded", fields=rows
    )
    ended = record.ended or ""
    if ended.startswith((RIG_ERROR, LEGACY_ERROR)):
        result.outcome, result.fault = "void", "rig"
        result.reason = f"the test rig broke: {ended}"
        return result
    if ended.startswith(NOT_ANSWERED):
        result.outcome, result.fault = "failed", "unknown"
        result.reason = f"the call was {ended}"
        return result
    unanswered = list(record.unanswered)
    # How the call ended is judged whether or not there is a trace.
    normal = ended.startswith(ENDED_NORMALLY)
    at_limit = ended.startswith(ENDED_AT_LIMIT)
    # A scenario with no line to hang up on, or one that says the agent may end
    # the call, ends properly when the agent ends it. A dropped line never does.
    open_ended = scenario.hangup_after is None or scenario.agent_may_end
    agent_ended = bool(ended) and not normal and not at_limit
    acceptable = normal or (open_ended and agent_ended and not ended.startswith(LINE_DROPPED))
    if trace is None:
        if unanswered and not normal:
            result.outcome, result.fault = "void", "rig"
            result.reason = f"the caller had no answer to: {unanswered[-1]!r}"
        elif not acceptable and not at_limit:
            # The agent's side ended the call before the scenario's end and left
            # nothing behind. That is a broken call, not one to leave out.
            result.outcome, result.fault = "failed", "unknown"
            result.reason = f"the call ended before the scenario's end and left no trace: {ended}"
        else:
            result.reason = "no trace of the call could be read"
        return result

    for expected in scenario.expect.tools:
        matching = [
            c
            for c in trace.tool_calls
            if (
                c.name.casefold() == expected.name.casefold()
                if expected.name
                else expected.name_contains.lower() in c.name.lower()
            )
            and _carries(c.arguments, expected.arguments)
        ]
        label = f"tool '{expected.name or expected.name_contains}'"
        if expected.arguments:
            label += " with " + ", ".join(f"{k}={v}" for k, v in expected.arguments.items())
        if not expected.called:
            result.checks.append(Check(f"{label} is not called", not matching))
            continue
        result.checks.append(_tool_check(expected, matching, label, scenario.tool_outcomes))
        for key, value in expected.outputs.items():
            what = f"{label} reports {key}={value}"
            if any(_carries(c.outputs, {key: value}) for c in matching):
                result.checks.append(Check(what, True))
            elif matching and not any(key in c.outputs for c in matching):
                # The tool ran, and the trace does not say what it reported.
                result.checks.append(
                    Check(f"{what} ({NOT_CHECKED}: the trace does not carry '{key}')", False)
                )
            else:
                result.checks.append(Check(what, False))

    by_field = {row.field: row for row in rows}
    for name in scenario.expect.fields:
        row = by_field.get(name)
        result.checks.append(
            Check(
                f"'{name}' reaches a tool as the caller said it", bool(row and row.stored_matches)
            )
        )

    agent_lines = [str(e.get("text", "")) for e in record.timeline if e.get("kind") == "agent"]
    for pattern in scenario.expect.agent_says:
        result.checks.append(
            Check(
                f"the agent says /{pattern.pattern}/",
                any(pattern.search(line) for line in agent_lines),
            )
        )
    said_forbidden = False
    for pattern in scenario.expect.agent_never_says:
        clean = not any(pattern.search(line) for line in agent_lines)
        said_forbidden = said_forbidden or not clean
        result.checks.append(Check(f"the agent never says /{pattern.pattern}/", clean))

    for key, wanted in scenario.expect.read_back.items():
        if read_back is None:
            result.checks.append(
                Check(f"the system holds {key}={wanted} ({NOT_CHECKED}: no read-back)", False)
            )
            continue
        got = read_back.get(key)
        held = got is not None and got.strip().casefold() == wanted.strip().casefold()
        result.checks.append(Check(f"the system holds {key}={wanted} (read back)", held))

    result.checks.extend(
        booking_checks(scenario.expect.booking, trace, read_back, scenario.tool_outcomes)
    )

    if not result.checks:
        result.reason = "the scenario asks nothing of the call"
        return result  # nothing was asked of this call, so nothing can be claimed

    if at_limit:
        result.checks.append(
            Check("the call reaches its end (it ran to the time limit instead)", False)
        )
    elif not acceptable:
        # The checks can all hold on a call that died half-way: the tools ran,
        # then the agent hung up or the line went. That call did not succeed.
        result.checks.append(
            Check(
                f"the call reaches its end (it ended early: {ended or 'no ending was recorded'})",
                False,
            )
        )

    unknown = [c for c in result.checks if NOT_CHECKED in c.what]
    wrong = [c for c in result.checks if not c.passed and NOT_CHECKED not in c.what]
    how_it_ended = ("time limit", "ended early")
    agent_wrong = [c for c in wrong if not any(mark in c.what for mark in how_it_ended)]

    spoiled = not acceptable or bool(wrong) or bool(unknown)
    if unanswered and not normal and spoiled and not said_forbidden:
        # The caller went silent on a question. Whatever followed is the rig's doing.
        result.outcome, result.fault = "void", "rig"
        result.reason = f"the caller had no answer to: {unanswered[-1]!r}"
        return result
    if wrong:
        result.outcome = "failed"
        result.fault = "agent" if agent_wrong else "unknown"
        if not agent_wrong:
            result.reason = (
                "the call ran to the time limit without reaching its end"
                if at_limit
                else f"the call ended before the scenario's end: {ended or 'no ending recorded'}"
            )
        return result
    says_what = (
        scenario.expect.booking.stated()
        or scenario.expect.read_back
        or any(t.arguments for t in scenario.expect.tools)
    )
    if scenario.mutates in APPOINTMENT_LEVELS and not says_what:
        result.outcome = "ungradeable"
        result.reason = (
            f"the scenario's level is '{scenario.mutates}' (it writes an appointment) and it "
            "states nothing about what the appointment should be (expect.booking)"
        )
        return result
    if unknown:
        withheld = any("no read-back" not in c.what for c in unknown)
        result.outcome = "ungradeable" if withheld else "ungraded"
        result.reason = "; ".join(c.what for c in unknown)
        return result
    result.outcome = "passed"
    return result
