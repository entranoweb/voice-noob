"""Grading of spoken forms, fragments, stored-value sources and the agent's own words.

Each test that expects a pass has a twin that expects a failure for a genuinely wrong value.
"""

from __future__ import annotations

from typing import Any

from synthiq_caller.director import RunRecord, Said
from synthiq_caller.grading import field_rows, grade, normalise_phone, normalise_postcode, same
from synthiq_caller.scenario import parse_scenario
from synthiq_caller.traces.base import ToolCall, Trace
from synthiq_caller.traces.elevenlabs import parse_conversation


def _said(field: str, heard: list[str]) -> Said:
    return Said(
        t=1.0, step_index=0, field=field, intended="", clips=["x.wav"], dtmf="",
        seconds=1.0, talk_over=False, answering="", heard=heard,
    )  # fmt: skip


def _record(
    field: str, heard: list[str], timeline: list[dict[str, Any]] | None = None
) -> RunRecord:
    return RunRecord(
        scenario="s", call_ref="c", ended="caller hung up: scenario end",
        said=[_said(field, heard)], timeline=timeline or [],
    )  # fmt: skip


def _scenario(facts: dict[str, str], expect: dict[str, Any] | None = None, **more: Any) -> Any:
    return parse_scenario(
        {
            "name": "s",
            "persona": {"facts": facts},
            "steps": [{"when": "q", "clips": ["x.wav"], "field": next(iter(facts))}],
            "expect": expect or {},
            **more,
        }
    )


def _trace(*calls: ToolCall, agent_lines: list[str] | None = None) -> Trace:
    trace = Trace(call_ref="c", tool_calls=list(calls))
    if agent_lines is not None:
        trace.agent_lines = agent_lines
    return trace


def _call(name: str, **arguments: Any) -> ToolCall:
    return ToolCall(name=name, arguments=arguments, result={}, ok=True)


# --- F13: spoken forms, unparseable is unknown --------------------------------------------


def test_postcode_spoken_as_letters_and_digit_words_matches() -> None:
    assert normalise_postcode("S W one A, one A A") == "SW1A1AA"
    assert normalise_postcode("A five six T B") == "A56TB"
    assert normalise_postcode("N double eight two P Q") == "N882PQ"
    assert same("postcode", "SW1A 1AA", "S W one A one A A")


def test_a_wrong_spoken_postcode_still_differs() -> None:
    assert not same("postcode", "SW1A 1AA", "S W one A two A A")
    assert not same("postcode", "A56TB", "A five six six D B")


def test_phone_spoken_as_digit_words() -> None:
    assert normalise_phone("oh one two three, four five six, seven eight nine") == "0123456789"
    assert not same("phone", "0123456789", "oh one two three four five six seven eight eight")


def test_a_heard_value_that_cannot_be_parsed_is_unknown_not_wrong() -> None:
    scenario = _scenario({"date_of_birth": "1987-07-05"})
    rows = field_rows(scenario, _record("date_of_birth", ["the fifth of eighty seven"]), _trace())
    assert rows[0].heard_matches is None
    assert "misheard" not in rows[0].verdict


def test_a_heard_wrong_date_is_still_misheard() -> None:
    scenario = _scenario({"date_of_birth": "1987-07-05"})
    rows = field_rows(
        scenario,
        _record("date_of_birth", ["6th July 1987"]),
        _trace(_call("x", dateOfBirth="1987-07-06")),
    )
    assert rows[0].heard_matches is False
    assert rows[0].verdict.startswith("misheard")


def test_postcode_heard_as_spoken_words_is_not_misheard() -> None:
    scenario = _scenario({"postcode": "SW1A 1AA"})
    rows = field_rows(
        scenario,
        _record("postcode", ["S W one A one A A"]),
        _trace(_call("x", postcode="SW1A 1AA")),
    )
    assert rows[0].heard_matches is True


# --- F14: any fragment, or the join ---------------------------------------------------------


def test_a_match_in_an_earlier_fragment_counts_and_says_so() -> None:
    scenario = _scenario({"postcode": "SW1A 1AA"})
    rows = field_rows(
        scenario, _record("postcode", ["SW1A 1AA", "okay"]), _trace(_call("x", postcode="SW1A 1AA"))
    )
    assert rows[0].heard_matches is True
    assert rows[0].heard == "SW1A 1AA"
    assert rows[0].heard_via == "fragment 1 of 2"


def test_a_value_split_across_fragments_matches_by_the_join() -> None:
    scenario = _scenario({"postcode": "SW1A 1AA"})
    rows = field_rows(
        scenario, _record("postcode", ["SW1A", "1AA"]), _trace(_call("x", postcode="SW1A 1AA"))
    )
    assert rows[0].heard_matches is True
    assert rows[0].heard_via == "all fragments joined"


def test_fragments_that_all_differ_are_still_misheard() -> None:
    scenario = _scenario({"postcode": "SW1A 1AA"})
    rows = field_rows(
        scenario, _record("postcode", ["SW1A 2AA", "SW1B 1AA"]), _trace(_call("x", postcode="ZZ"))
    )
    assert rows[0].heard_matches is False
    assert rows[0].heard_via == ""
    assert rows[0].verdict.startswith("misheard")


# --- F15: the expected tool's arguments win; nested arguments are found ------------------------


def test_the_expected_tools_value_wins_over_a_later_lookup() -> None:
    scenario = _scenario(
        {"postcode": "SW1A 1AA"}, {"tools": [{"name_contains": "register", "called": True}]}
    )
    trace = _trace(
        _call("register_patient", postcode="SW1A 1AA"),
        _call("lookup_clinic", postcode="EC1A 1BB"),
    )
    rows = field_rows(scenario, _record("postcode", ["SW1A 1AA"]), trace)
    assert rows[0].stored == "SW1A 1AA"
    assert rows[0].stored_matches is True


def test_the_expected_tool_carrying_a_wrong_value_still_fails() -> None:
    scenario = _scenario(
        {"postcode": "SW1A 1AA"}, {"tools": [{"name_contains": "register", "called": True}]}
    )
    trace = _trace(
        _call("register_patient", postcode="SW1A 2AA"),
        _call("lookup_clinic", postcode="SW1A 1AA"),
    )
    rows = field_rows(scenario, _record("postcode", ["SW1A 1AA"]), trace)
    assert rows[0].stored == "SW1A 2AA"
    assert rows[0].stored_matches is False


def test_nested_arguments_are_searched_and_wrong_ones_still_fail() -> None:
    scenario = _scenario({"postcode": "SW1A 1AA"})
    good = _trace(_call("x", patient={"address": {"postcode": "SW1A 1AA"}}))
    bad = _trace(_call("x", patient={"address": {"postcode": "SW1A 9ZZ"}}))
    assert field_rows(scenario, _record("postcode", ["SW1A 1AA"]), good)[0].stored_matches is True
    assert field_rows(scenario, _record("postcode", ["SW1A 1AA"]), bad)[0].stored_matches is False


def test_without_an_expected_tool_the_last_value_is_used_as_before() -> None:
    scenario = _scenario({"postcode": "SW1A 1AA"})
    trace = _trace(_call("a", postcode="SW1A 1AA"), _call("b", postcode="EC1A 1BB"))
    assert field_rows(scenario, _record("postcode", ["SW1A 1AA"]), trace)[0].stored == "EC1A 1BB"


# --- F10: the agent's own transcript wins, and the source is named ------------------------------


def _says(pattern: str) -> dict[str, Any]:
    return {"agent_says": [pattern]}


def _agent(text: str) -> dict[str, Any]:
    return {"kind": "agent", "text": text}


def _check(result: Any, start: str) -> Any:
    return next(c for c in result.checks if c.what.startswith(start))


def test_agent_says_reads_the_agents_trace_when_present() -> None:
    scenario = _scenario({"name": "a b"}, _says(r"999|nine[ -]nine[ -]nine"))
    # The rig's phone leg heard "nine nine nine" as something else; the agent's own text is right.
    record = _record("name", ["a b"], [_agent("call nine nine eat")])
    result = grade(scenario, record, _trace(agent_lines=["Please call 999 now."]))
    check = _check(result, "the agent says")
    assert check.passed
    assert "agent's own transcript" in check.what


def test_agent_says_falls_back_to_the_rig_transcript_and_says_so() -> None:
    scenario = _scenario({"name": "a b"}, _says(r"999|nine[ -]nine[ -]nine"))
    record = _record("name", ["a b"], [_agent("please call nine nine nine")])
    for trace in (_trace(), _trace(agent_lines=[])):
        check = _check(grade(scenario, record, trace), "the agent says")
        assert check.passed
        assert "rig's transcript" in check.what


def test_agent_says_fails_when_the_agent_did_not_say_it_in_its_own_text() -> None:
    scenario = _scenario({"name": "a b"}, _says(r"999"))
    # The rig's phone leg may hear what the agent never said; the trace decides.
    record = _record("name", ["a b"], [_agent("call 999")])
    result = grade(scenario, record, _trace(agent_lines=["Let's carry on with your booking."]))
    assert not _check(result, "the agent says").passed


def test_agent_never_says_fails_on_a_hit_in_either_transcript() -> None:
    scenario = _scenario({"name": "a b"}, {"agent_never_says": ["date of birth"]})
    rig_hit = _record("name", ["a b"], [_agent("what is your date of birth")])
    trace_hit = _record("name", ["a b"], [_agent("how can I help")])
    # The agent's own text says it: fails even though the rig heard something else.
    said = grade(scenario, trace_hit, _trace(agent_lines=["What is your date of birth?"]))
    assert not _check(said, "the agent never says").passed
    # The rig heard it though the trace did not: still a fail, neither reading may hide it.
    heard = grade(scenario, rig_hit, _trace(agent_lines=["Please call 999."]))
    assert not _check(heard, "the agent never says").passed
    clean = grade(scenario, trace_hit, _trace(agent_lines=["Please call 999."]))
    assert _check(clean, "the agent never says").passed


# --- review of #75 -------------------------------------------------------------------------


def test_sibling_nested_values_that_disagree_are_ambiguous_never_a_pass() -> None:
    scenario = _scenario({"phone": "07700 900123"})
    args = {"emergency_contact": {"phone": "07700 900123"}, "patient": {"phone": "07999 000111"}}
    row = field_rows(scenario, _record("phone", ["07700 900123"]), _trace(_call("x", **args)))[0]
    assert row.stored_matches is not True
    # Nested siblings that agree, or a top-level key, are still read.
    agree = {"a": {"phone": "07700 900123"}, "b": {"mobile": "07700 900123"}}
    row = field_rows(scenario, _record("phone", ["07700 900123"]), _trace(_call("x", **agree)))[0]
    assert row.stored_matches is True
    top = {"phone": "07700 900123", "patient": {"phone": "07999 000111"}}
    row = field_rows(scenario, _record("phone", ["07700 900123"]), _trace(_call("x", **top)))[0]
    assert row.stored_matches is True


def test_a_wrong_postcode_inside_a_sentence_is_still_compared() -> None:
    scenario = _scenario({"postcode": "SW1A 1AA"})
    row = field_rows(scenario, _record("postcode", ["SW1A 1AB is that right"]), _trace())[0]
    assert row.heard_matches is False
    row = field_rows(scenario, _record("postcode", ["SW1A 1AA is that right"]), _trace())[0]
    assert row.heard_matches is True
    assert normalise_postcode("is that right") is None


def test_dates_are_not_matched_by_a_join_of_fragments() -> None:
    scenario = _scenario({"date_of_birth": "1990-03-12"})
    row = field_rows(scenario, _record("date_of_birth", ["12 March", "5 May 1990"]), _trace())[0]
    assert row.heard_matches is not True
    row = field_rows(scenario, _record("date_of_birth", ["12 March 1990", "okay"]), _trace())[0]
    assert row.heard_matches is True


def test_a_lone_o_in_a_phone_number_is_zero_and_double_takes_one_digit() -> None:
    assert normalise_phone("o seven seven o o nine double o one two three") == "07700900123"
    assert normalise_phone("oh seven seven oh oh nine double oh one two three") == "07700900123"
    assert not same("phone", "07700900123", "o seven seven o o nine double o one two four")
    assert normalise_phone("zero double 18") == "018"  # double repeats one digit only
    assert normalise_postcode("A double 18") == "A18"
    assert normalise_postcode("A double eight") == "A88"


def test_elevenlabs_trace_carries_the_agents_lines() -> None:
    trace = parse_conversation(
        {
            "conversation_id": "c",
            "status": "done",
            "transcript": [
                {"role": "agent", "message": "Hello, how can I help?"},
                {"role": "user", "message": "chest pain"},
                {"role": "agent", "message": "Please call 999."},
                {"role": "agent", "message": None},
            ],
        }
    )
    assert trace.agent_lines == ["Hello, how can I help?", "Please call 999."]
    assert Trace.from_json(trace.to_json()).agent_lines == trace.agent_lines
