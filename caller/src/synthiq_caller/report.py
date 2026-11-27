"""Summarise a directory of runs: how often it passed, what was misheard, where it failed."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from synthiq_caller.completeness import Completeness, read_object, read_run
from synthiq_caller.mutation_watch import describe as describe_writes
from synthiq_caller.mutation_watch import tally as tally_writes
from synthiq_caller.stats import describe, gate, wilson_interval
from synthiq_caller.timing import TimingTally, describe_timing, measure


@dataclass
class FieldTally:
    answered: int = 0
    stored_correctly: int = 0
    misheard: int = 0
    mis_stored: int = 0
    not_stored: int = 0
    # How many of the stored values were fetched from the system under test,
    # rather than taken from what the agent's tool was sent.
    read_back: int = 0
    # Answers whose clip label no person has confirmed: "said" is itself a guess.
    labels_unverified: int = 0
    # The agent's tool was sent the right value, and nothing confirmed the
    # system kept it. Not counted in ``stored_correctly``.
    sent_correctly: int = 0
    # Answers given by a synthetic voice rather than a recorded caller.
    synthetic: int = 0


@dataclass
class Failure:
    run: str
    scenario: str
    failing_step: str
    checks: list[str]
    # Who the failure is charged to: "agent" or "unknown".
    fault: str = "agent"
    reason: str = ""


@dataclass
class Excluded:
    """A call that was placed and is in neither the passes nor the failures."""

    run: str
    scenario: str
    outcome: str
    fault: str
    reason: str


@dataclass
class Summary:
    runs: int = 0
    passed: int = 0
    failed: int = 0
    ungraded: int = 0
    # Spoiled by the test rig: not a mark against the agent, and not a pass.
    void: int = 0
    # Calls in which a synthetic voice gave at least one answer.
    synthetic_calls: int = 0
    # Calls that played a clip whose manifest did not say where it came from.
    provenance_unknown_calls: int = 0
    # Calls that played a clip with no lawful basis or consent record, allowed for the run.
    consent_missing_calls: int = 0
    # Calls that played a clip that has since been removed (``forget``).
    removed_clip_calls: int = 0
    # Cannot be judged as the scenario stands (see ``grading.py``).
    ungradeable: int = 0
    # Failures by who they are charged to.
    faults: dict[str, int] = field(default_factory=dict)
    # Every placed call left out of "of graded", with its reason.
    excluded: list[Excluded] = field(default_factory=list)
    by_scenario: dict[str, list[int]] = field(default_factory=dict)  # name -> [passed, graded]
    # name -> [passed, placed and not void]. A call that could not be graded is
    # in the second number and not the first: it shows nothing, so it counts against.
    by_scenario_placed: dict[str, list[int]] = field(default_factory=dict)
    # Calls placed against a scripted stand-in for the agent (``--leg fake``).
    rehearsals: int = 0
    fields: dict[str, FieldTally] = field(default_factory=dict)
    failures: list[Failure] = field(default_factory=list)
    blocked: list[dict[str, Any]] = field(default_factory=list)
    # Chains graded as one unit: name -> [passed, graded, proved nothing].
    chains: dict[str, list[int]] = field(default_factory=dict)
    chain_failures: list[str] = field(default_factory=list)
    # How many calls went over each kind of line (socket, phone), when the run says.
    legs: dict[str, int] = field(default_factory=dict)
    # Why calls could not be graded, when the run says (a trace that was not found).
    ungraded_reasons: dict[str, int] = field(default_factory=dict)
    # Response time, re-asks and call length over every call placed (``timing.py``).
    timing: TimingTally = field(default_factory=TimingTally)
    # Undeclared-write watch over read-only scenarios (``mutation_watch.tally``).
    writes: dict[str, int] = field(default_factory=dict)


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


def ungraded_reason(payload: dict[str, Any]) -> str:
    """Why a call has no verdict, in a few words."""
    error = str(payload.get("trace_error") or "")
    if "trace not found" in error:
        return "trace not found"
    if payload.get("trace") is None:
        return "no trace" + (" (could not be read)" if error else "")
    return "nothing to check, or the system was not read back"


def fail_under_exit(
    summary: Summary, fail_under: float | None, completeness: Completeness | None = None
) -> tuple[int, str]:
    """An exit code for a pipeline: 1 when the pass rate is under ``fail_under`` percent.

    A plain threshold, not a demonstration of a rate (that is the ``gate``
    command). The rate is passed calls over every call counted: every call
    placed, less those the rig itself spoiled (void, to be re-run). A call that
    could not be graded counts against it: a batch must not clear the threshold
    by losing its traces. With nothing counted there is nothing to clear it with.
    """
    if fail_under is None:
        return 0, ""
    if completeness is not None and completeness.bad:
        return 1, (
            f"threshold: NOT met: the directory is {completeness.status.upper()}; the calls "
            "that are here do not stand for the calls that were planned"
        )
    if summary.rehearsals:
        return 1, (
            f"threshold: NOT met: {summary.rehearsals} call(s) are rehearsals against a "
            "scripted stand-in, not an agent"
        )
    counted = summary.runs - summary.void
    if counted <= 0:
        return 1, f"threshold: no calls to count, so {fail_under:g}% cannot be shown"
    rate = 100.0 * summary.passed / counted
    verdict = "met" if rate >= fail_under else "NOT met"
    spoiled = f" ({summary.void} void left out)" if summary.void else ""
    return (
        0 if rate >= fail_under else 1,
        f"threshold: {summary.passed} of {counted} calls passed ({rate:.1f}%){spoiled}; "
        f"{fail_under:g}% required: {verdict}",
    )


def summarise(directory: str | Path) -> Summary:
    root = Path(directory)
    summary = Summary()
    stored: list[dict[str, Any]] = []
    for path in sorted(root.glob("run-*.json")):
        payload = read_run(path)
        stored.append(payload)
        graded = payload["grade"]
        outcome = graded["outcome"]
        summary.runs += 1
        summary.timing.add(measure(payload))
        name = graded["scenario"]
        tally = summary.by_scenario.setdefault(name, [0, 0])
        placed = summary.by_scenario_placed.setdefault(name, [0, 0])
        if outcome != "void":
            placed[1] += 1
        if outcome == "passed":
            placed[0] += 1
        if payload.get("rehearsal"):
            summary.rehearsals += 1
        if payload.get("leg"):
            summary.legs[payload["leg"]] = summary.legs.get(payload["leg"], 0) + 1
        if outcome in ("passed", "failed"):
            tally[1] += 1
            if outcome == "passed":
                summary.passed += 1
                tally[0] += 1
            else:
                summary.failed += 1
                fault = graded.get("fault") or "agent"
                summary.faults[fault] = summary.faults.get(fault, 0) + 1
                summary.failures.append(
                    Failure(
                        run=path.name,
                        scenario=name,
                        failing_step=failing_step(payload),
                        checks=[c["what"] for c in graded["checks"] if not c["passed"]],
                        fault=fault,
                        reason=graded.get("reason", ""),
                    )
                )
        else:
            if outcome == "void":
                summary.void += 1
            elif outcome == "ungradeable":
                summary.ungradeable += 1
            else:
                summary.ungraded += 1
                why = ungraded_reason(payload)
                summary.ungraded_reasons[why] = summary.ungraded_reasons.get(why, 0) + 1
            reason = graded.get("reason", "") or "no trace, or nothing asked of the call"
            if outcome == "ungraded" and "trace not found" in str(payload.get("trace_error")):
                # A phone call whose trace could not be matched: say so, in its own words.
                reason = str(payload["trace_error"])
            summary.excluded.append(
                Excluded(
                    run=path.name,
                    scenario=name,
                    outcome=outcome,
                    fault=graded.get("fault", ""),
                    reason=reason,
                )
            )
        for row in graded["fields"]:
            field_tally = summary.fields.setdefault(row["field"], FieldTally())
            field_tally.answered += 1
            if row["stored_matches"] and row.get("stored_source") == "tool call":
                field_tally.sent_correctly += 1
            elif row["stored_matches"]:
                field_tally.stored_correctly += 1
            elif row["stored"] is None:
                field_tally.not_stored += 1
            else:
                field_tally.mis_stored += 1
            if row["heard_matches"] is False:
                field_tally.misheard += 1
            if row.get("stored_source") == "read back":
                field_tally.read_back += 1
            if row.get("labels_unverified"):
                field_tally.labels_unverified += 1
            if row.get("synthetic"):
                field_tally.synthetic += 1
        if payload.get("synthetic_clips"):
            summary.synthetic_calls += 1
        if payload.get("provenance_unknown"):
            summary.provenance_unknown_calls += 1
        if payload.get("consent_missing"):
            summary.consent_missing_calls += 1
        if payload.get("clips_removed"):
            summary.removed_clip_calls += 1
    summary.writes = tally_writes(stored)
    for path in sorted(root.glob("chain-*.json")):
        chain = read_object(path)
        chain_tally = summary.chains.setdefault(chain["chain"], [0, 0, 0])
        if chain["outcome"] == "passed":
            chain_tally[0] += 1
            chain_tally[1] += 1
        elif chain["outcome"] == "failed":
            chain_tally[1] += 1
            failed = next((c["id"] for c in chain["calls"] if c["outcome"] == "failed"), "?")
            summary.chain_failures.append(f"{path.name}  {chain['chain']}: call '{failed}' failed")
        else:
            chain_tally[2] += 1
    batch = root / "batch.json"
    if batch.exists():
        summary.blocked = list(read_object(batch).get("blocked", []))
    return summary


def scenario_level(summary: Summary) -> tuple[int, int, int]:
    """(scenarios passing every call placed, passing at least one, scenarios placed).

    A scenario passes only when every one of its calls passed, calls the rig
    itself spoiled aside. A call that was placed and could not be graded counts
    against its scenario: one pass beside two ungraded calls shows one call, not
    a scenario. A scenario with nothing but void calls is in the total and not
    among the passes.
    """
    placed = list(summary.by_scenario_placed.values())
    return (
        sum(t > 0 and p == t for p, t in placed),
        sum(p > 0 for p, _ in placed),
        len(placed),
    )


@dataclass
class GateReading:
    """A set of calls judged against a claimed pass rate (see ``gate_reading``)."""

    rate: float
    confidence: float
    max_unjudged: float
    passed: int
    # Placed and not void: what the by-call reading is out of.
    counted: int
    met_calls: bool
    shown_calls: float
    by_call: str
    # Scenarios that passed every call placed, and scenarios placed.
    every: int
    scenarios: int
    met_scenarios: bool
    shown_scenarios: float
    by_scenario: str
    attempted: int
    placed: int
    not_run: int
    unjudged: int
    share: float
    void: int
    ungraded: int
    ungradeable: int
    # Why the gate is refused whatever the numbers say (empty when it is not).
    refused: list[str] = field(default_factory=list)
    # Planned calls the rig chose not to place, and whether the claim was told to go on anyway.
    held_back: int = 0
    held_back_allowed: bool = False

    @property
    def met(self) -> bool:
        """The claim is demonstrated: by scenario, and nothing refuses it."""
        return self.met_scenarios and not self.refused


def gate_reading(
    summary: Summary,
    rate: float,
    confidence: float,
    max_unjudged: float = 0.1,
    completeness: Completeness | None = None,
    allow_held_back: bool = False,
) -> GateReading:
    """Judge a summary against a claimed pass rate.

    Two readings. By call, every placed call that is not void counts and
    anything not passed counts against. By scenario, a scenario counts once and
    passes only if every one of its calls passed: the reading that does not
    treat repeats of the same audio as independent evidence, and the one that
    decides. A call that was placed and could not be graded counts against in
    both.

    Void calls and calls that were never placed are in neither reading, so a
    batch could meet the numbers on the part of itself that happened to run.
    ``max_unjudged`` is the share of what was attempted that may go unjudged
    (void, ungraded, ungradeable, not run) before the gate is refused whatever
    the numbers say. A rehearsal against a scripted agent never meets a gate.
    """
    counted = summary.runs - summary.void
    met_calls, shown_calls, by_call = gate(summary.passed, counted, rate, confidence)
    every, _, scenarios = scenario_level(summary)
    met, shown, by_scenario = gate(every, scenarios, rate, confidence)
    held_back = completeness.held_back if completeness is not None else 0
    # batch.json and the plan are two records of the same thing; the larger is the one that
    # does not let a call go unmentioned.
    not_run = max(len(summary.blocked), held_back)
    unjudged = summary.void + summary.ungraded + summary.ungradeable + not_run
    attempted = summary.runs + not_run
    share = unjudged / attempted if attempted else 1.0
    refused = []
    if completeness is not None and completeness.bad:
        # The counts include files that are not the planned calls (copies, extras), so no
        # reading of them is shown as met or not met.
        by_call = by_scenario = f"not judged: the directory is {completeness.status.upper()}"
        met_calls = met = False
        # The numbers describe the calls that are here, not the calls that were planned.
        refused.append(
            f"the directory is {completeness.status.upper()} ("
            + "; ".join(completeness.problems[:2])
            + "): the calls here are not the calls that were planned"
        )
    if held_back and not allow_held_back:
        refused.append(
            f"{held_back} planned call(s) were held back and did not run: the claim is about "
            "every planned call (--allow-held-back judges only the calls that ran)"
        )
    if summary.rehearsals:
        refused.append(
            f"{summary.rehearsals} call(s) are rehearsals against a scripted stand-in, not an agent"
        )
    if share > max_unjudged:
        refused.append(
            f"{share:.0%} of what was attempted was not judged, more than the "
            f"{max_unjudged:.0%} allowed (--max-unjudged); the numbers above describe only "
            "the part that was"
        )
    return GateReading(
        rate=rate,
        confidence=confidence,
        max_unjudged=max_unjudged,
        passed=summary.passed,
        counted=counted,
        met_calls=met_calls,
        shown_calls=shown_calls,
        by_call=by_call,
        every=every,
        scenarios=scenarios,
        met_scenarios=met,
        shown_scenarios=shown,
        by_scenario=by_scenario,
        attempted=attempted,
        placed=summary.runs,
        not_run=not_run,
        unjudged=unjudged,
        share=share,
        void=summary.void,
        ungraded=summary.ungraded,
        ungradeable=summary.ungradeable,
        refused=refused,
        held_back=held_back,
        held_back_allowed=allow_held_back,
    )


def render(summary: Summary) -> str:
    graded = summary.passed + summary.failed
    lines = []
    if summary.rehearsals:
        lines.append(
            f"REHEARSAL: {summary.rehearsals} of {summary.runs} call(s) were placed against a "
            "scripted stand-in, not an agent. They show that the rig runs; they show nothing "
            "about any agent."
        )
    lines += [
        f"calls: {summary.runs}",
        f"passed: {describe(summary.passed, graded)}",
        f"  of graded:    {describe(summary.passed, graded)}",
        f"  of attempted: {describe(summary.passed, summary.runs)}",
    ]
    every, some, scenarios = scenario_level(summary)
    if scenarios:
        low, high = wilson_interval(every, scenarios)
        lines.append(
            f"  by scenario:  {every} of {scenarios} scenarios passed every call placed "
            f"({every / scenarios:.0%}; 95% interval {low:.0%} to {high:.0%}); "
            f"{some} passed at least once"
        )
        if graded > scenarios:
            lines.append(
                "  Repeats of one scenario replay the same audio and are not independent "
                "calls, so the 'of graded' interval is too narrow. The honest interval is "
                "the one by scenario."
            )
    if summary.failed:
        charged = ", ".join(f"{who} {count}" for who, count in sorted(summary.faults.items()))
        lines.append(f"failed: {summary.failed} (charged to: {charged})")
    if summary.excluded:
        lines.append(
            f"left out of 'of graded': {len(summary.excluded)} "
            f"(void {summary.void}, ungradeable {summary.ungradeable}, "
            f"ungraded {summary.ungraded})"
        )
        for left in summary.excluded:
            charged = f", fault {left.fault}" if left.fault else ""
            lines.append(f"  {left.run}  {left.scenario}: {left.outcome}{charged}: {left.reason}")
    if summary.legs:
        lines.append(
            "by line: " + ", ".join(f"{name} {n}" for name, n in sorted(summary.legs.items()))
        )

    lines.append("")
    lines += describe_timing(summary.timing.to_json())
    lines += describe_writes(summary.writes)
    lines.append("\nper scenario")
    for name, (passed, total) in sorted(summary.by_scenario.items()):
        lines.append(f"  {describe(passed, total):<52} {name}")

    if summary.fields:
        lines.append("\nsaid, heard, stored (per field, all calls)")
        lines.append(
            f"  {'field':<14} {'answered':>8} {'stored ok':>10} {'misheard':>9} "
            f"{'mis-stored':>11} {'not stored':>11} {'read back':>10} {'unverified':>11} "
            f"{'sent, unconfirmed':>18} {'synthetic':>10}"
        )
        for name, t in sorted(summary.fields.items()):
            lines.append(
                f"  {name:<14} {t.answered:>8} {t.stored_correctly:>10} {t.misheard:>9} "
                f"{t.mis_stored:>11} {t.not_stored:>11} {t.read_back:>10} "
                f"{t.labels_unverified:>11} {t.sent_correctly:>18} {t.synthetic:>10}"
            )
        sent = sum(t.sent_correctly for t in summary.fields.values())
        if sent:
            lines.append(
                f"  'stored ok' counts only values read back from the system. In {sent} "
                "answer(s) the right value was sent to a tool and nothing confirmed it was "
                "kept: sent, not confirmed stored."
            )
        if summary.synthetic_calls:
            lines.append(
                f"  synthetic voice: {summary.synthetic_calls} call(s) played a synthetic clip "
                "where no recording of a real caller exists; those answers say nothing about "
                "how the agent hears real people"
            )
        if summary.provenance_unknown_calls:
            lines.append(
                f"  provenance unknown: {summary.provenance_unknown_calls} call(s) played a clip "
                "whose manifest does not say if it is a real recording or a synthetic voice, "
                "or on what basis it is held"
            )
        if summary.consent_missing_calls:
            lines.append(
                f"  consent record missing: {summary.consent_missing_calls} call(s) played a clip "
                "with no lawful basis or consent record, allowed for the run"
            )
        if summary.removed_clip_calls:
            lines.append(
                f"  clips removed: {summary.removed_clip_calls} call(s) played a clip that has "
                "since been removed with 'forget'; the call stands as it was graded, the audio "
                "is gone and the call cannot be played again"
            )
        doubtful = sum(t.labels_unverified for t in summary.fields.values())
        if doubtful:
            lines.append(
                f"  labels unverified: in {doubtful} answer(s) 'said' is a recogniser's reading "
                "of the clip that no person has confirmed; a match or a mismatch there may "
                "be the label's doing"
            )
        stored = sum(t.answered - t.not_stored for t in summary.fields.values())
        fetched = sum(t.read_back for t in summary.fields.values())
        lines.append(
            f"  'stored' was read back from the system in {fetched} of {stored} cases; "
            "the rest is what the agent's tool was sent"
        )

    if summary.chains:
        lines.append("\nchains (each graded as one unit)")
        for name, (passed, total, unproven) in sorted(summary.chains.items()):
            note = f"  [{unproven} proved nothing: not run or ungraded]" if unproven else ""
            lines.append(f"  {describe(passed, total):<52} {name}{note}")
        for chain_failure in summary.chain_failures:
            lines.append(f"    {chain_failure}")

    if summary.failures:
        lines.append("\nfailures")
        for failure in summary.failures:
            lines.append(f"  {failure.run}  {failure.scenario}  [fault: {failure.fault}]")
            if failure.reason:
                lines.append(f"    {failure.reason}")
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
