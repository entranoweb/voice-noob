"""Grading and the ElevenLabs trace reader, on stored material. No network."""

import json
from pathlib import Path

import pytest

from synthiq_caller.director import RunRecord, Said
from synthiq_caller.grading import (
    grade,
    normalise_date,
    normalise_name,
    normalise_phone,
    normalise_postcode,
    same,
)
from synthiq_caller.scenario import parse_scenario
from synthiq_caller.traces.base import ToolCall, Trace
from synthiq_caller.traces.elevenlabs import parse_conversation

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> Trace:
    return parse_conversation(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5th July, 1987", "1987-07-05"),
        ("6th July, 1987.", "1987-07-06"),
        ("It's twentieth of February, two thousand and two.", "2002-02-20"),
        ("Fifth July nineteen eighty-seven", "1987-07-05"),
        ("20/06/1992", "1992-06-20"),
        ("01/01/1974", "1974-01-01"),
        ("1987-07-05", "1987-07-05"),
        ("12th of March, 1987", "1987-03-12"),
        ("July 28 1970", "1970-07-28"),
        ("Twenty-first of March, nineteen ninety-nine", "1999-03-21"),
        ("14 April.", None),  # no year: not a date of birth
        ("the 31st of February 1990", None),  # not a real date
        ("yes that's fine", None),
    ],
)
def test_dates_as_callers_say_them(text: str, expected: str | None) -> None:
    assert normalise_date(text) == expected


def test_the_mishearings_from_real_calls_are_told_apart() -> None:
    # 20 Nov, call 2ttj: caller said the fifth; the recogniser reported the sixth.
    assert not same("date_of_birth", "5th July, 1987", "6th July, 1987")
    assert same("date_of_birth", "1987-07-05", "5th July, 1987.")
    # 20 Nov, Alice Wonderland: one postcode, heard several ways.
    assert same("postcode", "A56TB", "It's A56 TB.")
    assert not same("postcode", "A56TB", "A56 6DB")
    assert not same("postcode", "A56TB", "A563B")
    # 13 Nov, call 8398: the clinician's first name leaked into another caller's name.
    assert not same("name", "Jordan Archer", "It's Jordan Example.")
    assert same("name", "Elliot Allen", "It's Elliot Allen.")
    assert not same("name", "Alice Wonderland", "Alex Wonderland")
    assert same("phone", "07813 953 95", "0781-395-395")
    assert not same("phone", "0782293457", "0782293458")


def test_normalisers_return_none_for_nothing() -> None:
    assert normalise_postcode("...") is None
    assert normalise_phone("no digits") is None
    assert normalise_name("...") is None
    assert not same("name", "Alice", None)


def test_workflow_trace_is_read_from_a_stored_call() -> None:
    trace = load("elevenlabs_2ttj.json")
    names = [c.name for c in trace.tool_calls]
    # Platform housekeeping is not the agent's tool use.
    assert "transfer_to_agent" not in names
    check = next(c for c in trace.tool_calls if "new_patient_check" in c.name)
    assert check.step_id == "NP_CHECK"
    assert check.ok
    assert check.arguments == {"fullName": "Elliot Allen", "dateOfBirth": "6th July, 1987"}
    # The path follows the workflow's edges in order.
    assert trace.step_path[:3] == ["R0", "I0", "I1"]
    assert trace.step_path.index("NP_CHECK") < trace.step_path.index("NP_PREP")


def test_a_refused_booking_is_not_ok() -> None:
    trace = load("elevenlabs_jgmh.json")
    book = next(c for c in trace.tool_calls if c.step_id == "MB")
    assert not book.ok
    assert "SLOT_NOT_AVAILABLE" in book.error
    confirm = next(c for c in trace.tool_calls if c.step_id == "BPC0")
    assert confirm.ok
    assert trace.step_path[-1] == "TRANSFER_RECEPTION"
    assert trace.termination == "Call was transferred to number"


def test_a_plain_agent_trace_pairs_calls_with_results() -> None:
    conversation = {
        "conversation_id": "c1",
        "transcript": [
            {
                "role": "agent",
                "time_in_call_secs": 4,
                "tool_calls": [{"tool_name": "lookup", "params_as_json": '{"dob": "1987-07-05"}'}],
                "tool_results": [
                    {
                        "tool_name": "lookup",
                        "result_value": '{"success": false, "code": "NOT_FOUND"}',
                    }
                ],
            }
        ],
    }
    trace = parse_conversation(conversation)
    assert trace.tool_calls == [
        ToolCall(
            name="lookup",
            arguments={"dob": "1987-07-05"},
            result={"success": False, "code": "NOT_FOUND"},
            ok=False,
            step_id="",
            t=4,
            error="NOT_FOUND",
        )
    ]


SCENARIO = parse_scenario(
    {
        "name": "date of birth reaches the tool",
        "persona": {"facts": {"name": "Elliot Allen", "date_of_birth": "1987-07-05"}},
        "steps": [
            {"when": "full name", "clips": ["n.wav"], "field": "name"},
            {"when": "date of birth", "clips": ["d.wav"], "field": "date_of_birth"},
        ],
        "expect": {
            "tools": [
                {"name_contains": "new_patient_check"},
                {"name_contains": "book", "called": False},
            ],
            "fields": ["date_of_birth", "name"],
        },
    }
)


def record_with(heard_dob: str) -> RunRecord:
    def said(index: int, field: str, heard: str) -> Said:
        return Said(
            t=1.0, step_index=index, field=field, intended="", clips=["x.wav"], dtmf="",
            seconds=1.0, talk_over=False, answering="", heard=[heard],
        )  # fmt: skip

    return RunRecord(
        scenario=SCENARIO.name,
        call_ref="c",
        ended="caller hung up: scenario end",
        said=[said(0, "name", "It's Elliot Allen."), said(1, "date_of_birth", heard_dob)],
    )


def test_the_real_call_fails_on_the_stored_date_of_birth() -> None:
    # The stored call: the tools received the sixth although the caller said the fifth.
    trace = load("elevenlabs_2ttj.json")
    result = grade(SCENARIO, record_with("6th July, 1987."), trace)
    rows = {row.field: row for row in result.fields}
    assert rows["name"].verdict == "sent correctly, not confirmed stored"
    assert rows["date_of_birth"].said == "1987-07-05"
    assert rows["date_of_birth"].stored == "6th July, 1987"
    assert rows["date_of_birth"].verdict == "misheard, stored wrongly"
    assert result.outcome == "failed"
    failed = [c.what for c in result.checks if not c.passed]
    assert failed == ["'date_of_birth' reaches a tool as the caller said it"]


def test_heard_right_but_stored_wrong_is_named_as_such() -> None:
    trace = load("elevenlabs_2ttj.json")
    result = grade(SCENARIO, record_with("5th July, 1987."), trace)
    row = next(r for r in result.fields if r.field == "date_of_birth")
    assert row.heard_matches is True and row.stored_matches is False
    assert row.verdict == "heard correctly, stored wrongly"


def test_a_correct_trace_passes_and_a_missing_trace_is_ungraded() -> None:
    good = Trace(
        call_ref="c",
        tool_calls=[
            ToolCall(
                name="example_v2_new_patient_check",
                arguments={"fullName": "Elliot Allen", "dateOfBirth": "5th July, 1987"},
                result={"success": True},
                ok=True,
            )
        ],
    )
    assert grade(SCENARIO, record_with("5th July, 1987."), good).outcome == "passed"
    ungraded = grade(SCENARIO, record_with("5th July, 1987."), None)
    assert ungraded.outcome == "ungraded"
    assert [row.verdict for row in ungraded.fields] == ["no trace", "no trace"]


def test_a_forbidden_tool_fails_the_call() -> None:
    trace = Trace(
        call_ref="c",
        tool_calls=[
            ToolCall(
                "new_patient_check",
                {"fullName": "Elliot Allen", "dateOfBirth": "5 July 1987"},
                {},
                True,
            ),
            ToolCall("example_v2_book", {}, {"success": True}, True),
        ],
    )
    result = grade(SCENARIO, record_with("5th July, 1987."), trace)
    assert result.outcome == "failed"
    assert [c.what for c in result.checks if not c.passed] == ["tool 'book' is not called"]


def test_trace_round_trips_through_json() -> None:
    trace = load("elevenlabs_2ttj.json")
    assert Trace.from_json(json.loads(json.dumps(trace.to_json()))) == trace
