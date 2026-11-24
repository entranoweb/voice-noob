"""Grading a call from what the agent did, not from what a transcript sounds like.

Two outputs:

* The **said / heard / stored** table. For each persona fact the caller gave:
  what was meant (the scenario), what the agent's recogniser reported hearing
  (the leg), and what the agent's tools actually received (the trace). A call
  can sound perfect and still store the wrong date of birth; this table is where
  that shows.
* The **outcome**: every expectation in the scenario checked against the trace.
  With no trace there is no outcome, only "ungraded". No model's opinion of the
  transcript is ever consulted.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from synthiq_caller.director import RunRecord
from synthiq_caller.scenario import Scenario
from synthiq_caller.traces.base import Trace

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


@dataclass
class Check:
    what: str
    passed: bool


@dataclass
class Grade:
    scenario: str
    call_ref: str | None
    outcome: str  # "passed", "failed" or "ungraded"
    checks: list[Check] = field(default_factory=list)
    fields: list[FieldRow] = field(default_factory=list)

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


def field_rows(scenario: Scenario, record: RunRecord, trace: Trace | None) -> list[FieldRow]:
    rows: list[FieldRow] = []
    for name, fact in scenario.facts.items():
        answers = [said for said in record.said if said.field == name]
        if not answers:
            continue
        heard_all = [text for said in answers for text in said.heard]
        heard = heard_all[-1] if heard_all else None
        keys = scenario.stored_keys.get(name) or DEFAULT_STORED_KEYS[name]
        stored = _stored_value(trace, keys) if trace is not None else None
        heard_ok = same(name, fact, heard) if heard is not None else None
        stored_ok = same(name, fact, stored) if stored is not None else None
        if stored_ok:
            verdict = "stored correctly"
        elif stored is None:
            verdict = "not stored" if trace is not None else "no trace"
        elif heard_ok:
            verdict = "heard correctly, stored wrongly"
        elif heard is None:
            verdict = "stored wrongly"
        else:
            verdict = "misheard, stored wrongly"
        rows.append(FieldRow(name, fact, heard, stored, heard_ok, stored_ok, verdict))
    return rows


def grade(scenario: Scenario, record: RunRecord, trace: Trace | None) -> Grade:
    rows = field_rows(scenario, record, trace)
    result = Grade(
        scenario=scenario.name, call_ref=record.call_ref, outcome="ungraded", fields=rows
    )
    if trace is None:
        return result

    for expected in scenario.expect.tools:
        matching = [c for c in trace.tool_calls if expected.name_contains.lower() in c.name.lower()]
        if not expected.called:
            result.checks.append(
                Check(f"tool '{expected.name_contains}' is not called", not matching)
            )
            continue
        wanted = "succeeds" if expected.ok else "is refused"
        passed = any(c.ok == expected.ok for c in matching)
        result.checks.append(
            Check(f"tool '{expected.name_contains}' is called and {wanted}", passed)
        )

    by_field = {row.field: row for row in rows}
    for name in scenario.expect.fields:
        row = by_field.get(name)
        result.checks.append(
            Check(
                f"'{name}' reaches a tool as the caller said it", bool(row and row.stored_matches)
            )
        )

    if not result.checks:
        return result  # nothing was asked of this call, so nothing can be claimed
    result.outcome = "passed" if all(c.passed for c in result.checks) else "failed"
    return result
