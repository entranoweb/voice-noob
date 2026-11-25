"""A batch against a stored baseline: what got worse, and whether it is more than noise.

A baseline is the pass counts of one batch, per scenario and per chain, kept as
a small JSON file. ``compare`` sets a later batch beside it.

Small batches are noisy: 2 of 3 after 3 of 3 is one call's difference. So every
change is given with the Wilson interval of both rates, and a drop is called
**beyond noise** only when the two intervals do not overlap. That test is
cautious: with few repeats it will call most drops "within noise", which is the
honest answer, and the cure is more repeats.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from synthiq_caller.completeness import Completeness, read_object
from synthiq_caller.jsonio import write_json
from synthiq_caller.report import Summary
from synthiq_caller.stats import wilson_interval

# 2: what counts as graded changed (unanswered, early-ended and time-limit
# calls are now failures; void and ungradeable calls are left out). A baseline
# made under the old rule is not comparable and must be made again.
VERSION = 2


@dataclass
class Baseline:
    created: str
    source: str
    # name -> [passed, graded]; ungraded and not-run calls are in neither number.
    scenarios: dict[str, list[int]] = field(default_factory=dict)
    chains: dict[str, list[int]] = field(default_factory=dict)
    version: int = VERSION

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def make_baseline(summary: Summary, source: str = "") -> Baseline:
    return Baseline(
        created=datetime.now(UTC).isoformat(timespec="seconds"),
        source=source,
        scenarios={name: [p, t] for name, (p, t) in summary.by_scenario.items()},
        chains={name: [tally[0], tally[1]] for name, tally in summary.chains.items()},
    )


def save_baseline(baseline: Baseline, path: str | Path) -> None:
    write_json(Path(path), baseline.to_json())


def load_baseline(path: str | Path) -> Baseline:
    data = read_object(Path(path))
    if data.get("version") != VERSION:
        raise ValueError(
            f"baseline version {data.get('version')!r} is not {VERSION}: it was made under an "
            "older grading rule and cannot be compared; make the baseline again"
        )
    return Baseline(
        created=str(data.get("created", "")),
        source=str(data.get("source", "")),
        scenarios={str(k): [int(v[0]), int(v[1])] for k, v in data.get("scenarios", {}).items()},
        chains={str(k): [int(v[0]), int(v[1])] for k, v in data.get("chains", {}).items()},
    )


@dataclass
class Change:
    kind: str  # "scenario" or "chain"
    name: str
    before: tuple[int, int] | None
    after: tuple[int, int] | None
    # "regressed", "regressed (within noise)", "regressed (no graded calls now)",
    # "improved", "improved (within noise)", "same", "new", "missing" or
    # "no graded calls"
    verdict: str

    @property
    def regressed(self) -> bool:
        return self.verdict.startswith("regressed")

    @property
    def beyond_noise(self) -> bool:
        return self.verdict == "regressed"


def _judge(before: tuple[int, int] | None, after: tuple[int, int] | None) -> str:
    if before is None:
        return "new"
    if after is None:
        return "missing"
    if before[1] > 0 and after[1] == 0:
        # It was being graded and now nothing of it can be. Whatever the cause
        # (the scenario became ungradeable, every call went void), the batch
        # shows less than the baseline did. That is a regression to look at,
        # not a blank row.
        return "regressed (no graded calls now)"
    if before[1] == 0 or after[1] == 0:
        return "no graded calls"
    old, new = before[0] / before[1], after[0] / after[1]
    if new == old:
        return "same"
    old_low, old_high = wilson_interval(*before)
    new_low, new_high = wilson_interval(*after)
    if new < old:
        return "regressed" if new_high < old_low else "regressed (within noise)"
    return "improved" if new_low > old_high else "improved (within noise)"


def _pairs(kind: str, before: dict[str, list[int]], after: dict[str, list[int]]) -> list[Change]:
    changes = []
    for name in sorted(set(before) | set(after)):
        was = (before[name][0], before[name][1]) if name in before else None
        now = (after[name][0], after[name][1]) if name in after else None
        changes.append(Change(kind, name, was, now, _judge(was, now)))
    return changes


def compare(baseline: Baseline, summary: Summary) -> list[Change]:
    chains = {name: [tally[0], tally[1]] for name, tally in summary.chains.items()}
    return _pairs("scenario", baseline.scenarios, summary.by_scenario) + _pairs(
        "chain", baseline.chains, chains
    )


def _rate(counts: tuple[int, int] | None) -> str:
    if counts is None:
        return "-"
    passed, total = counts
    if total == 0:
        return "0 graded"
    low, high = wilson_interval(passed, total)
    return f"{passed}/{total} {passed / total:.0%} ({low:.0%}-{high:.0%})"


def _total(changes: list[Change], side: str) -> tuple[int, int]:
    both = [c for c in changes if c.kind == "scenario" and c.before and c.after]
    counts = [getattr(c, side) for c in both]
    return (sum(c[0] for c in counts), sum(c[1] for c in counts))


def render_comparison(baseline: Baseline, changes: list[Change]) -> str:
    lines = [f"baseline: {baseline.created}  {baseline.source}".rstrip()]
    was, now = _total(changes, "before"), _total(changes, "after")
    lines.append(
        f"scenarios in both: before {_rate(was)}, now {_rate(now)}  "
        f"[{_judge(was, now)}]  (95% intervals)"
    )
    regressions = [c for c in changes if c.regressed]
    lines.append(f"\nregressions: {len(regressions)}")
    for change in regressions:
        lines.append(f"  {change.kind:<9} {change.name}")
        lines.append(
            f"    before {_rate(change.before)}   now {_rate(change.after)}   "
            + (
                "BEYOND NOISE"
                if change.beyond_noise
                else "NOTHING GRADED NOW: find out why before trusting the rest"
                if change.after is not None and change.after[1] == 0
                else "within noise: repeat to be sure"
            )
        )
    lines.append("\nall")
    lines.append(f"  {'before':<24} {'now':<24} {'verdict':<26} name")
    for change in changes:
        label = change.name if change.kind == "scenario" else f"[chain] {change.name}"
        lines.append(
            f"  {_rate(change.before):<24} {_rate(change.after):<24} {change.verdict:<26} {label}"
        )
    return "\n".join(lines)


def comparison_failures(
    changes: list[Change], fail_on: str, completeness: Completeness | None = None
) -> list[str]:
    """Why a comparison should fail a pipeline, one line each; empty when it should not.

    ``never`` never fails. ``beyond-noise`` fails on a drop beyond noise. ``any`` fails on any
    drop, and on a baseline scenario that is gone from the batch. Both fail when the
    directory is not what was planned (a call that never ran, a file removed or edited), and
    when nothing of the baseline was found at all: an empty or wrong directory has no
    regression in it because it has nothing in it.
    """
    if fail_on == "never":
        return []
    reasons: list[str] = []
    if completeness is not None and completeness.bad:
        reasons.append(f"the batch is {completeness.status.upper()}, so it is not comparable")
    scenarios = [c for c in changes if c.kind == "scenario" and c.before is not None]
    if scenarios and all(c.after is None for c in scenarios):
        reasons.append(
            f"none of the baseline's {len(scenarios)} scenario(s) is in this batch: nothing "
            "was compared"
        )
    if fail_on == "any":
        reasons += [f"{c.kind} {c.name}: {c.verdict}" for c in changes if c.regressed]
        reasons += [
            f"{c.kind} {c.name}: missing from this batch"
            for c in changes
            if c.verdict == "missing" and not reasons_cover(reasons, c)
        ]
    else:
        reasons += [f"{c.kind} {c.name}: {c.verdict}" for c in changes if c.beyond_noise]
    return reasons


def reasons_cover(reasons: list[str], change: Change) -> bool:
    """Whether ``reasons`` already names this change (no double line for one scenario)."""
    return any(r.startswith(f"{change.kind} {change.name}:") for r in reasons)
