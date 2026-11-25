"""Reading an uploaded caller bundle into rows, and rows into a summary.

The caller rig (``caller/``) writes a directory of JSON files per batch;
``synthiq-caller upload`` bundles them into one document and posts it. This
module is the only place that knows what is inside that document.

It reads defensively on purpose. The caller's result format is changing while
this is written (fault classes, counts "of attempted", marks for unverified
labels), so:

* a field this code does not know is kept, never rejected: the whole payload of
  every call is stored as it arrived;
* a field this code expects but does not find gets an honest empty value, not a
  guess;
* an outcome this code has not seen is kept as written and counted as "proves
  nothing", never as a pass and never as a failure.

Nothing here touches the database or the network.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

BUNDLE_FORMAT = "synthiq-caller-bundle"
# The newest bundle version this code was written against. A higher number is
# accepted: its unknown parts ride along in ``raw``.
KNOWN_BUNDLE_VERSION = 1

MAX_BUNDLE_BYTES = 25 * 1024 * 1024
MAX_CALLS = 2000
MAX_NOT_RUN = 5000

PASSED = "passed"
FAILED = "failed"
GRADED_OUTCOMES = (PASSED, FAILED)
UNCLASSIFIED = "unclassified"

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


class BundleError(ValueError):
    """The upload is not a caller bundle this server can store."""


@dataclass
class ParsedCall:
    position: int
    file: str
    scenario: str
    scenario_file: str
    mutates: str
    repeat: int | None
    outcome: str
    fault_class: str | None
    last_step: str
    conversation_id: str | None
    ended: str
    duration_s: float | None
    fields: list[dict[str, Any]]
    checks: list[dict[str, Any]]
    timeline: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    exclusion_reason: str
    synthetic: bool
    labels_unverified: bool
    raw: dict[str, Any]


@dataclass
class ParsedBundle:
    run_id: str
    content_hash: str
    payload_version: int
    rig_version: str
    target_label: str
    scenario_set_hash: str
    started_at: datetime | None
    ended_at: datetime | None
    calls: list[ParsedCall]
    not_run: list[dict[str, Any]]
    chains: list[dict[str, Any]]
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> int:
        return sum(call.outcome == PASSED for call in self.calls)

    @property
    def failed(self) -> int:
        return sum(call.outcome == FAILED for call in self.calls)


def _text(value: Any, limit: int) -> str:
    """A string no longer than its column, from whatever arrived."""
    if value is None:
        return ""
    return (value if isinstance(value, str) else str(value))[:limit]


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dict_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _whole(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _last_step(payload: dict[str, Any]) -> str:
    """Where a call stopped, when the uploader did not say.

    The caller's own ``report.failing_step`` is the better reading and the upload
    command sends it. This is the fallback for a bundle built some other way.
    """
    trace = _dict(payload.get("trace"))
    refused = [c for c in _dict_list(trace.get("tool_calls")) if c.get("ok") is False]
    if refused:
        last = refused[-1]
        return f"tool {last.get('name', '?')} refused at step {last.get('step_id') or '?'}"
    record = _dict(payload.get("record"))
    agent_lines = [e for e in _dict_list(record.get("timeline")) if e.get("kind") == "agent"]
    last_line = _text(agent_lines[-1].get("text"), 90) if agent_lines else ""
    path = trace.get("step_path")
    if isinstance(path, list) and path:
        return f"step {path[-1]}: {last_line!r}"
    return f"last agent line: {last_line!r}" if last_line else _text(record.get("ended"), 200)


def _exclusion_reason(outcome: str, payload: dict[str, Any], grade: dict[str, Any]) -> str:
    """Why a call that is neither passed nor failed is out of the pass rate."""
    for source in (grade, payload):
        for key in ("exclusion_reason", "reason", "ungraded_reason"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
    if isinstance(payload.get("trace_error"), str) and payload["trace_error"].strip():
        return f"the agent's trace could not be read: {payload['trace_error'].strip()[:400]}"
    if outcome == "ungraded":
        if payload.get("trace") is None:
            return "no trace was read for this call, so there was nothing to grade it from"
        return "the scenario states no expectation this call could be graded against"
    return f"outcome recorded as {outcome!r}"


def _call(position: int, entry: dict[str, Any]) -> ParsedCall:
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        raise BundleError(f"runs[{position - 1}] has no payload object")
    grade = _dict(payload.get("grade"))
    record = _dict(payload.get("record"))
    trace = _dict(payload.get("trace"))

    outcome = _text(grade.get("outcome") or payload.get("outcome") or "ungraded", 60).strip()
    outcome = outcome or "ungraded"
    # A person's ruling sent with the upload wins over anything the rig recorded.
    fault = (
        entry.get("fault")
        or grade.get("fault_class")
        or payload.get("fault_class")
        or payload.get("fault")
        or grade.get("fault")
    )
    last_step = entry.get("last_step")
    return ParsedCall(
        position=position,
        file=_text(entry.get("file"), 200),
        scenario=_text(
            grade.get("scenario") or record.get("scenario") or payload.get("scenario_file"), 300
        ),
        scenario_file=_text(payload.get("scenario_file"), 300),
        mutates=_text(payload.get("mutates"), 40),
        repeat=_whole(payload.get("repeat")),
        outcome=outcome,
        fault_class=_text(fault, 60).strip() or None,
        last_step=(last_step if isinstance(last_step, str) and last_step else _last_step(payload))[
            :2000
        ],
        conversation_id=_text(record.get("call_ref") or grade.get("call_ref"), 200) or None,
        ended=_text(record.get("ended"), 200),
        duration_s=_number(record.get("duration_s")),
        fields=_dict_list(grade.get("fields")),
        checks=_dict_list(grade.get("checks")),
        timeline=_dict_list(record.get("timeline")),
        tool_calls=_dict_list(trace.get("tool_calls")),
        synthetic=bool(payload.get("synthetic_clips"))
        or any(row.get("synthetic") for row in _dict_list(grade.get("fields"))),
        labels_unverified=bool(payload.get("labels_unverified"))
        or any(row.get("labels_unverified") for row in _dict_list(grade.get("fields"))),
        exclusion_reason=(
            "" if outcome in GRADED_OUTCOMES else _exclusion_reason(outcome, payload, grade)
        ),
        raw=payload,
    )


def _not_json(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def parse_bundle(body: bytes) -> ParsedBundle:
    """Read an uploaded bundle. Raises ``BundleError`` with a message fit to show."""
    if len(body) > MAX_BUNDLE_BYTES:
        raise BundleError(f"the bundle is larger than {MAX_BUNDLE_BYTES // (1024 * 1024)} MB")
    try:
        # NaN and Infinity are not JSON, and the database's JSON type refuses them.
        data = json.loads(body, parse_constant=_not_json)
    except (ValueError, UnicodeDecodeError) as error:
        raise BundleError("the upload is not valid JSON") from error
    if not isinstance(data, dict) or data.get("format") != BUNDLE_FORMAT:
        raise BundleError(
            f'not a caller bundle: expected an object with "format": "{BUNDLE_FORMAT}" '
            "(build one with `synthiq-caller upload`)"
        )
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise BundleError('the bundle has no whole-number "version"')
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
        raise BundleError(
            'the bundle needs a "run_id" of 1 to 64 letters, digits, dots, colons, '
            "dashes or underscores"
        )
    runs = data.get("runs")
    if not isinstance(runs, list):
        raise BundleError('the bundle has no "runs" list')
    if len(runs) > MAX_CALLS:
        raise BundleError(
            f"the bundle holds {len(runs)} calls; the most one upload may hold is {MAX_CALLS}"
        )
    calls = []
    for index, entry in enumerate(runs, start=1):
        if not isinstance(entry, dict):
            raise BundleError(f"runs[{index - 1}] is not an object")
        calls.append(_call(index, entry))

    batch = _dict(data.get("batch"))
    not_run = _dict_list(batch.get("blocked"))
    if len(not_run) > MAX_NOT_RUN:
        raise BundleError(
            f"the bundle lists {len(not_run)} calls as not run; the most is {MAX_NOT_RUN}"
        )

    return ParsedBundle(
        run_id=run_id,
        content_hash=hashlib.sha256(body).hexdigest(),
        payload_version=version,
        rig_version=_text(data.get("rig_version"), 64),
        target_label=_text(data.get("target_label"), 200),
        scenario_set_hash=_text(data.get("scenario_set_hash"), 64),
        started_at=_when(data.get("started_at")),
        ended_at=_when(data.get("ended_at")),
        calls=calls,
        not_run=not_run,
        chains=_dict_list(data.get("chains")),
        # Everything except the per-call payloads, which are stored on the calls.
        raw={key: value for key, value in data.items() if key != "runs"},
    )


def wilson_interval(passes: int, total: int, z: float = 1.96) -> tuple[float, float] | None:
    """The Wilson score interval for a pass rate, or None when there is nothing to rate.

    The same formula as the caller's ``stats.wilson_interval``. None, not (0, 1),
    for an empty total: a page must be able to tell "no graded calls" from "an
    interval that happens to span everything".
    """
    if total <= 0 or not 0 <= passes <= total:
        return None
    rate = passes / total
    denominator = 1 + z * z / total
    centre = (rate + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(rate * (1 - rate) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def rate(passes: int, total: int) -> dict[str, Any]:
    """A pass count with its rate and interval; rate and interval are null for no calls."""
    interval = wilson_interval(passes, total)
    return {
        "passed": passes,
        "total": total,
        "rate": passes / total if total > 0 else None,
        "interval_low": interval[0] if interval else None,
        "interval_high": interval[1] if interval else None,
    }


UNAVAILABLE = "unavailable"
UNAVAILABLE_KEY = "_unavailable"
REHEARSAL_PREFIX = "REHEARSAL"


def _has_unavailable(call: Any) -> bool:
    """Some tool's result was held back by the platform (the caller's ``unavailable`` mark)."""
    return any(
        item.get("result") == UNAVAILABLE or UNAVAILABLE_KEY in _dict(item.get("arguments"))
        for item in call.tool_calls
    )


def outcome_counts(calls: list[Any], *, rehearsal: bool = False) -> dict[str, int]:
    """Calls by outcome, plus the provenance counts a reader needs beside a rate.

    ``rehearsal_calls``: calls the caller marked as placed against a scripted
    stand-in. ``rehearsal_run`` is 1 when the bundle as a whole says so. Both
    live in this JSON column so no schema change is needed to show the mark.
    """
    counts: dict[str, int] = {}
    for call in calls:
        counts[call.outcome] = counts.get(call.outcome, 0) + 1
    counts["synthetic_calls"] = sum(bool(call.synthetic) for call in calls)
    counts["labels_unverified_calls"] = sum(bool(call.labels_unverified) for call in calls)
    counts["unavailable_calls"] = sum(_has_unavailable(call) for call in calls)
    counts["rehearsal_calls"] = sum(call.raw.get("rehearsal") is True for call in calls)
    counts["rehearsal_run"] = int(rehearsal)
    return counts


def is_rehearsal(counts: dict[str, int] | None, target_label: str) -> bool:
    """Whether a stored run was a rehearsal, from what is already stored (older rows too)."""
    counts = counts or {}
    return (
        bool(counts.get("rehearsal_calls"))
        or bool(counts.get("rehearsal_run"))
        or target_label.strip().upper().startswith(REHEARSAL_PREFIX)
    )


def scenario_table(calls: list[Any]) -> list[dict[str, Any]]:
    """Per scenario: how many calls, how many graded, how many passed."""
    rows: dict[str, dict[str, int]] = {}
    for call in calls:
        row = rows.setdefault(call.scenario, {"attempted": 0, "passed": 0, "failed": 0})
        row["attempted"] += 1
        if call.outcome == PASSED:
            row["passed"] += 1
        elif call.outcome == FAILED:
            row["failed"] += 1
    table = []
    for name, row in sorted(rows.items()):
        graded = row["passed"] + row["failed"]
        table.append(
            {
                "scenario": name,
                "attempted": row["attempted"],
                "graded": graded,
                "failed": row["failed"],
                "not_graded": row["attempted"] - graded,
                **rate(row["passed"], graded),
            }
        )
    return table


def failures_by_fault_class(calls: list[Any]) -> list[dict[str, Any]]:
    """Failed calls grouped by fault class; calls with none go under "unclassified"."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for call in calls:
        if call.outcome != FAILED:
            continue
        failed_checks = [
            str(check.get("what", ""))
            for check in (call.checks or [])
            if check.get("passed") is False
        ]
        groups.setdefault(call.fault_class or UNCLASSIFIED, []).append(
            {
                "id": str(call.id),
                "position": call.position,
                "scenario": call.scenario,
                "last_step": call.last_step,
                "failed_checks": failed_checks,
            }
        )
    # The largest group first; "unclassified" last among equals so a named fault leads.
    ordered = sorted(
        groups.items(), key=lambda item: (-len(item[1]), item[0] == UNCLASSIFIED, item[0])
    )
    return [{"fault_class": name, "calls": rows} for name, rows in ordered]


def field_tallies(calls: list[Any]) -> list[dict[str, Any]]:
    """Said, heard and stored per field across the run."""
    tallies: dict[str, dict[str, int]] = {}
    for call in calls:
        for row in call.fields or []:
            name = str(row.get("field", "")) or "?"
            tally = tallies.setdefault(
                name,
                {
                    "answered": 0,
                    "stored_correctly": 0,
                    "sent_correctly": 0,
                    "misheard": 0,
                    "mis_stored": 0,
                    "not_stored": 0,
                    "read_back": 0,
                    "labels_unverified": 0,
                    "synthetic": 0,
                },
            )
            tally["answered"] += 1
            if row.get("labels_unverified"):
                tally["labels_unverified"] += 1
            if row.get("synthetic"):
                tally["synthetic"] += 1
            if row.get("stored_matches") is True and row.get("stored_source") == "tool call":
                # The agent's tool was sent the right value and nothing confirmed the
                # system kept it: sent, not confirmed stored. Not a stored-correctly.
                tally["sent_correctly"] += 1
            elif row.get("stored_matches") is True:
                tally["stored_correctly"] += 1
            elif row.get("stored") is None:
                tally["not_stored"] += 1
            else:
                tally["mis_stored"] += 1
            if row.get("heard_matches") is False:
                tally["misheard"] += 1
            if row.get("stored_source") == "read back":
                tally["read_back"] += 1
    return [{"field": name, **tally} for name, tally in sorted(tallies.items())]
