"""Turn one recorded call into a scenario skeleton.

This is the first half of the loop that makes a failed call a permanent test:
cut the call into clips, then lay those clips out as steps keyed on the
question each one answered. The result is a starting point; a person still sets
the persona facts, the expectations and the ``mutates`` level.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from synthiq_caller.clips import load_overrides
from synthiq_caller.manifestcsv import read_rows

# The agent line each kind of answer responds to. Specific confirmations come
# first so a read-back that mentions a date of birth is not taken for the
# question that asks for one.
WHEN: dict[str, str] = {
    "confirm_fee": r"fee is|self-pay fee|pounds",
    "confirm_details": r"i have the name|that's the name",
    "confirm_practitioner": r"that would be with",
    "confirm_choice": r"one you'd like to (?:move|cancel)",
    "confirm_booking": r"read that back|that's a consultation|that's an mri|moving you|go ahead",
    "correction": r"i have the name|that's the name|once more",
    "opener": r"how can i help",
    "name": r"full name|your name|registered under",
    "been_before": r"been to see us before|visited us before|been to the clinic|been here before",
    "insurer": r"which insurer",
    "insurance": r"health insurance|paying yourself|paying privately",
    "referral_sent": r"already been sent|how to submit|online form",
    "referral": r"do you have a referral|referral letter",
    "body_part": r"which part of the body",
    "mobility": r"mobility|getting around",
    "phone": r"mobile number|phone number",
    "postcode": r"post ?code",
    "dob": r"date of birth",
    "reason": r"bringing you in|knee or shoulder",
    "slot": r"which suits you|which one would suit|which of those",
    "when": r"when would suit|particular day|earliest available|soonest we have",
    "anything_else": r"anything else",
}
FIELD = {"name": "name", "dob": "date_of_birth", "postcode": "postcode", "phone": "phone"}
_ORDER = list(WHEN)


def read_manifest(bank: Path) -> list[dict[str, str]]:
    return read_rows(bank / "manifest.csv")[1]


def scenario_from_call(
    bank: Path, call: str, *, name: str = "", mutates: str = "unknown"
) -> dict[str, Any]:
    rows = [r for r in read_manifest(bank) if r["call"] == call]
    if not rows:
        raise ValueError(f"no clips for call {call!r} in {bank}")
    steps: dict[str, dict[str, Any]] = {}
    facts: dict[str, str] = {}
    skipped: list[str] = []
    overrides = load_overrides(bank)
    for row in rows:
        category = row["category"]
        override = overrides.get(row["clip"])
        if override is not None and override.exclude:
            skipped.append(f"{row['clip']} (excluded: {override.reason}, {override.date})")
            continue
        if override is not None and override.label:
            row = {**row, "said": override.label, "intended": override.label}
        flags = row.get("review", "").split()
        if category not in WHEN or "overlaps_agent" in flags:
            skipped.append(f"{row['clip']} ({category}; {' '.join(flags) or 'no step for it'})")
            continue
        if category in steps:
            # The caller answered this kind of question more than once; keep the first
            # and leave the rest for a person to place.
            skipped.append(f"{row['clip']} (second answer to a {category} question)")
            continue
        step: dict[str, Any] = {
            "when": WHEN[category],
            "clips": [row["clip"]],
            "intended": row.get("intended") or row.get("said", ""),
            "max_uses": 2,
        }
        if category in FIELD:
            step["field"] = FIELD[category]
            if row.get("value"):
                facts[FIELD[category]] = row["value"]
        steps[category] = step
    ordered = [steps[c] for c in _ORDER if c in steps]
    return {
        "name": name or f"replay of {call}",
        "description": f"Generated from recorded call {call}. Review before use.",
        "mutates": mutates,
        "persona": {"facts": facts},
        "max_seconds": 360,
        "hangup_after": "have a lovely day|take care, bye|goodbye",
        "steps": ordered,
        "expect": {"tools": [], "fields": []},
        "notes": {"clips_not_placed": skipped},
    }
