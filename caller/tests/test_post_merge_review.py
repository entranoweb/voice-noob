"""One test per finding of the post-merge review (docs/audits/2026-11-25/post-merge-review.md).

Each is built from the review's own reproduction and failed before its fix.
"""

import json
import sys
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from conftest import mark_checked
from test_reset_chains_readback import BOOKED, CANCELLED, agents, chain_data

from synthiq_caller import cli, zones
from synthiq_caller.batch import CallInfo, ShellHooks, looks_unfinished, run_batch
from synthiq_caller.chain import parse_chain
from synthiq_caller.clips import ClipBank
from synthiq_caller.compare import Baseline, compare, load_baseline
from synthiq_caller.director import Director, RunRecord, asked_at, asks_something
from synthiq_caller.external import ExternalFailed, run_command
from synthiq_caller.grading import grade, parse_when
from synthiq_caller.jsonio import write_json, write_new_json
from synthiq_caller.leg import AgentText, Leg, LegEvent
from synthiq_caller.legs.fake import RATE, AgentLine, FakeLeg, tone
from synthiq_caller.readback import CommandReadBack, ReadBackContext, ReadBackFailed, _values
from synthiq_caller.report import Summary, scenario_level, summarise
from synthiq_caller.scenario import Scenario, ScenarioError, parse_scenario
from synthiq_caller.traces.base import ToolCall, Trace, TraceReader
from synthiq_caller.traces.vapi import parse_call
from synthiq_caller.turns import TurnSettings
from synthiq_caller.validation import validate_directory

FAST = TurnSettings(hangover_s=0.2, audio_start_wait_s=0.6, min_speech_s=0.06)
END = "caller hung up: scenario end"
PY = sys.executable
CLIPS = ("opener.wav", "dob.wav", "postcode.wav", "phone.wav", "sorry.wav")


def write_clips(directory: Path, names: tuple[str, ...] = CLIPS) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        with wave.open(str(directory / name), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(RATE)
            wav.writeframes(tone(0.2))
    mark_checked(directory)
    return directory


@pytest.fixture
def bank(tmp_path: Path) -> ClipBank:
    return ClipBank(write_clips(tmp_path / "bank"))


def scenario(**extra: Any) -> Scenario:
    data: dict[str, Any] = {
        "name": "a call",
        "mutates": "none",
        "persona": {
            "facts": {
                "name": "Alice Wonderland",
                "date_of_birth": "2002-02-20",
                "postcode": "A56TB",
                "phone": "07700900123",
            }
        },
        # The order the example scenarios list them in: phone, postcode, date of birth.
        "steps": [
            {"when": "how can i help", "clips": ["opener.wav"]},
            {"when": "mobile|phone number", "clips": ["phone.wav"], "field": "phone"},
            {"when": "post ?code", "clips": ["postcode.wav"], "field": "postcode"},
            {"when": "date of birth", "clips": ["dob.wav"], "field": "date_of_birth"},
        ],
        "hangup_after": "goodbye",
        "max_seconds": 20,
        "expect": {"tools": [{"name_contains": "check"}]},
    }
    data.update(extra)
    return parse_scenario(data)


def record(ended: str = END, **extra: Any) -> RunRecord:
    return RunRecord(scenario="a call", call_ref="c1", ended=ended, **extra)


def trace(*calls: ToolCall, **extra: Any) -> Trace:
    return Trace(call_ref="c1", tool_calls=list(calls), step_path=["S1"], **extra)


CHECK = ToolCall("patient_check", {"dateOfBirth": "2002-02-20"}, {"success": True}, True)


def stored_run(directory: Path, index: int, name: str, outcome: str, **extra: Any) -> None:
    payload = {
        "record": record().to_json(),
        "grade": {"scenario": name, "outcome": outcome, "checks": [], "fields": [], "fault": ""},
        **extra,
    }
    write_json(directory / f"run-{index:03d}.json", payload)


# --- F1. the gate ----------------------------------------------------------------------


def test_the_gate_is_not_met_by_scenarios_whose_other_calls_were_never_graded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 59 scenarios, each with one pass, one ungraded and one ungradeable call.
    index = 0
    for number in range(59):
        for outcome in ("passed", "ungraded", "ungradeable"):
            index += 1
            stored_run(tmp_path, index, f"scenario {number}", outcome)
    every, some, scenarios = scenario_level(summarise(tmp_path))
    assert (every, some, scenarios) == (0, 59, 59)
    assert cli._gate(str(tmp_path), 0.95, 0.95) == 1
    printed = capsys.readouterr().out
    assert "by scenario: 0 of 59" in printed and "NOT met" in printed


def test_the_gate_is_refused_when_too_much_of_the_batch_was_never_judged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for number in range(59):
        stored_run(tmp_path, number + 1, f"scenario {number}", "passed")
    blocked = [
        {"scenario_file": f"x{n}.json", "scenario": "x", "repeat": 1, "reason": "guard"}
        for n in range(30)
    ]
    write_json(tmp_path / "batch.json", {"out": str(tmp_path), "ran": 59, "blocked": blocked})

    # 59 of 59 on the numbers, and a third of the batch was never placed.
    assert cli._gate(str(tmp_path), 0.95, 0.95) == 1
    printed = capsys.readouterr().out
    assert "NOT met" in printed and "was not judged" in printed and "--max-unjudged" in printed
    # The operator may state a wider tolerance, and must do so out loud.
    assert cli._gate(str(tmp_path), 0.95, 0.95, max_unjudged=0.5) == 0


def test_a_scenario_with_only_void_calls_is_not_among_the_passes(tmp_path: Path) -> None:
    stored_run(tmp_path, 1, "proved", "passed")
    stored_run(tmp_path, 2, "spoiled", "void")
    stored_run(tmp_path, 3, "mixed", "passed")
    stored_run(tmp_path, 4, "mixed", "void")
    assert scenario_level(summarise(tmp_path)) == (2, 2, 3)


# --- F2 and G1. how the call ended -------------------------------------------------------


def test_a_call_the_agent_hung_up_on_does_not_pass_even_when_its_checks_hold() -> None:
    early = grade(scenario(), record("agent ended the call socket closed"), trace(CHECK))
    assert (early.outcome, early.fault) == ("failed", "unknown")
    assert any("ended early" in c.what and not c.passed for c in early.checks)
    assert "ended before the scenario's end" in early.reason

    dropped = grade(scenario(), record("line dropped: ConnectionResetError()"), trace(CHECK))
    assert (dropped.outcome, dropped.fault) == ("failed", "unknown")

    assert grade(scenario(), record(END), trace(CHECK)).outcome == "passed"


def test_a_scenario_may_say_that_the_agent_ending_the_call_is_its_proper_end() -> None:
    transfers = scenario(agent_may_end=True)
    assert grade(transfers, record("agent ended the call transfer"), trace(CHECK)).outcome == (
        "passed"
    )
    # A dropped line is never a proper end.
    dropped = grade(transfers, record("line dropped: ConnectionResetError()"), trace(CHECK))
    assert dropped.outcome == "failed"


def test_a_crash_in_the_rig_is_void_with_or_without_a_trace() -> None:
    missing_clip = "rig error: FileNotFoundError('clip not found in bank: x.wav')"
    for ended in (missing_clip, "error: ConnectionClosedError()"):  # the second is the old form
        with_trace = grade(scenario(), record(ended), trace(CHECK))
        assert (with_trace.outcome, with_trace.fault) == ("void", "rig")
        without = grade(scenario(), record(ended), None)
        assert (without.outcome, without.fault) == ("void", "rig")


async def test_a_clip_that_is_not_there_at_play_time_is_recorded_as_the_rig_breaking(
    tmp_path: Path,
) -> None:
    bank = ClipBank(write_clips(tmp_path / "bank", ("opener.wav",)))  # no dob.wav
    leg = FakeLeg([AgentLine("How can I help?"), AgentLine("Your date of birth?")])
    done = await Director(scenario(), leg, bank, turn_settings=FAST).run()
    assert done.ended.startswith("rig error")
    assert grade(scenario(), done, trace(CHECK)).outcome == "void"


class DroppingLeg(FakeLeg):
    """A line that goes down under the reader after the agent's first words."""

    async def events(self) -> AsyncIterator[LegEvent]:
        yield AgentText("How can I help?")
        raise ConnectionResetError("the far end went away")


async def test_a_connection_failing_under_the_call_is_a_dropped_line_not_the_rig(
    bank: ClipBank,
) -> None:
    done = await Director(scenario(), DroppingLeg([]), bank, turn_settings=FAST).run()
    assert done.ended.startswith("line dropped")
    result = grade(scenario(), done, None)
    assert (result.outcome, result.fault) == ("failed", "unknown")


# --- F3. which tool the booking is read from ---------------------------------------------


def booking_scenario(**booking: Any) -> Scenario:
    return scenario(
        mutates="books",
        expect={"tools": [{"name": "book"}], "booking": {"weekdays": ["wednesday"], **booking}},
    )


THURSDAY_THEN_A_WEDNESDAY_LOOKUP = (
    ToolCall("book", {"startTime": "2026-12-03T10:00:00Z"}, {"success": True}, True),
    ToolCall("check_availability", {"startTime": "2026-12-09T10:00:00Z"}, {"success": True}, True),
)


def test_the_booked_day_is_read_from_the_booking_not_from_a_later_lookup() -> None:
    named = grade(booking_scenario(tool="book"), record(), trace(*THURSDAY_THEN_A_WEDNESDAY_LOOKUP))
    assert (named.outcome, named.fault) == ("failed", "agent")
    assert any("2026-12-03" in c.what and not c.passed for c in named.checks)

    # With no tool named, two tools that disagree leave the day unknown, never guessed.
    unnamed = grade(booking_scenario(), record(), trace(*THURSDAY_THEN_A_WEDNESDAY_LOOKUP))
    assert unnamed.outcome == "ungradeable" and "expect.booking.tool" in unnamed.reason


def test_every_example_scenario_that_writes_an_appointment_names_the_tool_that_writes_it() -> None:
    directory = Path(__file__).parent.parent / "scenarios" / "example-receptionist"
    for path in sorted(directory.glob("*.json")):
        loaded = parse_scenario(json.loads(path.read_text(encoding="utf-8")))
        if loaded.expect.booking.stated():
            assert loaded.expect.booking.tool, path.name


# --- F4. "is refused" ---------------------------------------------------------------------

VERIFY_RULE = {"verify": {"path": "verified", "success": ["true"]}}


def refusal(**tool: Any) -> Scenario:
    return scenario(
        expect={"tools": [{"name": "verify", "ok": False, **tool}]}, tool_outcomes=VERIFY_RULE
    )


def verify(verified: bool) -> ToolCall:
    return ToolCall("verify", {}, {"success": True, "verified": verified}, True)


def test_a_request_that_failed_on_the_way_is_not_a_refusal() -> None:
    errored = ToolCall("verify", {}, None, False, error="HTTP 500")
    result = grade(refusal(), record(), trace(errored))
    assert (
        result.outcome == "ungradeable" and "failed before the tool could answer" in result.reason
    )
    # The same with no rule for the tool: the error text says it never answered.
    plain = scenario(expect={"tools": [{"name": "verify", "ok": False}]})
    assert grade(plain, record(), trace(errored)).outcome == "ungradeable"


def test_a_refusal_the_agent_got_past_is_not_a_refusal_unless_once_is_enough() -> None:
    then_through = trace(verify(False), verify(True))
    assert grade(refusal(), record(), then_through).outcome == "failed"
    assert grade(refusal(at_least_once=True), record(), then_through).outcome == "passed"
    assert grade(refusal(), record(), trace(verify(True), verify(False))).outcome == "passed"
    assert grade(refusal(), record(), trace(verify(False))).outcome == "passed"


# --- F5. a read-back that holds nothing ---------------------------------------------------


def test_a_read_back_that_holds_nothing_fails_the_field_and_is_not_replaced() -> None:
    assert _values('{"name": "Alice Wonderland", "phone": null, "postcode": ""}') == {
        "name": "Alice Wonderland",
        "phone": "",
        "postcode": "",
    }
    from synthiq_caller.director import Said

    said = Said(1.0, 1, "phone", "", ["phone.wav"], "", 0.3, False, "number?", ["07700 900123"])
    registered = ToolCall("register_check", {"phone": "07700900123"}, {"success": True}, True)
    wants_phone = scenario(expect={"tools": [{"name_contains": "check"}], "fields": ["phone"]})

    held_nothing = grade(wants_phone, record(said=[said]), trace(registered), {"phone": ""})
    row = held_nothing.fields[0]
    assert held_nothing.outcome == "failed"
    assert (row.stored, row.stored_matches, row.stored_source) == (None, False, "read back")
    assert "holds nothing" in row.verdict

    # A read-back that does not speak of the field at all is a gap: the tool's value, labelled.
    silent = grade(wants_phone, record(said=[said]), trace(registered), {"name": "Alice"})
    assert silent.fields[0].stored_source == "tool call"
    assert silent.fields[0].verdict.startswith("sent correctly, not confirmed stored")


def test_an_appointment_the_system_does_not_hold_fails_whatever_the_tool_said() -> None:
    wanted = booking_scenario(tool="book", start_keys=["appointment_start"])
    said_booked = ToolCall("book", {"appointment_start": "2026-12-09T10:00:00Z"}, {}, True)
    result = grade(wanted, record(), trace(said_booked), {"appointment_start": ""})
    assert (result.outcome, result.fault) == ("failed", "agent")
    assert any("holds nothing" in c.what for c in result.checks)


# --- F6. run files are never written over -------------------------------------------------


def agent() -> tuple[Leg, TraceReader | None]:
    return FakeLeg([AgentLine("How can I help?"), AgentLine("Thank you, goodbye.")]), None


async def test_a_new_run_never_takes_the_number_of_a_run_that_is_stored(
    bank: ClipBank, tmp_path: Path
) -> None:
    out = tmp_path / "runs"
    out.mkdir()
    for kept in ("run-001.json", "run-003.json"):  # run-002 was removed
        (out / kept).write_text(json.dumps({"kept": kept}), encoding="utf-8")
    await run_batch([("a.json", scenario())], agent, bank, out, turn_settings=FAST)
    assert json.loads((out / "run-003.json").read_text(encoding="utf-8")) == {
        "kept": "run-003.json"
    }
    assert (out / "run-004.json").exists()
    with pytest.raises(FileExistsError):
        write_new_json(out / "run-004.json", {})


# --- F8. nothing from the call reaches a shell --------------------------------------------


async def test_a_command_is_not_given_to_a_shell() -> None:
    printed = await run_command(
        f'"{PY}" -c "import sys; print(sys.argv[1:])" a && echo INJECTED', {}, 30
    )
    # A shell would run the second command. Here it is three more arguments.
    assert printed.strip() == "['a', '&&', 'echo', 'INJECTED']"


async def test_a_call_reference_that_is_not_a_plain_id_never_reaches_a_command(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "ran"
    script = f"import pathlib; pathlib.Path(r'{marker}').write_text('x'); print('{{}}')"
    hostile = ReadBackContext("a.json", "a", 1, "abc & echo INJECTED-BY-CALL-REF", {})
    with pytest.raises(ReadBackFailed, match="not safe"):
        await CommandReadBack([PY, "-c", script]).read(hostile)

    hooks = ShellHooks(after=f'"{PY}" -c "{script}"')  # names no placeholder at all
    call = CallInfo("abc & echo INJECTED", "run-001.json", "out", "passed")
    with pytest.raises(RuntimeError, match="not safe"):
        await hooks.after_call("a.json", scenario(), 1, call)
    assert not marker.exists()

    with pytest.raises(ExternalFailed, match="no command"):
        await run_command("", {}, 5)


# --- F10. a scenario that lost every graded call ------------------------------------------


def test_a_scenario_that_lost_all_its_graded_calls_is_a_regression(tmp_path: Path) -> None:
    before = Baseline(created="", source="", scenarios={"lookup": [2, 2], "other": [1, 1]})
    now = Summary(by_scenario={"lookup": [0, 0], "other": [1, 1]})
    changes = {c.name: c for c in compare(before, now)}
    assert changes["lookup"].regressed and "no graded calls now" in changes["lookup"].verdict
    assert not changes["other"].regressed

    old = tmp_path / "baseline.json"
    old.write_text(json.dumps({"version": 1, "scenarios": {"lookup": [2, 2]}}), encoding="utf-8")
    with pytest.raises(ValueError, match="older grading rule"):
        load_baseline(old)


# --- G2. excluded clips, wherever they are played ------------------------------------------


def exclude(directory: Path, clip: str) -> None:
    overrides = {"clips": {clip: {"exclude": True, "reason": "wrong words", "date": "2026-11-25"}}}
    (directory / "overrides.json").write_text(json.dumps(overrides), encoding="utf-8")


async def test_an_excluded_fallback_clip_holds_the_call_back_before_it_is_placed(
    tmp_path: Path,
) -> None:
    directory = write_clips(tmp_path / "bank")
    exclude(directory, "sorry")
    bank = ClipBank(directory)
    with_fallback = scenario(fallback={"clips": ["sorry.wav"]})

    result = await run_batch(
        [("a.json", with_fallback)], agent, bank, tmp_path / "runs", turn_settings=FAST
    )
    assert result.ran == 0 and "excluded clips" in result.blocked[0].reason

    (tmp_path / "set").mkdir()
    raw = {
        "name": "a call",
        "mutates": "none",
        "steps": [{"when": "help", "clips": ["opener.wav"]}],
        "fallback": {"clips": ["sorry.wav"]},
    }
    (tmp_path / "set" / "a.json").write_text(json.dumps(raw), encoding="utf-8")
    row = validate_directory(tmp_path / "set", bank, None)[0]
    assert row.clips_state == "missing" and "sorry" in row.missing_clips[0]


async def test_an_excluded_clip_in_a_chain_call_stops_the_chain_before_its_first_call(
    tmp_path: Path,
) -> None:
    directory = write_clips(tmp_path / "bank")
    exclude(directory, "dob")
    result = await run_batch(
        [],
        agents([[BOOKED], [CANCELLED]]),
        ClipBank(directory),
        tmp_path / "runs",
        chains=[("c.json", parse_chain(chain_data()))],
        allow_mutating="all",
        turn_settings=FAST,
    )
    assert result.ran == 0  # not placed and broken half-way, as it used to be
    assert all("excluded clips" in blocked.reason for blocked in result.blocked)
    stored = json.loads((tmp_path / "runs" / "chain-001.json").read_text(encoding="utf-8"))
    assert stored["outcome"] == "not run"


# --- G3. a request with no question mark ---------------------------------------------------


def test_a_request_is_a_request_without_a_question_mark() -> None:
    assert asks_something("Please could you tell me your date of birth, including the year.")
    assert asks_something("Please tell me your shoe size.")
    assert asks_something("I'll need your postcode.")
    assert not asks_something("I'm sorry to hear that.")
    assert not asks_something("Thank you. I have your date of birth.")


async def test_a_request_the_caller_cannot_answer_is_recorded_as_unanswered(bank: ClipBank) -> None:
    leg = FakeLeg([AgentLine("Please tell me your shoe size.", patience_s=0.8)])
    done = await Director(scenario(), leg, bank, turn_settings=FAST).run()
    assert done.unanswered and "shoe size" in done.unanswered[0]
    assert grade(scenario(), done, None).outcome == "void"


# --- G4. answering only what was asked ------------------------------------------------------

ACKNOWLEDGES_THEN_ASKS = (
    "Thanks, I have your date of birth. Could I take your postcode and a mobile number?"
)


def test_a_fact_the_agent_only_acknowledged_is_not_being_asked_for() -> None:
    text = ACKNOWLEDGES_THEN_ASKS
    assert not asked_at(text, text.index("date of birth"))
    assert asked_at(text, text.index("postcode"))
    assert asked_at(text, text.index("mobile number"))
    assert asked_at("Can I have your date of birth?", len("Can I have your "))
    assert not asked_at(
        "I have your postcode, and what is your date of birth?", len("I have your ")
    )


async def test_the_caller_answers_the_two_things_asked_not_the_one_acknowledged(
    bank: ClipBank,
) -> None:
    leg = FakeLeg([AgentLine(ACKNOWLEDGES_THEN_ASKS, patience_s=3.0), AgentLine("Goodbye.")])
    done = await Director(scenario(), leg, bank, turn_settings=FAST).run()
    assert [said.field for said in done.said] == ["postcode", "phone"]


async def test_a_step_gives_way_to_one_whose_words_are_being_asked_for(bank: ClipBank) -> None:
    # The date of birth step comes first in the file and is only mentioned here.
    order = scenario(
        steps=[
            {"when": "date of birth", "clips": ["dob.wav"], "field": "date_of_birth"},
            {"when": "post ?code", "clips": ["postcode.wav"], "field": "postcode"},
        ]
    )
    leg = FakeLeg(
        [AgentLine("Thanks, I have your date of birth. What is your postcode?"), AgentLine("Bye.")]
    )
    done = await Director(order, leg, bank, turn_settings=FAST).run()
    assert [said.field for said in done.said] == ["postcode"]


# --- G5. an engine with no flow steps ------------------------------------------------------


def test_a_finished_trace_with_no_tool_calls_can_be_graded() -> None:
    spoke = record(timeline=[{"t": 0.1, "kind": "agent", "text": "How can I help?"}])
    assert looks_unfinished(Trace("c1"), spoke)  # nothing says the record is complete
    assert not looks_unfinished(Trace("c1", final=True), spoke)

    said = {"messages": [{"role": "bot", "message": "How can I help?", "secondsFromStart": 0.4}]}
    ended = parse_call(
        {"id": "c1", "status": "ended", "endedReason": "customer-ended-call", "artifact": said}
    )
    assert ended.final and not ended.tool_calls and not ended.step_path
    assert not parse_call({"id": "c1", "status": "in-progress", "artifact": said}).final

    must_not_book = scenario(expect={"tools": [{"name": "book", "called": False}]})
    assert grade(must_not_book, spoke, ended).outcome == "passed"


# --- G6. times with no offset, and a window across midnight ---------------------------------


def test_a_time_with_no_offset_is_read_in_the_zone_the_target_writes_it_in() -> None:
    london = "Europe/London"
    # Summer: a database that stores UTC is an hour behind the clock on the wall.
    assert parse_when("2026-07-01 08:30:00", london, "UTC")[1:] == ("wednesday", 9 * 60 + 30)
    assert parse_when("2026-06-30 23:30:00", london, "UTC")[1:] == ("wednesday", 30)
    # Winter: the two agree.
    assert parse_when("2026-12-02 08:30:00", london, "UTC")[1:] == ("wednesday", 8 * 60 + 30)
    # Left unsaid, a time with no offset is already in the scenario's own zone.
    assert parse_when("2026-07-01 08:30:00", london)[1:] == ("wednesday", 8 * 60 + 30)

    stored_utc = booking_scenario(tool="book", naive_times="UTC", start_keys=["start"])
    late = ToolCall("book", {"start": "2026-06-30 23:30:00"}, {}, True)  # 00:30 Wednesday in London
    assert grade(stored_utc, record(), trace(late)).outcome == "passed"
    as_written = booking_scenario(tool="book", start_keys=["start"])
    assert grade(as_written, record(), trace(late)).outcome == "failed"  # a Tuesday as written

    with pytest.raises(ScenarioError, match="no such time zone"):
        booking_scenario(naive_times="Mars/Olympus")


def test_a_time_window_across_midnight_is_not_an_empty_window() -> None:
    night = scenario(
        mutates="books",
        expect={
            "tools": [{"name": "book"}],
            "booking": {"tool": "book", "time_from": "22:00", "time_to": "02:00"},
        },
    )

    def at(start: str) -> Trace:
        return trace(ToolCall("book", {"startTime": start}, {}, True))

    assert grade(night, record(), at("2026-12-02T23:30:00+00:00")).outcome == "passed"
    assert grade(night, record(), at("2026-12-03T01:15:00+00:00")).outcome == "passed"
    assert grade(night, record(), at("2026-12-02T12:00:00+00:00")).outcome == "failed"


# --- G7. a scenario with no line to hang up on ----------------------------------------------


def test_a_call_the_rig_went_silent_on_is_void_even_with_no_hangup_line() -> None:
    open_ended = scenario(hangup_after=None, expect={"tools": [{"name": "book"}]})
    silent = record("agent ended the call", unanswered=["What is your shoe size?"])
    result = grade(open_ended, silent, trace(CHECK))  # the booking never happened
    assert (result.outcome, result.fault) == ("void", "rig")
    # With nothing unanswered the same call is the agent's failure.
    assert grade(open_ended, record("agent ended the call"), trace(CHECK)).fault == "agent"


# --- the time zone database -----------------------------------------------------------------


def test_the_time_zone_database_is_installed_and_its_absence_is_said_plainly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fails here, by name, on a machine with no zone database (Windows without tzdata).
    assert zones.time_zone("Europe/London").key == "Europe/London"
    with pytest.raises(zones.UnknownZone):
        zones.time_zone("Mars/Olympus")

    zones.time_zone.cache_clear()
    monkeypatch.setattr(zones, "ZoneInfo", lambda name: (_ for _ in ()).throw(KeyError(name)))
    monkeypatch.setattr(zones, "database_present", lambda: False)
    with pytest.raises(zones.NoZoneDatabase, match="tzdata"):
        zones.time_zone("Europe/London")
    zones.time_zone.cache_clear()


# --- a rehearsal against a scripted agent ----------------------------------------------------


def test_a_batch_can_be_rehearsed_against_a_scripted_agent_and_says_so_everywhere(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bank = write_clips(tmp_path / "bank")
    (tmp_path / "set").mkdir()
    raw = {
        "name": "lookup",
        "mutates": "none",
        "persona": {"facts": {"date_of_birth": "2002-02-20"}},
        "steps": [
            {"when": "how can i help", "clips": ["opener.wav"]},
            {"when": "date of birth", "clips": ["dob.wav"], "field": "date_of_birth"},
        ],
        "hangup_after": "goodbye",
        "max_seconds": 20,
        "expect": {"tools": [{"name_contains": "check"}], "fields": ["date_of_birth"]},
    }
    (tmp_path / "set" / "lookup.json").write_text(json.dumps(raw), encoding="utf-8")
    script = {
        "lines": [
            {"text": "Hi, how can I help?", "speech_s": 0.3},
            {"text": "Could I take your date of birth?", "speech_s": 0.3, "hears": "20 Feb 2002"},
            "Thank you, goodbye.",
        ],
        "trace": {
            "tool_calls": [
                {
                    "name": "patient_check",
                    "arguments": {"dateOfBirth": "2002-02-20"},
                    "result": {"success": True},
                    "ok": True,
                }
            ]
        },
    }
    (tmp_path / "agent.json").write_text(json.dumps(script), encoding="utf-8")
    out = tmp_path / "runs"
    arguments = ["batch", str(tmp_path / "set"), "--leg", "fake", "--clips", str(bank)]
    arguments += ["--out", str(out), "--settle", "0"]

    with pytest.raises(SystemExit, match="--fake-agent"):
        cli.main(arguments)
    assert cli.main([*arguments, "--fake-agent", str(tmp_path / "agent.json")]) == 0

    run = json.loads((out / "run-001.json").read_text(encoding="utf-8"))
    assert run["rehearsal"] is True and run["grade"]["outcome"] == "passed"
    assert json.loads((out / "batch.json").read_text(encoding="utf-8"))["rehearsal"] is True
    assert "REHEARSAL" in capsys.readouterr().out
    assert summarise(out).rehearsals == 1

    # However good it looks, a rehearsal meets no gate.
    for index in range(2, 60):
        write_json(out / f"run-{index:03d}.json", {**run, "grade": {**run["grade"]}})
    assert cli._gate(str(out), 0.95, 0.95) == 1
    assert "rehearsals against a scripted stand-in" in capsys.readouterr().out
