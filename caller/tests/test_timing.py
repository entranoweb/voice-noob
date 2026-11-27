"""C7: response time, re-asks and call length come from the timeline; no data is 'not measured'."""

import json
from pathlib import Path
from typing import Any

from test_ci_gate import AGENTS, SETS

from synthiq_caller import ci_gate, scorecard
from synthiq_caller.cli import main
from synthiq_caller.report import render, summarise
from synthiq_caller.timing import (
    NOT_MEASURED,
    CallTiming,
    TimingTally,
    describe_timing,
    measure,
    percentile,
)
from synthiq_caller.turns import AgentTurnTracker


def payload(timeline: list[dict[str, Any]], said: list[dict[str, Any]] | None, dur: float) -> Any:
    record: dict[str, Any] = {"timeline": timeline, "duration_s": dur}
    if said is not None:
        record["said"] = said
    return {"record": record}


def test_latency_is_agent_first_audio_after_the_end_of_the_answer() -> None:
    timeline = [
        {"t": 1.0, "kind": "agent_speech"},
        {"t": 2.0, "kind": "said", "step": 0, "seconds": 1.0},  # answer ends at 3.0
        {"t": 3.8, "kind": "agent_speech"},
        {"t": 6.0, "kind": "said", "step": 1, "seconds": 2.0},  # ends at 8.0
        {"t": 9.5, "kind": "agent_speech"},
    ]
    found = measure(payload(timeline, [{"step_index": 0}, {"step_index": 1}], 12.0))
    assert found.latencies_s == [0.8, 1.5]
    assert found.reasks == 0 and found.length_s == 12.0


def test_agent_talking_over_the_answer_or_never_replying_has_no_latency() -> None:
    timeline = [
        {"t": 2.0, "kind": "said", "step": 0, "seconds": 2.0},
        {"t": 3.0, "kind": "agent_speech"},  # during the answer: a barge-in, not a response
        {"t": 7.0, "kind": "said", "step": 1, "seconds": 1.0},
    ]
    assert measure(payload(timeline, [], 9.0)).latencies_s == []


def test_a_repeated_step_is_a_reask_and_a_fallback_is_not() -> None:
    said = [{"step_index": 2}, {"step_index": 2}, {"step_index": 2}, {"step_index": -1}]
    assert measure(payload([], said, 5.0)).reasks == 2


def test_missing_data_is_none_never_zero() -> None:
    found = measure({"record": {"timeline": []}})
    assert found.latencies_s == [] and found.reasks is None and found.length_s is None
    tally = TimingTally()
    tally.add(found)
    lines = describe_timing(tally.to_json())
    assert len(lines) == 3 and all(NOT_MEASURED in line for line in lines)
    assert not any(" 0 " in line for line in lines)


def test_figures_are_median_and_p90_over_all_calls() -> None:
    tally = TimingTally()
    for latency, length in ((1.0, 10.0), (2.0, 20.0), (3.0, 30.0), (10.0, 40.0)):
        tally.add(
            measure(
                payload(
                    [
                        {"t": 0.0, "kind": "said", "seconds": 1.0},
                        {"t": 1.0 + latency, "kind": "agent_speech"},
                    ],
                    [{"step_index": 0}],
                    length,
                )
            )
        )
    card = tally.to_json()
    assert card["response_median_s"] == 2.5 and card["response_p90_s"] == 10.0
    assert card["length_median_s"] == 25.0 and card["reasks"] == 0
    assert percentile([1.0, 2.0], 0.9) == 2.0


def test_the_tracker_reports_each_new_stretch_of_speech_once() -> None:
    tracker = AgentTurnTracker()
    rate, frame = 16000, 0.02
    loud = b"\xe8\x03" * int(rate * frame)  # level 1000
    quiet = b"\0\0" * int(rate * frame)
    starts: list[float] = []
    now = 0.0
    for chunk in [quiet] * 5 + [loud] * 20 + [quiet] * 30 + [loud] * 20:
        began = tracker.feed(chunk, rate, now)
        if began is not None:
            starts.append(round(began, 2))
        now += frame
    assert starts == [0.1, 1.1]


def test_a_rehearsal_run_carries_the_figures_into_the_report_and_scorecard(
    tmp_path: Path,
) -> None:
    bank, out = tmp_path / "bank", tmp_path / "runs"
    ci_gate.make_bank(SETS / "smoke", bank)
    main(
        ["batch", str(SETS / "smoke"), "--leg", "fake", "--fake-agent", str(AGENTS / "smoke"),
         "--clips", str(bank), "--allow-mutating", "none", "--out", str(out), "--settle", "0",
         "--only", "01-"]
    )  # fmt: skip
    text = render(summarise(out))
    assert "response time" in text and "re-asks" in text and "call length" in text
    timing = summarise(out).timing.to_json()
    assert timing["response_turns"] >= 1
    assert timing["response_median_s"] is not None and timing["response_median_s"] > 0
    card = scorecard.build([out])
    assert card["timing"]["response_turns"] == timing["response_turns"]
    assert "Response time, re-asks, call length" in scorecard.render_html(card)


def test_old_runs_without_the_data_say_not_measured(tmp_path: Path) -> None:
    run = {
        "grade": {"scenario": "s", "outcome": "passed", "checks": [], "fields": []},
        "record": {"scenario": "s", "timeline": [], "said": []},
    }
    (tmp_path / "run-001.json").write_text(json.dumps(run))
    lines = render(summarise(tmp_path))
    assert "response time (agent first audio after the caller's answer): not measured" in lines
    assert "call length: not measured" in lines


def test_p90_is_labelled_nearest_rank() -> None:
    tally = TimingTally()
    tally.add(CallTiming(latencies_s=[0.5, 1.0], reasks=0, length_s=3.0, has_agent_speech=True))
    assert "nearest-rank" in "\n".join(describe_timing(tally.to_json()))
