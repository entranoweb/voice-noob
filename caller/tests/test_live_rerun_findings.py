"""What the live re-run of 25 November 2026 got wrong, pinned with runs saved from it.

The fixtures are three of that run's files, cut down and scrubbed of the
clinic's name and number (``tests/fixtures/rerun_*.json``).
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from synthiq_caller.batch import load_scenarios, run_batch
from synthiq_caller.clips import ClipBank
from synthiq_caller.director import RunRecord
from synthiq_caller.grading import grade
from synthiq_caller.jsonio import write_json
from synthiq_caller.scenario import ScenarioError, load_scenario, parse_scenario
from synthiq_caller.target import TargetError, load_target, parse_target
from synthiq_caller.traces.base import Trace
from synthiq_caller.validation import validate_directory

HERE = Path(__file__).parent
SOPHIE = HERE.parent / "scenarios" / "sophie"
ORIGINAL = SOPHIE / "targets" / "original-clinic.json"
TEST_CLINIC = SOPHIE / "targets" / "test-clinic.json"


def saved(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (HERE / "fixtures" / f"rerun_{name}.json").read_text(encoding="utf-8")
    )
    return loaded


def regraded(name: str) -> Any:
    run = saved(name)
    scenario = load_scenario(SOPHIE / run["scenario_file"], load_target(ORIGINAL))
    return grade(scenario, RunRecord.from_json(run["record"]), Trace.from_json(run["trace"]))


# --- (i) the caller had no answer, the agent gave up and said goodbye -------------------------


def test_a_question_the_caller_could_not_answer_is_the_rigs_even_when_the_agent_said_goodbye() -> (
    None
):
    run = saved("unanswered_then_goodbye")
    # The agent asked three times, gave up, and its farewell was the scenario's last line.
    assert run["record"]["ended"] == "caller hung up: scenario end"
    assert len(run["record"]["unanswered"]) == 3
    assert run["graded_then"] == {"outcome": "failed", "fault": "agent"}  # what it was charged

    result = regraded("unanswered_then_goodbye")
    assert (result.outcome, result.fault) == ("void", "rig")
    assert "for you or someone else" in result.reason


# --- (ii) the system under test was not in the state the scenario needs -----------------------


def test_a_patient_who_was_already_there_is_the_fixtures_fault_not_the_agents() -> None:
    assert saved("duplicate_patient")["graded_then"] == {"outcome": "failed", "fault": "agent"}
    result = regraded("duplicate_patient")
    assert (result.outcome, result.fault) == ("void", "fixture")
    assert "DUPLICATE_FOUND" in result.reason and "register_patient" in result.reason


def test_a_fixture_mark_does_not_excuse_a_call_that_otherwise_passed_or_said_the_forbidden() -> (
    None
):
    run = saved("duplicate_patient")
    record, trace = RunRecord.from_json(run["record"]), Trace.from_json(run["trace"])
    forbidding = parse_scenario(
        {
            "name": "x",
            "mutates": "none",
            "steps": [{"when": "help", "clips": ["a.wav"]}],
            "hangup_after": "goodbye",
            "expect": {"agent_never_says": ["reception"], "tools": [{"name": "nothing"}]},
            "fixture_faults": {"register_patient": ["DUPLICATE_FOUND"]},
        }
    )
    # The agent said something it must never say. That is its own doing.
    assert grade(forbidding, record, trace).fault == "agent"


# --- (iii) "is the appointment for yourself, or for someone else?" ----------------------------

ASKED_LIVE = (
    "Just so I know, is the appointment for yourself, or for someone else?",
    "I’m here. Just to check, is the appointment for yourself, or for someone else?",
    "I'm here, sorry. Is the appointment for you or someone else?",
)


def test_the_booking_scenarios_answer_who_the_appointment_is_for() -> None:
    for number in ("01", "02", "03", "04", "05", "06", "07", "16", "17"):
        path = next(SOPHIE.glob(f"{number}-*.json"))
        scenario = load_scenario(path)
        step = next((s for s in scenario.steps if s.clips == ("booking_for_myself",)), None)
        assert step is not None, path.name
        for line in ASKED_LIVE:
            assert step.when.search(line), (path.name, line)
    # The caller who is booking for a son has an answer of their own.
    son = load_scenario(next(SOPHIE.glob("20-*.json")))
    assert not any("booking_for_myself" in s.clips for s in son.steps)


# --- (iv) the clinic's times belong to the clinic's profile ------------------------------------


def test_no_scenario_file_states_a_bookable_day_or_time() -> None:
    for path in sorted(SOPHIE.glob("*.json")):
        booking = (json.loads(path.read_text(encoding="utf-8")).get("expect") or {}).get("booking")
        for part in ("weekdays", "time_from", "time_to", "date_from", "date_to"):
            assert part not in (booking or {}), f"{path.name} states {part}"


def test_a_scenario_gets_its_times_from_the_target_and_is_held_back_without_them(
    tmp_path: Path,
) -> None:
    shoulder = "01-consultation-self-pay-shoulder.json"
    with_times = dict(load_scenarios(SOPHIE, load_target(ORIGINAL)))[shoulder]
    assert (with_times.expect.booking.time_from, with_times.missing_target) == ("16:30", ())

    for target in (None, load_target(TEST_CLINIC)):  # no profile, and one not yet filled in
        without = dict(load_scenarios(SOPHIE, target))[shoulder]
        assert without.missing_target == ("time_from", "time_to")

    rows = {
        row.file: row for row in validate_directory(SOPHIE, None, None, load_target(TEST_CLINIC))
    }
    assert rows[shoulder].missing_target and "TARGET PROFILE LACKS" in rows[shoulder].line()
    assert not rows["14-symptom-first-new-caller.json"].missing_target


async def test_a_batch_does_not_place_a_call_whose_times_the_target_did_not_give(
    tmp_path: Path,
) -> None:
    scenarios = [(f, s) for f, s in load_scenarios(SOPHIE) if f.startswith("01-")]

    def never() -> Any:
        raise AssertionError("no call may be placed")

    result = await run_batch(
        scenarios, never, ClipBank(tmp_path), tmp_path / "runs", allow_mutating="all"
    )
    assert result.ran == 0 and "target profile does not give" in result.blocked[0].reason


def test_a_profile_fills_in_zone_marks_and_budget_and_is_checked_when_read() -> None:
    raw = {
        "name": "a booking",
        "mutates": "books",
        "steps": [{"when": "help", "clips": ["a.wav"]}],
        "expect": {
            "tools": [{"name": "book"}],
            "booking": {"type": "consultation"},
            "booking_from_target": ["weekdays"],
        },
    }
    target = parse_target(
        {
            "name": "somewhere",
            "timezone": "Europe/Paris",
            "naive_times": "UTC",
            "identity_budget": 3,
            "fixture_faults": {"register": ["ALREADY_THERE"]},
            "bookings": {"a-booking": {"weekdays": ["friday"]}},
        }
    )
    made = parse_scenario(raw, target, "a-booking")
    booking = made.expect.booking
    assert (booking.weekdays, booking.timezone, booking.naive_times) == (
        ("friday",),
        "Europe/Paris",
        "UTC",
    )
    assert (made.identity_budget, made.fixture_faults) == (3, {"register": ("ALREADY_THERE",)})
    assert made.missing_target == ()

    with pytest.raises(TargetError, match="unknown parts"):
        parse_target({"name": "x", "bookable": {}})
    with pytest.raises(ScenarioError, match="unknown parts"):
        parse_scenario({**raw, "expect": {**raw["expect"], "booking_from_target": ["hour"]}})


# --- (v) the pound sign and the apostrophe, on the way out -------------------------------------


def test_a_saved_runs_pound_sign_and_apostrophe_survive_the_file_and_the_console(
    tmp_path: Path,
) -> None:
    fee, sorry = saved("fee_in_pounds"), saved("unanswered_then_goodbye")
    fee_line = next(e["text"] for e in fee["record"]["timeline"] if "£" in e.get("text", ""))
    assert "’" in json.dumps(sorry["record"], ensure_ascii=False)

    # Stored: UTF-8 bytes of the characters themselves, not escapes and not another code page.
    for index, (run, last_line) in enumerate(((fee, fee_line), (sorry, "I’m here, sorry.")), 1):
        payload = {
            "record": {
                **run["record"],
                "timeline": [{"t": 1.0, "kind": "agent", "text": last_line}],
            },
            "trace": None,
            "grade": {
                "scenario": "s",
                "outcome": "failed",
                "fault": "agent",
                "reason": "",
                "checks": [{"what": "x", "passed": False}],
                "fields": [],
            },
        }
        write_json(tmp_path / f"run-{index:03d}.json", payload)
    stored = (tmp_path / "run-001.json").read_bytes() + (tmp_path / "run-002.json").read_bytes()
    assert b"\xc2\xa3" in stored and b"\xe2\x80\x99" in stored and b"\\u" not in stored

    # Printed: the same bytes, with nothing in the environment asking for UTF-8.
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    printed = subprocess.run(
        [sys.executable, "-m", "synthiq_caller.cli", "report", str(tmp_path), "--summary-only"],
        capture_output=True,
        env=env,
        check=True,
    ).stdout
    assert b"\xc2\xa3" in printed and b"\xe2\x80\x99" in printed
    assert b"\xef\xbf\xbd" not in printed and b"?375" not in printed
