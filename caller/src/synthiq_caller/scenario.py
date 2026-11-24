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
class ToolExpectation:
    name_contains: str
    ok: bool = True
    # False means the tool must NOT have been called.
    called: bool = True


@dataclass(frozen=True)
class Expectations:
    tools: tuple[ToolExpectation, ...] = ()
    # Persona fields whose stored value (what a tool received) must equal the fact.
    fields: tuple[str, ...] = ()


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


class ScenarioError(ValueError):
    pass


def _pattern(text: str) -> re.Pattern[str]:
    try:
        return re.compile(text, re.IGNORECASE)
    except re.error as error:
        raise ScenarioError(f"bad pattern {text!r}: {error}") from error


def parse_scenario(data: dict[str, Any]) -> Scenario:
    if not isinstance(data.get("name"), str) or not data["name"]:
        raise ScenarioError("a scenario needs a name")
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
    expect = Expectations(
        tools=tuple(
            ToolExpectation(
                name_contains=str(t["name_contains"]),
                ok=bool(t.get("ok", True)),
                called=bool(t.get("called", True)),
            )
            for t in raw_expect.get("tools", ())
        ),
        fields=tuple(str(f) for f in raw_expect.get("fields", ())),
    )
    for name in expect.fields:
        if name not in facts:
            raise ScenarioError(f"expect.fields names {name!r} but the persona has no such fact")

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
    )


def load_scenario(path: str | Path) -> Scenario:
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
    return parse_scenario(data)
