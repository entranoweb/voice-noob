"""Is a pass true, and is a fail true? One test per way the answer used to be wrong."""

import csv
import json
import wave
from pathlib import Path
from typing import Any

import pytest
from conftest import mark_checked

from synthiq_caller.batch import NameBudget, load_scenarios, run_batch
from synthiq_caller.cli import _trace_reader, main
from synthiq_caller.clips import ClipBank
from synthiq_caller.director import Director, RunRecord, Said
from synthiq_caller.grading import grade, parse_when
from synthiq_caller.leg import Leg
from synthiq_caller.legs.fake import RATE, AgentLine, FakeLeg, tone
from synthiq_caller.report import render, summarise
from synthiq_caller.scenario import Scenario, ScenarioError, parse_scenario
from synthiq_caller.stats import calls_needed, gate, lower_bound
from synthiq_caller.traces.base import ToolCall, Trace, TraceReader
from synthiq_caller.traces.common import missing_result
from synthiq_caller.traces.retell import RetellTraceReader
from synthiq_caller.turns import AgentTurnTracker, TurnSettings

FAST = TurnSettings(hangover_s=0.2, audio_start_wait_s=0.6, min_speech_s=0.06)
SOPHIE = Path(__file__).parent.parent / "scenarios" / "sophie"
END = "caller hung up: scenario end"


def write_clips(directory: Path, names: tuple[str, ...]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        with wave.open(str(directory / name), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(RATE)
            wav.writeframes(tone(0.2))


@pytest.fixture
def bank(tmp_path: Path) -> ClipBank:
    directory = tmp_path / "bank"
    write_clips(directory, ("opener.wav", "dob.wav", "postcode.wav", "yes.wav", "again.wav"))
    mark_checked(directory)
    return ClipBank(directory)


def scenario(**extra: Any) -> Scenario:
    data: dict[str, Any] = {
        "name": "a call",
        "mutates": "none",
        "persona": {
            "facts": {
                "name": "Alice Wonderland",
                "date_of_birth": "2002-02-20",
                "postcode": "A56TB",
            }
        },
        "steps": [
            {"when": "how can i help", "clips": ["opener.wav"]},
            {"when": "date of birth", "clips": ["dob.wav"], "field": "date_of_birth"},
            {"when": "post ?code", "clips": ["postcode.wav"], "field": "postcode"},
        ],
        "hangup_after": "goodbye",
        "max_seconds": 20,
        "expect": {"tools": [{"name_contains": "check"}]},
    }
    data.update(extra)
    return parse_scenario(data)


def record(ended: str = END, **extra: Any) -> RunRecord:
    return RunRecord(scenario="a call", call_ref="c1", ended=ended, **extra)


def trace(*calls: ToolCall) -> Trace:
    return Trace(call_ref="c1", tool_calls=list(calls), step_path=["S1"])


CHECK = ToolCall("patient_check", {"dateOfBirth": "2002-02-20"}, {"success": True}, True)


def booked(when: str, kind: str = "consultation") -> list[ToolCall]:
    return [
        ToolCall("new_patient_fee", {"appointmentType": kind}, {"success": True}, True),
        ToolCall(
            "book", {}, {"success": True, "outcome": "BOOKED", "bookedDisplayTime": when}, True
        ),
    ]


def books(**booking: Any) -> Scenario:
    return scenario(
        mutates="books",
        expect={"tools": [{"name": "book"}], "booking": booking}
        if booking
        else {"tools": [{"name": "book"}]},
        tool_outcomes={"book": {"path": "outcome", "success": ["BOOKED"]}},
    )


WEDNESDAY_TEN = "Wednesday the twenty-sixth of November at ten in the morning"
THURSDAY_TEN = "Thursday the twenty-seventh of November at ten in the morning"


# --- 1. a booking passes only when what was booked is what was asked for -----------


def test_a_booking_on_the_wrong_day_fails_and_on_the_right_day_passes() -> None:
    wanted = books(weekdays=["wednesday"], time_from="10:00", time_to="10:00", type="mri",
                   start_keys=["bookedDisplayTime"])  # fmt: skip
    assert grade(wanted, record(), trace(*booked(WEDNESDAY_TEN, "mri"))).outcome == "passed"

    wrong_day = grade(wanted, record(), trace(*booked(THURSDAY_TEN, "mri")))
    assert (wrong_day.outcome, wrong_day.fault) == ("failed", "agent")
    assert any("wednesday" in c.what and not c.passed for c in wrong_day.checks)

    wrong_type = grade(wanted, record(), trace(*booked(WEDNESDAY_TEN, "consultation")))
    assert wrong_type.outcome == "failed"
    wrong_time = grade(
        wanted, record(), trace(*booked(WEDNESDAY_TEN.replace("ten", "half past ten"), "mri"))
    )
    assert wrong_time.outcome == "failed"


def test_a_booking_whose_slot_cannot_be_seen_is_ungradeable_not_passed() -> None:
    wanted = books(weekdays=["wednesday"], start_keys=["bookedDisplayTime"])
    bare = ToolCall("book", {}, {"success": True, "outcome": "BOOKED"}, True)
    result = grade(wanted, record(), trace(bare))
    assert result.outcome == "ungradeable" and "not available" in result.reason


def test_a_scenario_that_books_and_states_nothing_about_the_booking_is_ungradeable() -> None:
    result = grade(books(), record(), trace(*booked(THURSDAY_TEN)))
    assert result.outcome == "ungradeable" and "expect.booking" in result.reason


def test_the_read_back_wins_over_what_the_tool_said() -> None:
    wanted = books(weekdays=["wednesday"], start_keys=["bookedDisplayTime"])
    held = {"appointment_start": "2026-11-27T10:00:00Z"}  # a Thursday, whatever the tool said
    assert grade(wanted, record(), trace(*booked(WEDNESDAY_TEN)), held).outcome == "passed"
    by_record = books(weekdays=["wednesday"])
    result = grade(by_record, record(), trace(*booked(WEDNESDAY_TEN)), held)
    assert result.outcome == "failed" and "read back" in result.checks[-1].what


def test_times_are_read_from_dates_and_from_words() -> None:
    assert parse_when("2026-07-01T09:30:00Z", "Europe/London")[1:] == ("wednesday", 10 * 60 + 30)
    assert parse_when("Monday the first of December at midday", "Europe/London")[1:] == (
        "monday",
        12 * 60,
    )
    assert (
        parse_when("Friday at quarter to five in the afternoon", "Europe/London")[2] == 16 * 60 + 45
    )
    assert parse_when("Friday at four", "Europe/London")[2] is None  # morning or afternoon unknown
    with pytest.raises(ScenarioError):
        books(weekdays=["someday"])


def test_every_shipped_scenario_that_writes_an_appointment_says_what_it_should_be() -> None:
    from synthiq_caller.target import load_target

    # The times belong to the clinic, so they come from its profile.
    original = load_target(SOPHIE / "targets" / "original-clinic.json")
    for file, loaded in load_scenarios(SOPHIE, original):
        if loaded.mutates in ("books", "moves"):
            assert loaded.expect.booking.stated() and not loaded.missing_target, file
        assert loaded.tool_outcomes, file
        if loaded.facts.get("name"):
            assert loaded.identity_budget == 2, file


# --- 2. nothing that was attempted drops out of the count ------------------------


class NobodyAnswers(FakeLeg):
    async def connect(self) -> None:
        raise ConnectionRefusedError("busy")


async def test_an_unanswered_call_is_a_failed_call_not_a_crashed_batch(
    bank: ClipBank, tmp_path: Path
) -> None:
    def make() -> tuple[Leg, TraceReader | None]:
        return NobodyAnswers([]), None

    out = tmp_path / "runs"
    result = await run_batch([("a.json", scenario())], make, bank, out, turn_settings=FAST)
    assert result.ran == 1 and (out / "batch.json").exists()
    stored = json.loads((out / "run-001.json").read_text(encoding="utf-8"))["grade"]
    assert (stored["outcome"], stored["fault"]) == ("failed", "unknown")
    summary = summarise(out)
    assert (summary.failed, summary.faults) == (1, {"unknown": 1})


def test_a_call_that_died_before_its_end_with_no_trace_is_a_failure() -> None:
    died = grade(scenario(), record("agent ended the call script finished"), None)
    assert (died.outcome, died.fault) == ("failed", "unknown")
    # Ended where the scenario ends, and the trace simply could not be read: not known.
    assert grade(scenario(), record(), None).outcome == "ungraded"


def test_a_call_that_ran_to_the_time_limit_cannot_pass() -> None:
    result = grade(scenario(), record("caller hung up: time limit"), trace(CHECK))
    assert (result.outcome, result.fault) == ("failed", "unknown")
    assert grade(scenario(), record(), trace(CHECK)).outcome == "passed"


def test_the_headline_shows_graded_and_attempted_and_lists_what_was_left_out(
    tmp_path: Path,
) -> None:
    def stored(index: int, outcome: str, fault: str = "", reason: str = "") -> None:
        payload = {
            "record": {"timeline": [], "ended": ""},
            "trace": None,
            "grade": {"scenario": f"s{index}", "outcome": outcome, "fault": fault,
                      "reason": reason, "checks": [], "fields": []},
        }  # fmt: skip
        (tmp_path / f"run-{index:03d}.json").write_text(json.dumps(payload), encoding="utf-8")

    stored(1, "passed")
    stored(2, "failed", "unknown", "the call was not answered")
    stored(3, "void", "rig", "the caller had no answer to: 'Surname?'")
    stored(4, "ungradeable", "", "states nothing about the booking")
    summary = summarise(tmp_path)
    assert (summary.runs, summary.passed, summary.failed, summary.void, summary.ungradeable) == (
        4, 1, 1, 1, 1,
    )  # fmt: skip
    text = render(summary)
    assert "of graded:    1 of 2" in text and "of attempted: 1 of 4" in text
    assert "run-003.json  s3: void, fault rig" in text
    assert "run-004.json  s4: ungradeable" in text
    assert "charged to: unknown 1" in text


# --- 3. the rig's own silence is not the agent's failure -------------------------


async def test_a_question_the_scenario_cannot_answer_voids_the_call(bank: ClipBank) -> None:
    leg = FakeLeg(
        [
            AgentLine("Hi, how can I help you today?", speech_s=0.2),
            AgentLine("Could I take your surname first?", speech_s=0.2, patience_s=0.8),
        ]
    )
    run = await Director(scenario(), leg, bank, turn_settings=FAST).run()
    assert run.unanswered == ["Could I take your surname first?"]
    result = grade(scenario(), run, trace())
    assert (result.outcome, result.fault) == ("void", "rig")
    assert "surname" in result.reason


async def test_a_fallback_answers_what_no_step_matches(bank: ClipBank) -> None:
    with_fallback = scenario(
        fallback={"clips": ["again.wav"], "intended": "Sorry, say that again?"}
    )
    leg = FakeLeg(
        [
            AgentLine("Could I take your surname first?", speech_s=0.2),
            AgentLine("Thanks. Goodbye.", speech_s=0.2),
        ]
    )
    run = await Director(with_fallback, leg, bank, turn_settings=FAST).run()
    assert not run.unanswered
    assert [(s.clips, s.fallback) for s in run.said] == [(["again.wav"], True)]


def test_a_forbidden_line_is_still_the_agents_fault_after_a_silence() -> None:
    strict = scenario(
        expect={"tools": [{"name_contains": "check"}], "agent_never_says": ["different name"]}
    )
    spoiled = record(
        "agent ended the call",
        unanswered=["Is that right?"],
        timeline=[{"kind": "agent", "text": "Could you try a different name?"}],
    )
    result = grade(strict, spoiled, trace())
    assert (result.outcome, result.fault) == ("failed", "agent")


# --- 4. a clip nobody has checked is not played as if its label were a fact -------


async def test_unchecked_clips_are_refused_unless_allowed_and_then_marked(tmp_path: Path) -> None:
    directory = tmp_path / "raw"
    write_clips(directory, ("opener.wav", "dob.wav", "postcode.wav"))
    with (directory / "manifest.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["clip", "review", "checked_by_human", "provenance"])
        writer.writerow(["opener", "", "sam", "real"])
        writer.writerow(["dob", "low_energy", "", "real"])
        writer.writerow(["postcode", "", "", "real"])
    raw = ClipBank(directory)
    assert raw.verified("opener") and not raw.verified("dob")
    assert raw.label_doubts(["opener.wav", "dob.wav"]) == [
        "dob.wav: flagged low_energy, not checked by a person"
    ]

    def make() -> tuple[Leg, TraceReader | None]:
        leg = FakeLeg(
            [
                AgentLine("Hi, how can I help you today?", speech_s=0.2),
                AgentLine("Your date of birth?", speech_s=0.2, hears="20th February 2002"),
                AgentLine("Goodbye.", speech_s=0.2),
            ]
        )
        return leg, None

    refused = await run_batch(
        [("a.json", scenario())], make, raw, tmp_path / "no", turn_settings=FAST
    )
    assert refused.ran == 0 and "not confirmed by a person" in refused.blocked[0].reason

    out = tmp_path / "yes"
    allowed = await run_batch(
        [("a.json", scenario())], make, raw, out, turn_settings=FAST, allow_unverified_clips=True
    )
    assert allowed.ran == 1
    payload = json.loads((out / "run-001.json").read_text(encoding="utf-8"))
    row = payload["grade"]["fields"][0]
    assert row["labels_unverified"] and "labels unverified" in row["verdict"]
    assert "labels unverified" in render(summarise(out))


# --- 5. the phone leg does not wait for speech it has already heard --------------


def test_text_that_trails_the_audio_does_not_add_a_fixed_wait() -> None:
    speech = tone(0.5)

    def answered_at(follows: bool) -> float:
        tracker = AgentTurnTracker(TurnSettings(text_follows_audio=follows))
        tracker.feed(speech, RATE, 0.0)  # the agent speaks from 0.0 to 0.5
        tracker.mark(1.2)  # its words arrive, transcribed, at 1.2
        return next(t / 10 for t in range(12, 60) if tracker.turn_finished(t / 10))

    assert answered_at(False) == pytest.approx(3.7, abs=0.11)  # 1.2 + the fixed 2.5 s
    assert answered_at(True) == pytest.approx(1.2, abs=0.11)  # the speech was the line


def test_speech_from_before_the_callers_answer_is_not_taken_for_the_next_line() -> None:
    tracker = AgentTurnTracker(TurnSettings(text_follows_audio=True))
    tracker.feed(tone(0.5), RATE, 0.0)  # an earlier line
    tracker.answered()  # the caller answers it
    tracker.mark(1.2)  # words arrive for a line whose audio has not come yet
    assert not tracker.turn_finished(1.5)  # so the caller waits for it, as before


# --- 6. the identity budget matches the target's own lock ------------------------


async def test_a_scenario_spends_what_it_says_and_a_second_run_waits_or_is_refused(
    bank: ClipBank, tmp_path: Path
) -> None:
    two_attempts = scenario(identity_budget=2, identity_attempts=2)

    def make() -> tuple[Leg, TraceReader | None]:
        return FakeLeg([AgentLine("Goodbye.", speech_s=0.2)]), None

    refused = await run_batch(
        [("a.json", two_attempts)], make, bank, tmp_path / "r", repeats=2,
        budget=NameBudget(limit=6), turn_settings=FAST,
    )  # fmt: skip
    assert refused.ran == 1 and "name budget" in refused.blocked[0].reason

    now = [0.0]
    waits: list[float] = []

    async def wait(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds

    spaced = await run_batch(
        [("a.json", two_attempts)], make, bank, tmp_path / "s", repeats=2,
        budget=NameBudget(limit=6, clock=lambda: now[0]), turn_settings=FAST,
        space_names=True, wait=wait,
    )  # fmt: skip
    assert spaced.ran == 2 and not spaced.blocked
    assert any(w > 900 for w in waits)


def test_the_budget_can_be_kept_between_batches(tmp_path: Path) -> None:
    file = tmp_path / "budget.json"
    now = [1000.0]
    first = NameBudget(path=file, clock=lambda: now[0])
    assert first.take("Claire Collins", cost=2)
    again = NameBudget(path=file, clock=lambda: now[0])
    assert not again.take("claire collins")
    assert again.wait_s("Claire Collins") == pytest.approx(900.0)
    assert again.wait_s("Claire Collins", cost=3) is None  # more than the target ever allows


# --- 7. the arithmetic of a gate -------------------------------------------------


def test_the_gate_says_how_many_calls_a_claim_needs() -> None:
    assert [calls_needed(0.95, 0.95, f) for f in (0, 1, 2)] == [59, 93, 124]
    assert lower_bound(58, 60) == pytest.approx(0.899, abs=0.001)
    met, shown, said = gate(58, 60)
    assert not met and "NOT met" in said and "124" in said
    assert gate(59, 59)[0] and gate(122, 124)[0]
    assert not gate(0, 0)[0]


def test_repeats_are_reported_by_scenario_as_well_as_pooled(tmp_path: Path) -> None:
    index = 0
    for name, outcomes in (("one", "ppp"), ("two", "ppf"), ("three", "ppp")):
        for letter in outcomes:
            index += 1
            payload = {
                "record": {"timeline": [], "ended": ""},
                "trace": None,
                "grade": {"scenario": name, "outcome": "passed" if letter == "p" else "failed",
                          "fault": "agent", "checks": [], "fields": []},
            }  # fmt: skip
            (tmp_path / f"run-{index:03d}.json").write_text(json.dumps(payload), encoding="utf-8")
    text = render(summarise(tmp_path))
    assert "of graded:    8 of 9" in text
    assert "2 of 3 scenarios passed every call placed" in text and "3 passed at least once" in text
    assert "not independent" in text
    assert main(["gate", str(tmp_path)]) == 1


# --- 8. one scenario goes through the same guard as a batch ----------------------


def test_the_run_command_will_not_book_without_being_told_it_may(
    bank: ClipBank, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = tmp_path / "books.json"
    file.write_text(
        json.dumps(
            {
                "name": "books",
                "mutates": "books",
                "steps": [{"when": "help", "clips": ["opener.wav"]}],
                "expect": {"tools": [{"name": "book"}], "booking": {"type": "mri"}},
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out"
    main(["run", str(file), "--leg", "elevenlabs-socket", "--agent", "x",
          "--clips", str(bank.directory), "--out", str(out)])  # fmt: skip
    assert not list(out.glob("run-*.json"))
    assert "mutation guard" in capsys.readouterr().out


# --- 9. a result the platform withheld proves nothing either way ------------------


def test_a_withheld_result_does_not_satisfy_an_expected_refusal() -> None:
    expects_refusal = scenario(expect={"tools": [{"name_contains": "book", "ok": False}]})
    withheld = missing_result("book_slot", {}, None, "")
    result = grade(expects_refusal, record(), trace(withheld))
    assert result.outcome == "ungradeable" and "withheld" in result.reason
    refused = ToolCall("book_slot", {}, {"error": "taken"}, False)
    assert grade(expects_refusal, record(), trace(refused)).outcome == "passed"


# --- 10. the other trace readers can be reached ----------------------------------


def test_the_trace_source_is_chosen_on_the_command_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import argparse

    monkeypatch.setenv("RETELL_API_KEY", "k")
    reader = _trace_reader(argparse.Namespace(trace="retell", env_file=None))
    assert isinstance(reader, RetellTraceReader)
    with pytest.raises(SystemExit):
        main(["batch", str(tmp_path), "--leg", "elevenlabs-socket", "--clips", ".", "--out", ".",
              "--trace", "somewhere-else"])  # fmt: skip


# --- 11. a tool's outcome is read from its reply, not the transport ---------------


def test_an_identity_check_that_ran_and_did_not_match_is_not_a_success() -> None:
    verifies = scenario(
        expect={"tools": [{"name": "verify_identity"}]},
        tool_outcomes={"verify_identity": {"path": "verified"}},
    )
    no_match = ToolCall("verify_identity", {}, {"success": True, "verified": False}, True)
    match = ToolCall("verify_identity", {}, {"success": True, "verified": True}, True)
    bare = ToolCall("verify_identity", {}, {"success": True}, True)
    assert grade(verifies, record(), trace(no_match)).outcome == "failed"
    assert grade(verifies, record(), trace(no_match, match)).outcome == "passed"
    unknown = grade(verifies, record(), trace(bare))
    assert unknown.outcome == "ungradeable" and "does not carry 'verified'" in unknown.reason
    # A whole name does not catch a longer tool that merely contains it.
    other = ToolCall("prepare_verify_identity", {}, {"verified": True}, True)
    assert grade(verifies, record(), trace(other)).outcome == "failed"


# --- 12. sent to a tool is not the same as stored ---------------------------------


def test_a_value_only_seen_in_a_tool_call_is_sent_not_stored(tmp_path: Path) -> None:
    wants = scenario(expect={"tools": [{"name_contains": "check"}], "fields": ["date_of_birth"]})
    said = Said(1.0, 1, "date_of_birth", "20th February 2002", ["dob.wav"], "", 0.2, False, "dob?")
    sent = grade(wants, record(said=[said]), trace(CHECK))
    assert sent.fields[0].verdict == "sent correctly, not confirmed stored"
    kept = grade(wants, record(said=[said]), trace(CHECK), {"date_of_birth": "2002-02-20"})
    assert kept.fields[0].verdict == "stored correctly"
    for index, graded in enumerate((sent, kept), start=1):
        payload = {
            "record": {"timeline": [], "ended": END},
            "trace": None,
            "grade": graded.to_json(),
        }
        (tmp_path / f"run-{index:03d}.json").write_text(json.dumps(payload), encoding="utf-8")
    tally = summarise(tmp_path).fields["date_of_birth"]
    assert (tally.stored_correctly, tally.sent_correctly) == (1, 1)
    assert "sent, not confirmed stored" in render(summarise(tmp_path))


# --- 13. one turn can ask for two things ------------------------------------------


async def test_a_combined_question_gets_both_answers_in_the_order_asked(bank: ClipBank) -> None:
    leg = FakeLeg(
        [
            AgentLine("Could I take your postcode and your date of birth?", speech_s=0.2),
            AgentLine("Thanks. Goodbye.", speech_s=0.2),
        ]
    )
    run = await Director(scenario(), leg, bank, turn_settings=FAST).run()
    assert [s.field for s in run.said] == ["postcode", "date_of_birth"]


async def test_a_read_back_that_mentions_a_fact_is_answered_once(bank: ClipBank) -> None:
    confirms = scenario(
        steps=[
            {"when": "is that right", "clips": ["yes.wav"]},
            {"when": "date of birth", "clips": ["dob.wav"], "field": "date_of_birth"},
            {"when": "post ?code", "clips": ["postcode.wav"], "field": "postcode"},
        ]
    )
    leg = FakeLeg(
        [
            AgentLine(
                "Your date of birth is the 20th and your postcode A5 6TB, is that right?",
                speech_s=0.2,
            ),  # fmt: skip
            AgentLine("Thanks. Goodbye.", speech_s=0.2),
        ]
    )
    run = await Director(confirms, leg, bank, turn_settings=FAST).run()
    assert [s.clips for s in run.said] == [["yes.wav"]]


# --- 14. a synthetic stand-in is never passed off as a real caller ----------------


async def test_a_second_bank_supplies_a_synthetic_clip_and_every_result_says_so(
    bank: ClipBank, tmp_path: Path
) -> None:
    stand_ins = tmp_path / "synthetic"
    write_clips(stand_ins, ("no_preference.wav",))
    with (stand_ins / "manifest.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["clip", "said", "provenance"])
        writer.writerow(["no_preference", "I don't mind.", "synthetic stock voice"])
    both = ClipBank(bank.directory, extra=(stand_ins,))
    assert both.synthetic("no_preference") and not both.synthetic("dob")
    assert both.verified("no_preference")  # made from its text, so its label is known
    with pytest.raises(FileNotFoundError):
        bank.path("no_preference")

    asks = scenario(
        steps=[
            {"when": "which doctor", "clips": ["no_preference"], "field": "name"},
            {"when": "date of birth", "clips": ["dob.wav"], "field": "date_of_birth"},
        ]
    )

    def make() -> tuple[Leg, TraceReader | None]:
        leg = FakeLeg(
            [
                AgentLine("Which doctor would you like to see?", speech_s=0.2),
                AgentLine("And your date of birth?", speech_s=0.2),
                AgentLine("Goodbye.", speech_s=0.2),
            ]
        )
        return leg, None

    out = tmp_path / "runs"
    await run_batch([("a.json", asks)], make, both, out, turn_settings=FAST)
    payload = json.loads((out / "run-001.json").read_text(encoding="utf-8"))
    assert payload["synthetic_clips"] == ["no_preference"]
    rows = {row["field"]: row for row in payload["grade"]["fields"]}
    assert rows["name"]["synthetic"] and "synthetic voice" in rows["name"]["verdict"]
    assert not rows["date_of_birth"]["synthetic"]
    assert "synthetic voice: 1 call(s)" in render(summarise(out))


def test_the_booking_scenarios_answer_which_doctor_with_the_stand_in() -> None:
    for file, loaded in load_scenarios(SOPHIE):
        if file[:2] in ("01", "02", "03", "04", "05", "06", "07"):
            step = next(
                s for s in loaded.steps if s.when.search("Which doctor would you like to see?")
            )
            assert step.clips == ("practitioner_no_preference",), file
            assert "synthetic" in loaded.description, file


# --- what a tool reported, and what the system must already hold ------------------


def test_a_tools_outcome_is_read_from_what_it_reported_when_the_result_is_bare() -> None:
    verifies = scenario(
        expect={"tools": [{"name": "verify_identity", "outputs": {"verified": "true"}}]},
        tool_outcomes={"verify_identity": {"path": "verified"}},
    )
    bare = {"success": True}
    no_match = ToolCall("verify_identity", {}, bare, True, outputs={"verified": False})
    match = ToolCall("verify_identity", {}, bare, True, outputs={"verified": True})
    silent = ToolCall("verify_identity", {}, bare, True)
    assert grade(verifies, record(), trace(no_match)).outcome == "failed"
    assert grade(verifies, record(), trace(no_match, match)).outcome == "passed"
    unknown = grade(verifies, record(), trace(silent))
    assert unknown.outcome == "ungradeable" and "does not carry" in unknown.reason


def test_a_booked_time_is_found_in_what_the_tool_reported() -> None:
    wanted = books(weekdays=["wednesday"], start_keys=["bookedDisplayTime"])
    wanted = parse_scenario(
        {
            "name": "a call",
            "mutates": "books",
            "steps": [{"when": "help", "clips": ["opener.wav"]}],
            "expect": {
                "tools": [{"name": "book"}],
                "booking": {"weekdays": ["wednesday"], "start_keys": ["bookedDisplayTime"]},
            },
            "tool_outcomes": {"book": {"path": "bookOutcome", "success": ["BOOKED"]}},
        }
    )
    reported = {"bookOutcome": "BOOKED", "bookedDisplayTime": THURSDAY_TEN}
    call = ToolCall("book", {}, {"success": True}, True, outputs=reported)
    result = grade(wanted, record(ended="agent ended the call"), trace(call))
    assert result.outcome == "failed" and "Thursday" in result.checks[-1].what


class Unmet:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def check(self, file: str, name: str, requires: str) -> Any:
        from synthiq_caller.prereq import PrereqResult

        self.asked.append(file)
        return PrereqResult("unmet", "Mark Marshall is not on file")


async def test_a_scenario_whose_prerequisite_is_unmet_is_not_run(
    bank: ClipBank, tmp_path: Path
) -> None:
    made: list[int] = []

    def make() -> tuple[Leg, TraceReader | None]:
        made.append(1)
        return FakeLeg([AgentLine("Goodbye.", speech_s=0.2)]), None

    checker = Unmet()
    needs = scenario(requires="Mark Marshall is on file")
    free = scenario(name="needs nothing", persona={"facts": {"name": "Ben Bromley"}})
    result = await run_batch(
        [("needs.json", needs), ("free.json", free)], make, bank, tmp_path / "runs",
        turn_settings=FAST, prereq=checker,
    )  # fmt: skip
    assert result.ran == 1 and len(made) == 1 and checker.asked == ["needs.json"]
    assert result.blocked[0].reason == "prerequisite unmet: Mark Marshall is not on file"
