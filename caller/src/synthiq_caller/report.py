"""Summarise a directory of runs: how often it passed, what was misheard, where it failed."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from synthiq_caller.stats import describe


@dataclass
class FieldTally:
    answered: int = 0
    stored_correctly: int = 0
    misheard: int = 0
    mis_stored: int = 0
    not_stored: int = 0


@dataclass
class Failure:
    run: str
    scenario: str
    failing_step: str
    checks: list[str]


@dataclass
class Summary:
    runs: int = 0
    passed: int = 0
    failed: int = 0
    ungraded: int = 0
    by_scenario: dict[str, list[int]] = field(default_factory=dict)  # name -> [passed, graded]
    fields: dict[str, FieldTally] = field(default_factory=dict)
    failures: list[Failure] = field(default_factory=list)
    blocked: list[dict[str, Any]] = field(default_factory=list)


def failing_step(payload: dict[str, Any]) -> str:
    """Where a failed call stopped, in the agent's own terms when the trace has them."""
    trace = payload.get("trace") or {}
    path = trace.get("step_path") or []
    refused = [c for c in trace.get("tool_calls") or [] if not c.get("ok", True)]
    if refused:
        last = refused[-1]
        return f"tool {last.get('name', '?')} refused at step {last.get('step_id') or '?'}"
    agent = [e for e in payload["record"].get("timeline", []) if e.get("kind") == "agent"]
    last_line = str(agent[-1].get("text", ""))[:90] if agent else ""
    if path:
        return f"step {path[-1]}: {last_line!r}"
    return f"last agent line: {last_line!r}" if last_line else payload["record"].get("ended", "")


def summarise(directory: str | Path) -> Summary:
    root = Path(directory)
    summary = Summary()
    for path in sorted(root.glob("run-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        graded = payload["grade"]
        outcome = graded["outcome"]
        summary.runs += 1
        name = graded["scenario"]
        tally = summary.by_scenario.setdefault(name, [0, 0])
        if outcome == "ungraded":
            summary.ungraded += 1
        else:
            tally[1] += 1
            if outcome == "passed":
                summary.passed += 1
                tally[0] += 1
            else:
                summary.failed += 1
                summary.failures.append(
                    Failure(
                        run=path.name,
                        scenario=name,
                        failing_step=failing_step(payload),
                        checks=[c["what"] for c in graded["checks"] if not c["passed"]],
                    )
                )
        for row in graded["fields"]:
            field_tally = summary.fields.setdefault(row["field"], FieldTally())
            field_tally.answered += 1
            if row["stored_matches"]:
                field_tally.stored_correctly += 1
            elif row["stored"] is None:
                field_tally.not_stored += 1
            else:
                field_tally.mis_stored += 1
            if row["heard_matches"] is False:
                field_tally.misheard += 1
    batch = root / "batch.json"
    if batch.exists():
        summary.blocked = list(json.loads(batch.read_text(encoding="utf-8")).get("blocked", []))
    return summary


def render(summary: Summary) -> str:
    graded = summary.passed + summary.failed
    lines = [f"calls: {summary.runs}", f"passed: {describe(summary.passed, graded)}"]
    if summary.ungraded:
        lines.append(f"ungraded (no trace or no expectations): {summary.ungraded}")

    lines.append("\nper scenario")
    for name, (passed, total) in sorted(summary.by_scenario.items()):
        lines.append(f"  {describe(passed, total):<52} {name}")

    if summary.fields:
        lines.append("\nsaid, heard, stored (per field, all calls)")
        lines.append(
            f"  {'field':<14} {'answered':>8} {'stored ok':>10} {'misheard':>9} "
            f"{'mis-stored':>11} {'not stored':>11}"
        )
        for name, t in sorted(summary.fields.items()):
            lines.append(
                f"  {name:<14} {t.answered:>8} {t.stored_correctly:>10} {t.misheard:>9} "
                f"{t.mis_stored:>11} {t.not_stored:>11}"
            )

    if summary.failures:
        lines.append("\nfailures")
        for failure in summary.failures:
            lines.append(f"  {failure.run}  {failure.scenario}")
            lines.append(f"    stopped at: {failure.failing_step}")
            for check in failure.checks:
                lines.append(f"    failed check: {check}")

    if summary.blocked:
        lines.append(f"\nnot run: {len(summary.blocked)}")
        reasons: dict[str, int] = {}
        for item in summary.blocked:
            key = f"{item['scenario_file']}: {item['reason']}"
            reasons[key] = reasons.get(key, 0) + 1
        for key, count in sorted(reasons.items()):
            lines.append(f"  x{count}  {key}")
    return "\n".join(lines)
