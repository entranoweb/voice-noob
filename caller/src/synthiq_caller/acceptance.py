"""A dated statement of what was tested, what passed, and what that does and does not show.

    synthiq-caller acceptance runs/monday runs/tuesday --claim 0.95 --out report.html

One self-contained page for the person who has to decide whether to accept an
agent. It adds no arithmetic of its own: the counts are ``report.summarise``,
the verdict is ``report.gate_reading`` (the rule behind the ``gate`` command),
the failure groups and disagreements are the scorecard's, and what produced the
runs is read from each directory's manifest (``manifest.py``).

**Three verdicts, never "passed".**

* *The claim is met*: the runs demonstrate the claimed rate at the stated
  confidence, by scenario, and nothing refuses the gate.
* *Not enough calls to support the claim*: what was observed is at or above the
  claimed rate, and there are too few independent results to demonstrate it.
* *The claim is not met*: the observed rate is below the claim, or the gate is
  refused (too much of what was attempted was never judged).

**Refused outright**, with no page written: no runs; a rehearsal against a
scripted stand-in; runs made against different versions of the same scenario
file (unless told to go on); runs with no manifest (unless told to go on).

**Numbers.** Every percentage has its counts beside it. A percentage is never
shown on the other side of the claim from the true value: 94.96% against a
claim of 95% is printed as 94.96%, not 95.0%.

The page depends only on the run directories and the options given, apart from
the line saying when it was generated.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from synthiq_caller import manifest, scorecard
from synthiq_caller.jsonio import write_json
from synthiq_caller.report import gate_reading, summarise
from synthiq_caller.scorecard import _STYLE, _e, _span, _table
from synthiq_caller.stats import calls_needed, wilson_interval

MET = "The claim is met"
NOT_ENOUGH = "Not enough calls to support the claim"
NOT_MET = "The claim is not met"
NOT_RECORDED = "not recorded"


class Refused(ValueError):
    """These runs cannot be made into an acceptance report as they stand."""


def pct(value: float, claim: float | None = None) -> str:
    """A rate as a percentage that never lands on the wrong side of ``claim``."""
    for places in (1, 2, 3, 4, 6):
        shown = round(value * 100, places)
        if claim is None:
            break
        edge = round(claim * 100, 9)  # 0.57 * 100 is 56.99999999999999
        if (value < claim) == (shown < edge) and (value > claim) == (shown > edge):
            break
    return f"{shown:.{places}f}%"


def _rate(passed: int, total: int, claim: float) -> dict[str, Any]:
    low, high = wilson_interval(passed, total)
    return {
        "passed": passed,
        "total": total,
        "rate": passed / total if total else None,
        "interval": [low, high] if total else None,
        "text": (
            f"{passed} of {total} ({pct(passed / total, claim)}; 95% interval "
            f"{pct(low)} to {pct(high)})"
            if total
            else "none"
        ),
    }


def _distinct(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for item in items:
        seen.setdefault(json.dumps(item, sort_keys=True), item)
    return [seen[key] for key in sorted(seen)]


def _provenance(
    roots: list[Path], allow_no_manifest: bool, mixed: dict[str, list[str]]
) -> dict[str, Any]:
    """What produced the runs, gathered from each directory's manifest."""
    batches: list[dict[str, Any]] = []
    unrecorded: list[str] = []
    for root in roots:
        found = manifest.load(root)
        if found is None:
            unrecorded.append(root.name)
            continue
        batches += [e for e in found["entries"] if e.get("kind") == "batch"]
    if unrecorded and not allow_no_manifest:
        raise Refused(
            f"no run manifest in {unrecorded}: these runs cannot be tied to the rig, scenarios "
            "and clips that produced them. Run them again with this version of the rig, or "
            "pass --allow-no-manifest to go on and have the report say so."
        )
    files: dict[str, dict[str, int]] = {}
    for root in roots:
        for file, tally in manifest.run_hashes(root).items():
            into = files.setdefault(file, {})
            for value, count in tally.items():
                into[value] = into.get(value, 0) + count
    single = {file: next(iter(tally)) for file, tally in files.items() if len(tally) == 1}
    whole = len(single) == len(files) and manifest.UNRECORDED not in single.values()
    started = sorted(str(b["started"]) for b in batches if b.get("started"))
    ended = sorted(str(b["ended"]) for b in batches if b.get("ended"))
    return {
        "unrecorded_directories": unrecorded,
        "rigs": _distinct([b["rig"] for b in batches]),
        "targets": _distinct([b["target"] for b in batches]),
        "clip_banks": _distinct([bank for b in batches for bank in b["clips"]["banks"]]),
        "played_sha256": sorted({str(b["clips"]["played_sha256"]) for b in batches}),
        "clips_unavailable": sorted({c for b in batches for c in b["clips"]["unavailable"]}),
        "scenario_files": [
            {"file": file, "hashes": dict(sorted(tally.items()))}
            for file, tally in sorted(files.items())
        ],
        # One hash for the scenarios the stored calls answer to; none when they
        # answer to more than one version, or to none that was recorded.
        "scenario_set_sha256": manifest.set_hash(single) if whole and files else "",
        "mixed": mixed,
        "started": started[0] if started else "",
        "ended": ended[-1] if ended and len(ended) == len(batches) else "",
        "batches": len(batches),
    }


def _clips(roots: list[Path]) -> dict[str, Any]:
    """Which calls played a clip nobody confirmed, and which played a synthetic voice."""
    unverified: dict[str, str] = {}
    synthetic: set[str] = set()
    unverified_calls = synthetic_calls = calls = 0
    for root in roots:
        for path in sorted(root.glob("run-*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            calls += 1
            doubts = [str(d) for d in payload.get("labels_unverified") or []]
            for doubt in doubts:
                clip, _, why = doubt.partition(": ")
                unverified[clip] = why
            unverified_calls += bool(doubts)
            made = [str(c) for c in payload.get("synthetic_clips") or []]
            synthetic.update(made)
            synthetic_calls += bool(made)
    return {
        "calls": calls,
        "unverified": [{"clip": c, "why": w} for c, w in sorted(unverified.items())],
        "unverified_calls": unverified_calls,
        "synthetic": sorted(synthetic),
        "synthetic_calls": synthetic_calls,
    }


def _not_shown(report: dict[str, Any]) -> list[str]:
    """In plain words, what these runs are not evidence of."""
    made, clips, counts = report["made_with"], report["clips"], report["counts"]
    limits: list[str] = []
    legs = sorted({str(t.get("leg", "")) for t in made["targets"]})
    if not legs or legs == [""]:
        limits.append(
            "How the calls reached the agent was not recorded, so nothing here can be taken "
            "as a test over a phone line."
        )
    elif not any("phone" in leg for leg in legs):
        limits.append(
            f"The calls were placed over a direct connection to the agent (leg: "
            f"{', '.join(legs)}), not a phone line. Line quality, the phone codec and "
            "anything the carrier does are not tested."
        )
    elif len(legs) > 1:
        limits.append(
            f"The calls were placed in more than one way (legs: {', '.join(legs)}); only "
            "those on a phone leg test the phone line."
        )
    else:
        limits.append(
            "The calls were placed over a phone line (leg: phone), from one calling number "
            "on one carrier. Other carriers, mobile networks, poor lines and callers' own "
            "handsets are not tested."
        )
    if counts["graded"] > report["gate"]["scenarios"]:
        limits.append(
            "Repeats of a scenario replay the same recorded audio. They show whether the "
            "agent is consistent, and they are not independent callers; the verdict counts "
            "each scenario once for that reason."
        )
    stated = [t for t in made["targets"] if t.get("version")]
    if not made["targets"] or len(stated) < len(made["targets"]):
        limits.append(
            "The version of the agent under test was not recorded for every batch. A result "
            "holds only for the agent as it was when the calls were placed."
        )
    else:
        limits.append(
            "The agent's version is as stated by whoever ran the batch. The rig did not ask "
            "the platform for it."
        )
    fields = report["fields"]
    stored = sum(f["answered"] - f["not_stored"] for f in fields)
    fetched = sum(f["read_back"] for f in fields)
    if stored > fetched:
        limits.append(
            f"In {stored - fetched} of {stored} stored values, 'stored' is what the agent's "
            "tool was sent, not what the system was afterwards found to hold. Those show "
            "what the agent tried to save."
        )
    if clips["synthetic"]:
        limits.append(
            f"{clips['synthetic_calls']} call(s) played a synthetic voice where no recording "
            "of a real caller exists. Those answers say nothing about how the agent hears "
            "real people."
        )
    if clips["unverified"]:
        limits.append(
            f"{clips['unverified_calls']} call(s) played a clip whose label no person has "
            "confirmed. There, what the caller 'said' is a recogniser's reading, and a match "
            "or a mismatch may be the label's doing."
        )
    if made["unrecorded_directories"]:
        limits.append(
            f"No manifest was kept for {', '.join(made['unrecorded_directories'])}: those "
            "calls cannot be tied to a rig version, a scenario version or a clip bank."
        )
    if made["mixed"]:
        limits.append(
            "Some scenario files were changed between runs (listed above). Calls made "
            "against different versions of a scenario are not repeats of one test."
        )
    if any(rig.get("dirty") for rig in made["rigs"]) or any(
        rig.get("commit") == "unknown" for rig in made["rigs"]
    ):
        limits.append(
            "The rig ran with changes that no commit holds, or from an unknown commit, so "
            "the grading code cannot be reproduced exactly from the commit shown."
        )
    limits.append(
        "Only the scenarios listed were tested. Nothing here speaks for callers, accents, "
        "requests or conditions outside them."
    )
    return limits


def build(
    directories: list[Path] | list[str],
    *,
    claim: float = 0.95,
    confidence: float = 0.95,
    max_unjudged: float = 0.1,
    allow_mixed: bool = False,
    allow_no_manifest: bool = False,
    title: str = "",
    generated: str = "",
) -> dict[str, Any]:
    """Everything the page says, as data. Raises ``Refused`` when no report can be made."""
    roots = [Path(d) for d in directories]
    summary = scorecard.merged([summarise(root) for root in roots])
    if not summary.runs and not summary.blocked:
        raise Refused("no runs found in " + ", ".join(root.name for root in roots))
    rehearsed = summary.rehearsals or any(
        entry.get("rehearsal")
        for root in roots
        for entry in (manifest.load(root) or {"entries": []})["entries"]
    )
    if rehearsed:
        raise Refused(
            "these runs include a rehearsal against a scripted stand-in, not an agent. A "
            "rehearsal shows that the rig runs and nothing about any agent, so no acceptance "
            "report is made from one."
        )
    try:
        mixed = manifest.refuse_mixed(roots, allow=allow_mixed)
    except manifest.MixedScenarios as error:
        raise Refused(str(error)) from error

    card = scorecard.build(roots)
    read = gate_reading(summary, claim, confidence, max_unjudged)
    graded = summary.passed + summary.failed
    failures = read.scenarios - read.every
    needed = calls_needed(claim, confidence, failures)
    if read.refused:
        verdict = NOT_MET
        why = "; ".join(read.refused)
    elif read.met:
        verdict = MET
        why = (
            f"{read.every} of {read.scenarios} scenarios passed every call placed, and "
            f"{needed} are needed with {failures} failing"
        )
    elif read.scenarios and read.every / read.scenarios >= claim:
        verdict = NOT_ENOUGH
        why = (
            f"{read.every} of {read.scenarios} scenarios passed every call placed. With "
            f"{failures} failing, {needed} scenarios are needed to demonstrate {pct(claim)}; "
            f"{read.scenarios} were tested"
        )
    elif not read.scenarios:
        verdict = NOT_ENOUGH
        why = "no call was placed that could be judged"
    else:
        verdict = NOT_MET
        why = (
            f"{read.every} of {read.scenarios} scenarios passed every call placed "
            f"({pct(read.every / read.scenarios, claim)}), which is below the claimed "
            f"{pct(claim)}"
        )

    report: dict[str, Any] = {
        "title": title or "Acceptance report",
        "generated": generated or datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "sources": [root.name for root in roots],
        "claim": claim,
        "confidence": confidence,
        "verdict": verdict,
        "verdict_reason": why,
        "made_with": _provenance(roots, allow_no_manifest, mixed),
        "clips": _clips(roots),
        "counts": {
            "attempted": read.attempted,
            "placed": read.placed,
            "not_run": read.not_run,
            "graded": graded,
            "passed": summary.passed,
            "failed": summary.failed,
            "failed_by_fault": dict(sorted(summary.faults.items())),
            "void": summary.void,
            "ungraded": summary.ungraded,
            "ungradeable": summary.ungradeable,
        },
        "rates": {
            "of_graded": _rate(summary.passed, graded, claim),
            "of_placed": _rate(summary.passed, read.placed, claim),
            "of_attempted": _rate(summary.passed, read.attempted, claim),
            "by_scenario": _rate(read.every, read.scenarios, claim),
        },
        "gate": {
            "claim": claim,
            "confidence": confidence,
            "max_unjudged": max_unjudged,
            "passed": read.passed,
            "counted": read.counted,
            "calls_needed": calls_needed(claim, confidence, read.counted - read.passed),
            "met_by_call": read.met_calls,
            "shown_by_call": read.shown_calls,
            "by_call": read.by_call,
            "every": read.every,
            "scenarios": read.scenarios,
            "scenarios_needed": needed,
            "met_by_scenario": read.met_scenarios,
            "shown_by_scenario": read.shown_scenarios,
            "by_scenario": read.by_scenario,
            "unjudged": read.unjudged,
            "unjudged_share": read.share,
            "refused": list(read.refused),
            "met": read.met,
            "needs": [calls_needed(claim, confidence, n) for n in (0, 1, 2)],
        },
        "scenarios": [
            {
                "scenario": name,
                "passed": summary.by_scenario.get(name, [0, 0])[0],
                "graded": summary.by_scenario.get(name, [0, 0])[1],
                "placed": placed,
                "every_call_passed": placed > 0 and passed == placed,
            }
            for name, (passed, placed) in sorted(summary.by_scenario_placed.items())
        ],
        "excluded": card["not_graded"],
        "not_run": card["not_run"],
        "failure_groups": card["failure_groups"],
        "fields": card["fields"],
        "chains": card["chains"],
    }
    report["not_shown"] = _not_shown(report)
    return report


# --- the page -------------------------------------------------------------------------

_EXTRA_STYLE = """
.verdict { border:1px solid var(--line); border-left-width:6px; border-radius:8px;
  background:var(--card); padding:14px 16px; margin:16px 0; }
.verdict.met { border-left-color:var(--ok); } .verdict.short { border-left-color:var(--warn); }
.verdict.unmet { border-left-color:var(--bad); }
.verdict strong { font-size:22px; display:block; }
ul.plain { margin:8px 0; padding-left:20px; } ul.plain li { margin:6px 0; }
"""
_VERDICT_CSS = {MET: "met", NOT_ENOUGH: "short", NOT_MET: "unmet"}
_VERDICT_TEXT = {MET: "ok", NOT_ENOUGH: "warn", NOT_MET: "bad"}


def _short(value: str) -> str:
    return value[:12] if len(value) > 12 else value


def _made_with(report: dict[str, Any]) -> list[str]:
    made = report["made_with"]
    rows: list[list[str]] = []
    for target in made["targets"] or [{}]:
        profile = target.get("profile") or {}
        rows.append(
            [
                "Target",
                f"{_e(target.get('label') or NOT_RECORDED)}; version "
                f"{_e(target.get('version') or NOT_RECORDED)} (as stated by the operator); "
                f"agent <code>{_e(target.get('agent') or NOT_RECORDED)}</code>; "
                f"reached by {_e(target.get('leg') or NOT_RECORDED)}; "
                f"trace from {_e(target.get('trace') or 'none')}; profile "
                + (
                    f"{_e(profile.get('name') or profile.get('file'))} "
                    f"<code>{_e(_short(str(profile.get('sha256', ''))))}</code>"
                    if profile.get("file")
                    else "none"
                ),
            ]
        )
    for rig in made["rigs"] or [{}]:
        changed = " with changes no commit holds" if rig.get("dirty") else ""
        rows.append(
            [
                "Rig",
                f"version {_e(rig.get('version') or NOT_RECORDED)}, commit "
                f"<code>{_e(rig.get('commit') or NOT_RECORDED)}</code>{changed}",
            ]
        )
    rows.append(
        [
            "Scenario set",
            f"<code>{_e(made['scenario_set_sha256'])}</code> "
            f"({len(made['scenario_files'])} file(s) with stored calls)"
            if made["scenario_set_sha256"]
            else "no single recorded version: see the files below",
        ]
    )
    for bank in made["clip_banks"]:
        rows.append(
            [
                "Clip bank",
                f"{_e(bank['directory'])}: {bank['clips']} clip(s); sheet "
                f"<code>{_e(_short(bank['manifest_sha256']) or 'none')}</code>; overrides "
                f"<code>{_e(_short(bank['overrides_sha256']) or 'none')}</code>",
            ]
        )
    if not made["clip_banks"]:
        rows.append(["Clip bank", NOT_RECORDED])
    if made["played_sha256"]:
        rows.append(
            [
                "Audio the scenarios name",
                ", ".join(f"<code>{_e(_short(h))}</code>" for h in made["played_sha256"]),
            ]
        )
    rows.append(
        [
            "Calls placed between",
            f"{_e(made['started'] or NOT_RECORDED)} and {_e(made['ended'] or NOT_RECORDED)} "
            f"({made['batches']} batch(es); directories {_e(', '.join(report['sources']))})",
        ]
    )
    parts = [_table([("", False), ("", False)], rows)]
    files = [
        [
            _e(item["file"]),
            "<br>".join(
                f"<code>{_e(_short(value))}</code> ({count} call(s))"
                for value, count in item["hashes"].items()
            ),
        ]
        for item in made["scenario_files"]
    ]
    if files:
        parts.append(
            _table([("Scenario file", False), ("Version the calls answer to", False)], files)
        )
    return parts


def _clip_section(report: dict[str, Any]) -> list[str]:
    clips = report["clips"]
    parts = []
    if clips["unverified"]:
        parts.append(
            f"<p>{_span('warn', 'Labels not all confirmed.')} {clips['unverified_calls']} of "
            f"{clips['calls']} call(s) played a clip whose label no person has confirmed:</p>"
        )
        parts.append(
            _table(
                [("Clip", False), ("Why it is in doubt", False)],
                [[f"<code>{_e(c['clip'])}</code>", _e(c["why"])] for c in clips["unverified"]],
            )
        )
    else:
        parts.append(
            "<p>Every clip played carried a label a person has confirmed, or was a synthetic "
            "clip made from its own text.</p>"
        )
    if clips["synthetic"]:
        names = ", ".join(f"<code>{_e(c)}</code>" for c in clips["synthetic"])
        parts.append(
            f"<p>{_span('warn', 'Synthetic voice.')} {clips['synthetic_calls']} of "
            f"{clips['calls']} call(s) played a synthetic clip: {names}.</p>"
        )
    else:
        parts.append("<p>No synthetic voice was played: every clip is a recorded caller.</p>")
    return parts


def render_html(report: dict[str, Any]) -> str:
    counts, rates, gate, claim = report["counts"], report["rates"], report["gate"], report["claim"]
    verdict = report["verdict"]
    parts = [
        f"<h1>{_e(report['title'])}</h1>",
        f'<p class="meta">Generated {_e(report["generated"])}. Claim under test: a pass rate '
        f"of at least {pct(claim)}, at {pct(report['confidence'])} confidence. Each call is "
        "graded from the agent's own tool results and stored values.</p>",
        f'<div class="verdict {_VERDICT_CSS[verdict]}">'
        f'<strong class="{_VERDICT_TEXT[verdict]}">{_e(verdict)}</strong>'
        f"{_e(report['verdict_reason'])}.</div>",
        "<h2>What was tested</h2>",
        *_made_with(report),
        "<h2>Calls</h2>",
        _table(
            [("", False), ("Calls", True), ("", False)],
            [
                ["Attempted", str(counts["attempted"]), "placed, or held back by the rig"],
                ["Not run", str(counts["not_run"]), "held back by the rig; listed below"],
                ["Placed", str(counts["placed"]), ""],
                ["Graded", str(counts["graded"]), "passed or failed"],
                ["Passed", str(counts["passed"]), ""],
                [
                    "Failed",
                    str(counts["failed"]),
                    _e(
                        ", ".join(
                            f"{n} charged to {who}" for who, n in counts["failed_by_fault"].items()
                        )
                    ),
                ],
                ["Void", str(counts["void"]), "spoiled by the test rig or the fixture; re-run"],
                [
                    "Ungradeable",
                    str(counts["ungradeable"]),
                    "cannot be judged as the scenario stands",
                ],
                ["Ungraded", str(counts["ungraded"]), "no trace could be read"],
            ],
        ),
        "<h2>Pass rate</h2>",
        _table(
            [("Reading", False), ("Passed", False)],
            [
                ["Of graded calls", _e(rates["of_graded"]["text"])],
                ["Of placed calls", _e(rates["of_placed"]["text"])],
                ["Of everything attempted", _e(rates["of_attempted"]["text"])],
                [
                    "By scenario (a scenario passes only if every call placed for it passed)",
                    _e(rates["by_scenario"]["text"]),
                ],
            ],
        ),
        '<p class="meta">The first three pool every call. Repeats of one scenario replay the '
        "same audio, so the pooled intervals are narrower than the evidence supports; the "
        "reading by scenario is the one the verdict rests on.</p>",
        f"<h2>The gate for {pct(claim)}</h2>",
        _table(
            [
                ("Reading", False),
                ("Passed", True),
                ("Counted", True),
                ("Needed with this many failing", True),
                ("True rate shown, at least", True),
                ("Result", False),
            ],
            [
                [
                    "By scenario (decides)",
                    str(gate["every"]),
                    str(gate["scenarios"]),
                    str(gate["scenarios_needed"]),
                    pct(gate["shown_by_scenario"], claim),
                    _span("ok", "met") if gate["met_by_scenario"] else _span("bad", "not met"),
                ],
                [
                    "By call",
                    str(gate["passed"]),
                    str(gate["counted"]),
                    str(gate["calls_needed"]),
                    pct(gate["shown_by_call"], claim),
                    _span("ok", "met") if gate["met_by_call"] else _span("bad", "not met"),
                ],
            ],
        ),
        f'<p class="meta">{pct(claim)} at {pct(report["confidence"])} confidence needs '
        f"{gate['needs'][0]} independent results with none failing, {gate['needs'][1]} with "
        f"one, {gate['needs'][2]} with two. Not judged: {gate['unjudged']} of "
        f"{counts['attempted']} attempted ({pct(gate['unjudged_share'])}); more than "
        f"{pct(gate['max_unjudged'])} refuses the gate whatever the numbers say. Void calls "
        "are not counted; calls that were placed and could not be graded count against.</p>",
    ]
    if gate["refused"]:
        items = "".join(f"<li>{_e(reason)}</li>" for reason in gate["refused"])
        parts.append(f'<p class="bad">The gate is refused:</p><ul class="plain">{items}</ul>')

    parts.append("<h2>By scenario</h2>")
    parts.append(
        _table(
            [("Scenario", False), ("Passed of graded", True), ("Placed", True), ("", False)],
            [
                [
                    _e(s["scenario"]),
                    f"{s['passed']} of {s['graded']}",
                    str(s["placed"]),
                    _span("ok", "every call passed")
                    if s["every_call_passed"]
                    else _span("bad", "not every call passed"),
                ]
                for s in report["scenarios"]
            ],
        )
    )
    if report["chains"]:
        parts.append("<h2>Chains of calls, each graded as one</h2>")
        parts.append(
            _table(
                [("Chain", False), ("Passed", True), ("Proved nothing", True)],
                [
                    [_e(c["chain"]), f"{c['passed']} of {c['graded']}", str(c["proved_nothing"])]
                    for c in report["chains"]
                ],
            )
        )

    parts.append("<h2>Calls left out of 'of graded', each with its reason</h2>")
    if report["excluded"]:
        parts.append(
            _table(
                [("Run", False), ("Scenario", False), ("Charged to", False), ("Why", False)],
                [
                    [
                        f"<code>{_e(n['run'])}</code>",
                        _e(n["scenario"]),
                        _e(n["fault"]),
                        _e(n["reason"]),
                    ]
                    for n in report["excluded"]
                ],
            )
        )
    else:
        parts.append('<p class="muted">None: every placed call was graded.</p>')
    parts.append("<h2>Calls not run</h2>")
    if report["not_run"]:
        parts.append(
            _table(
                [("Scenario file", False), ("Times", True), ("Reason", False)],
                [
                    [_e(n["scenario_file"]), str(n["count"]), _e(n["reason"])]
                    for n in report["not_run"]
                ],
            )
        )
    else:
        parts.append('<p class="muted">None: every call the batch set out to place was placed.</p>')

    parts.append("<h2>Failures, by fault class and last step</h2>")
    if report["failure_groups"]:
        head = [
            "Charged to",
            "Last step",
            "Calls",
            "Scenarios",
            "Failed checks",
            "Stopped at",
            "Runs",
        ]
        parts.append(
            _table(
                [(h, h == "Calls") for h in head],
                [
                    [
                        _e(g["fault"]),
                        f"<code>{_e(g['last_step'])}</code>",
                        str(g["count"]),
                        "<br>".join(_e(s) for s in g["scenarios"]),
                        "<br>".join(_e(c) for c in g["checks"]),
                        _e(g["stopped_at"]),
                        "<br>".join(f"<code>{_e(r)}</code>" for r in g["runs"]),
                    ]
                    for g in report["failure_groups"]
                ],
            )
        )
    else:
        parts.append('<p class="muted">No graded call failed.</p>')

    parts.append("<h2>Said, heard, stored</h2>")
    parts += scorecard._fields({"fields": report["fields"]})
    if not any(f["disagreements"] for f in report["fields"]) and report["fields"]:
        parts.append(
            '<p class="muted">No field was heard or stored differently from what was said.</p>'
        )

    parts.append("<h2>The recordings played</h2>")
    parts += _clip_section(report)

    parts.append("<h2>What this report does not show</h2>")
    parts.append(
        '<ul class="plain">'
        + "".join(f"<li>{_e(limit)}</li>" for limit in report["not_shown"])
        + "</ul>"
    )
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_e(report['title'])}</title>\n<style>{_STYLE}{_EXTRA_STYLE}</style>\n"
        "</head>\n<body>\n<main>\n" + "\n".join(parts) + "\n</main>\n</body>\n</html>\n"
    )


def write(report: dict[str, Any], out: str | Path) -> Path:
    """Write the page, and its data beside it as ``.json``."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(report), encoding="utf-8", newline="\n")
    write_json(out.with_suffix(".json"), report)
    return out
