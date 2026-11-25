"""The scenario reference page, generated from the scenario schema.

    synthiq-caller reference                      print the page
    synthiq-caller reference --out docs/SCENARIO-REFERENCE.md
    synthiq-caller reference --check docs/SCENARIO-REFERENCE.md   exit 1 if it differs

The schema is the dataclasses in ``scenario.py`` (``Scenario``, ``Step``,
``Expectations`` and the rest) and the constants beside them. Everything the
page says about a field's name, type and default is read from them, so a field
added or changed there changes the page. What a dataclass cannot say, what a
field means and the JSON key it goes by when that differs from its attribute,
is written down below. A test fails when a field has no entry here, when an
entry names a field that no longer exists, and when the committed page differs
from the one this module makes.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import types
import typing
from pathlib import Path
from typing import Any

from synthiq_caller import scenario as schema
from synthiq_caller.target import ALLOWANCES

# The JSON key a dataclass field goes by, where it is not its own name.
JSON_KEY = {"Scenario.facts": "persona.facts"}
# Spellings the parser also takes for the same field.
ALSO_ACCEPTED = {
    "Step.gap_s": "gap",
    "ToolExpectation.name_contains": "name",
    "BookingExpectation.appointment_type": "type",
}
# Fields that are never written in a file: the rig fills them in.
DERIVED = {
    "Scenario.missing_target": (
        "Set by the rig, never written in a file: the parts named in "
        "`expect.booking_from_target` that the target profile did not give. A scenario "
        "with any is held back."
    ),
}

# What each field means. Keyed "Class.field"; every dataclass field needs one.
MEANING = {
    "Scenario.name": "A name for the scenario. Required.",
    "Scenario.steps": "What the caller does, by what the agent says. At least one. See Step.",
    "Scenario.facts": (
        "The persona: facts the caller gives (see Allowed values). "
        "Written as `persona: {facts: {...}}`."
    ),
    "Scenario.hangup_after": (
        "Pattern over the agent's words. When the agent says a line matching it, the caller "
        "has reached the end of what it came to do and hangs up."
    ),
    "Scenario.max_seconds": "Time limit for the call. A call that hits it cannot pass.",
    "Scenario.expect": "What must be true of the call. See Expectations.",
    "Scenario.stored_keys": (
        "Tool-argument names that carry each persona field, when the defaults do not fit: "
        '`{"postcode": ["zip"]}`.'
    ),
    "Scenario.mutates": (
        "What running this can change on the system under test. The default is the most "
        "dangerous reading, so a scenario that says nothing is not run by default."
    ),
    "Scenario.requires": (
        "What must already be true on the system under test. Free text, for people."
    ),
    "Scenario.missing_clips": (
        "Utterances the scenario needs that no recorded clip exists for. A scenario that "
        "lists any is not runnable."
    ),
    "Scenario.description": "Free text, for people.",
    "Scenario.clean_state": "The system under test must be reset before this call.",
    "Scenario.fallback": (
        "What the caller says to a question no step matches, instead of silence: an object "
        "with `clips` or `dtmf`, and optionally `intended`, `gap_s`, `max_uses` (default 2)."
    ),
    "Scenario.identity_budget": (
        "How many identity checks the system under test allows per caller in one window. "
        "0 leaves it to the batch's setting."
    ),
    "Scenario.identity_attempts": (
        "How many of those checks this one call spends (a wrong answer then a right one is two)."
    ),
    "Scenario.tool_outcomes": (
        "How to read each tool's outcome from its reply, by tool name or fragment. See "
        "ToolOutcome. A tool not listed is read by the platform's own success flag."
    ),
    "Scenario.agent_may_end": (
        "The agent ending the call before `hangup_after` is a proper end here (it transfers "
        "the caller, say). Without it, a call the agent ends early cannot pass."
    ),
    "Scenario.fixture_faults": (
        "Texts in a tool's reply that mean the system was not in the state this scenario "
        'needs: `{"register_patient": ["DUPLICATE_FOUND"]}`. A call that goes wrong after '
        "one is void, not a failure of the agent."
    ),
    "Step.when": (
        "Pattern over the agent's line (case-insensitive). The step fires when it matches."
    ),
    "Step.clips": ("Recorded clips to play, by name in the clip bank. Joined into one utterance."),
    "Step.dtmf": "Keypad digits to send instead of, or as well as, speech.",
    "Step.intended": "What the clips are meant to say. For the record and for word-level checks.",
    "Step.field": "Which persona fact this answer carries, if any.",
    "Step.gap_s": "Seconds of silence between clips.",
    "Step.delay_s": "Extra wait after the agent finishes before answering.",
    "Step.talk_over": (
        "Start speaking as soon as the agent's line begins, without waiting for it to end."
    ),
    "Step.stay_silent": "Say nothing in answer to this line. A step needs `clips`, `dtmf` or this.",
    "Step.max_uses": "How many times this step may fire. 0 means no limit.",
    "ToolOutcome.path": (
        "Dotted path into the tool's reply, for example `verified` or `booking.outcome`."
    ),
    "ToolOutcome.success": (
        "Values at that path that mean the tool did what was asked (trimmed, case-insensitive). "
        "`*` accepts any value."
    ),
    "ToolExpectation.name_contains": (
        "A fragment of the tool's name. `name` alone is also accepted and means the whole name."
    ),
    "ToolExpectation.name": "The tool's whole name, instead of a fragment.",
    "ToolExpectation.ok": "Whether the tool must have succeeded. False means it must have refused.",
    "ToolExpectation.called": "False means the tool must NOT have been called.",
    "ToolExpectation.arguments": (
        "Argument values the call must have carried (trimmed, case-insensitive). Empty means "
        "any arguments will do."
    ),
    "ToolExpectation.outputs": (
        'Named values the tool must have reported back, for a lookup: `{"status": "not_found"}`.'
    ),
    "ToolExpectation.at_least_once": (
        'Only read with `ok` false. By default "refused" is about how the matter ended; set '
        "this for a tool that only needs to have refused once on the way."
    ),
    "BookingExpectation.weekdays": "The appointment must fall on one of these days.",
    "BookingExpectation.date_from": "ISO date `YYYY-MM-DD`, inclusive.",
    "BookingExpectation.date_to": "ISO date `YYYY-MM-DD`, inclusive.",
    "BookingExpectation.time_from": "Local start time `HH:MM`, inclusive.",
    "BookingExpectation.time_to": "Local start time `HH:MM`, inclusive.",
    "BookingExpectation.appointment_type": "The appointment type.",
    "BookingExpectation.clinician": "The clinician, where the caller asked for one.",
    "BookingExpectation.status": (
        "For a cancellation, or any state with no slot: the status the system must hold."
    ),
    "BookingExpectation.timezone": "The zone the day and time are stated in (an IANA name).",
    "BookingExpectation.naive_times": (
        "The zone the system writes a start time in when it gives no offset. A database that "
        'stores UTC without saying so is `"UTC"`. Empty means the same as `timezone`.'
    ),
    "BookingExpectation.tool": (
        "The tool that writes the appointment (whole name or fragment). The booked values "
        "are read from that tool's calls only."
    ),
    "BookingExpectation.start_keys": (
        "Names the start time travels under, when the defaults do not fit."
    ),
    "BookingExpectation.type_keys": (
        "Names the appointment type travels under, when the defaults do not fit."
    ),
    "BookingExpectation.clinician_keys": (
        "Names the clinician travels under, when the defaults do not fit."
    ),
    "BookingExpectation.status_keys": (
        "Names the status travels under, when the defaults do not fit."
    ),
    "Expectations.tools": "Tools that must (or must not) have been called. See ToolExpectation.",
    "Expectations.fields": (
        "Persona fields whose stored value (what a tool received) must equal the fact. Each "
        "must be one of the persona's facts."
    ),
    "Expectations.agent_says": (
        "Patterns the agent must have spoken, for outcomes no tool records, such as being "
        "told to call emergency services."
    ),
    "Expectations.agent_never_says": "Patterns the agent must never have spoken.",
    "Expectations.read_back": (
        "Values the system must hold after the call, as fetched by the batch's read-back. "
        "With no read-back these cannot be checked and the call is left ungraded."
    ),
    "Expectations.booking": (
        "What the appointment the call writes must be. Required, with at least one part "
        "stated, when `mutates` is `books` or `moves`. See BookingExpectation."
    ),
}

# Keys the parser reads that are not dataclass fields: (class shown under, key, type, meaning).
EXTRA_KEYS = (
    (
        "Scenario",
        "persona",
        "object",
        "Holds `facts`: `persona.facts` is the persona above. Nothing else in it is read.",
    ),
    (
        "Expectations",
        "booking_from_target",
        "list of text",
        "Names of `expect.booking` parts that belong to the system under test, not to the "
        "caller's intent (a slot time, a clinician). They come from the target profile's "
        "`bookings`. A scenario whose parts the profile does not give is held back, never "
        "run with a weaker check.",
    ),
)

# The classes, in the order the page shows them, with where each sits in a file.
SECTIONS: tuple[tuple[type, str], ...] = (
    (schema.Scenario, "The top level of a scenario file."),
    (schema.Step, "One entry of `steps`, and the shape of `fallback`."),
    (schema.Expectations, "The `expect` object."),
    (schema.ToolExpectation, "One entry of `expect.tools`."),
    (schema.BookingExpectation, "The `expect.booking` object."),
    (schema.ToolOutcome, "One value of `tool_outcomes`, keyed by tool name."),
)

EXAMPLE = {
    "name": "caller gives a date of birth",
    "mutates": "none",
    "persona": {"facts": {"date_of_birth": "2000-01-01"}},
    "steps": [
        {"when": "how can i help", "clips": ["opener"], "intended": "I have a question."},
        {"when": "date of birth", "clips": ["dob"], "field": "date_of_birth"},
    ],
    "hangup_after": "goodbye",
    "max_seconds": 90,
    "expect": {
        "tools": [{"name_contains": "lookup"}],
        "fields": ["date_of_birth"],
        "agent_says": ["thank you"],
    },
}


def _type_name(tp: Any) -> str:
    if tp is str:
        return "text"
    if tp is bool:
        return "true or false"
    if tp is int:
        return "whole number"
    if tp is float:
        return "number"
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if tp is re.Pattern or origin is re.Pattern:
        return "regex"
    if origin is tuple:
        return f"list of {_type_name(args[0])}"
    if origin is dict:
        return "object"
    if origin in (typing.Union, types.UnionType):
        return " or ".join(_type_name(a) for a in args if a is not type(None))
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        return f"object ({tp.__name__})"
    return str(tp)


def _default(f: dataclasses.Field[Any]) -> str:
    if f.default is not dataclasses.MISSING:
        value = f.default
    elif f.default_factory is not dataclasses.MISSING:
        value = f.default_factory()
    else:
        return "required"
    if value is None or dataclasses.is_dataclass(value):
        return "none stated"
    if isinstance(value, tuple | dict) and not value:
        return "empty"
    if isinstance(value, str):
        return f'`"{value}"`' if value else "empty"
    return f"`{json.dumps(value)}`"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def documented_names() -> set[str]:
    """Every ``Class.field`` the page covers, as the schema has them (for the staleness test)."""
    return {f"{cls.__name__}.{f.name}" for cls, _ in SECTIONS for f in dataclasses.fields(cls)}


def _codes(values: tuple[str, ...]) -> str:
    return ", ".join(f"`{v}`" for v in values)


def render() -> str:
    lines = [
        "# Scenario reference",
        "",
        "<!-- Generated by `synthiq-caller reference --out docs/SCENARIO-REFERENCE.md`. "
        "Do not edit by hand: change `scenario.py` or `reference.py` and regenerate. "
        "A test fails when this page is out of date. -->",
        "",
        "A scenario is one JSON file (YAML too, when PyYAML is installed). It says who is "
        "calling, what they say to which question, and what must be true after. Nothing in "
        "it knows about any voice engine. A file with a `chain` key is a chain of calls, "
        "described in `RESET-READ-BACK-CHAINS.md`; a target profile is described in "
        "`TARGETS-REHEARSAL-AND-SAFETY.md`.",
        "",
        "A scenario that states no expectation (no `expect.tools`, `fields`, `agent_says`, "
        "`agent_never_says`, `read_back` or booking part) is ungradeable: a call is never "
        "passed for having happened.",
        "",
        "## A complete small scenario",
        "",
        "```json",
        json.dumps(EXAMPLE, indent=2),
        "```",
        "",
        "## Allowed values",
        "",
        f"- Persona fact names (and `field` on a step): {_codes(schema.KNOWN_FIELDS)}",
        f"- `mutates`, least to most: {_codes(schema.MUTATION_LEVELS)}. `unknown` is the "
        "default and is treated as the most dangerous",
        f"- A scenario at these `mutates` levels must state `expect.booking`: "
        f"{_codes(schema.APPOINTMENT_LEVELS)}",
        f"- `expect.booking.weekdays`: {_codes(schema.WEEKDAYS)}",
        f"- A batch's `--allow-mutating` (and a target profile's `allow_mutating`), least to "
        f"most: {_codes(ALLOWANCES)}",
        "",
    ]
    for cls, where in SECTIONS:
        lines += [f"## {cls.__name__}", "", where, ""]
        lines += ["| Key | Type | Default | Meaning |", "| --- | --- | --- | --- |"]
        hints = typing.get_type_hints(cls)
        for f in dataclasses.fields(cls):
            name = f"{cls.__name__}.{f.name}"
            kind = _type_name(hints[f.name])
            if name in DERIVED:
                lines.append(
                    f"| (never written) `{f.name}` | {kind} | - | {_cell(DERIVED[name])} |"
                )
                continue
            meaning = _cell(MEANING[name])
            if name in ALSO_ACCEPTED:
                meaning += f" Also accepted as `{ALSO_ACCEPTED[name]}`."
            key = JSON_KEY.get(name, f.name)
            lines.append(f"| `{key}` | {kind} | {_default(f)} | {meaning} |")
        for owner, key, kind, meaning in EXTRA_KEYS:
            if owner == cls.__name__:
                lines.append(f"| `{key}` | {kind} | empty | {_cell(meaning)} |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run(args: argparse.Namespace) -> int:
    text = render()
    if args.check:
        path = Path(args.check)
        have = path.read_text(encoding="utf-8").replace("\r\n", "\n") if path.exists() else ""
        if have != text:
            print(
                f"{path} is out of date with the scenario schema. Regenerate it:\n"
                f"  synthiq-caller reference --out {path}"
            )
            return 1
        print(f"{path} matches the scenario schema")
        return 0
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {args.out}")
        return 0
    print(text, end="")
    return 0


def register(sub: Any) -> None:
    """Add the ``reference`` command to the command line's subcommands."""
    parser = sub.add_parser("reference", help="print the scenario reference made from the schema")
    parser.add_argument("--out", default="", help="write the page to this file")
    parser.add_argument("--check", default="", help="exit 1 when this file differs from the page")
    parser.set_defaults(handler=run)
