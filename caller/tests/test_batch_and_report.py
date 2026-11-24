"""The batch guards, the report arithmetic, and a small batch against the scripted agent."""

import json
import wave
from pathlib import Path
from typing import Any

import pytest
from conftest import mark_checked

from synthiq_caller.batch import NameBudget, ShellHooks, load_scenarios, regrade, run_batch
from synthiq_caller.clips import ClipBank
from synthiq_caller.leg import Leg
from synthiq_caller.legs.fake import RATE, AgentLine, FakeLeg, tone
from synthiq_caller.report import render, summarise
from synthiq_caller.scenario import Scenario, ScenarioError, parse_scenario
from synthiq_caller.traces.base import ToolCall, Trace, TraceReader
from synthiq_caller.turns import TurnSettings

FAST = TurnSettings(hangover_s=0.2, audio_start_wait_s=0.6, min_speech_s=0.06)
SCENARIOS = Path(__file__).parent.parent / "scenarios" / "sophie"


def scenario(
    name: str, mutates: str = "none", caller: str = "Alice Wonderland", **extra: Any
) -> Scenario:
    data: dict[str, Any] = {
        "name": name,
        "mutates": mutates,
        "persona": {"facts": {"name": caller, "date_of_birth": "2002-02-20"}},
        "steps": [
            {"when": "how can i help", "clips": ["opener.wav"]},
            {"when": "date of birth", "clips": ["dob.wav"], "field": "date_of_birth"},
        ],
        "hangup_after": "mobile number",
        "max_seconds": 20,
        "expect": {"tools": [{"name_contains": "new_patient_check"}], "fields": ["date_of_birth"]},
    }
    data.update(extra)
    return parse_scenario(data)


@pytest.fixture
def bank(tmp_path: Path) -> ClipBank:
    directory = tmp_path / "bank"
    directory.mkdir()
    for name in ("opener.wav", "dob.wav"):
        with wave.open(str(directory / name), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(RATE)
            wav.writeframes(tone(0.25))
    mark_checked(directory)
    return ClipBank(directory)


class FixedTrace:
    """A trace reader that reports one tool call with the date it is told to."""

    def __init__(self, stored: str) -> None:
        self.stored = stored

    async def read(self, call_ref: str) -> Trace:
        call = ToolCall("sophie_v2_new_patient_check", {"dateOfBirth": self.stored}, {}, True, "NP")
        return Trace(call_ref=call_ref, tool_calls=[call], step_path=["I1", "NP_CHECK"])


def agent(stored: str = "20th of February, 2002") -> tuple[Leg, TraceReader | None]:
    leg = FakeLeg(
        [
            AgentLine("Hi, how can I help you today?", speech_s=0.3),
            AgentLine("Could I take your date of birth?", speech_s=0.3, hears=stored),
            AgentLine("And the best mobile number?", speech_s=0.3),
        ]
    )
    return leg, FixedTrace(stored)


# --- the guards -----------------------------------------------------------------


def test_a_scenario_that_does_not_say_what_it_changes_is_treated_as_dangerous() -> None:
    data = {"name": "x", "steps": [{"when": "a", "clips": ["a.wav"]}]}
    assert parse_scenario(data).mutates == "unknown"
    with pytest.raises(ScenarioError):
        parse_scenario({**data, "mutates": "probably fine"})


async def test_the_default_batch_runs_nothing_that_changes_the_target(
    bank: ClipBank, tmp_path: Path
) -> None:
    made: list[int] = []

    def make() -> tuple[Leg, TraceReader | None]:
        made.append(1)
        return agent()

    scenarios = [
        ("books.json", scenario("books a slot", "books")),
        ("registers.json", scenario("registers", "registers")),
        ("unsaid.json", scenario("does not say", "unknown")),
        ("lookup.json", scenario("lookup only", "none")),
    ]
    result = await run_batch(scenarios, make, bank, tmp_path / "runs", turn_settings=FAST)

    assert result.ran == 1 and len(made) == 1
    assert sorted(b.scenario_file for b in result.blocked) == [
        "books.json",
        "registers.json",
        "unsaid.json",
    ]
    assert all("mutation guard" in b.reason for b in result.blocked)


async def test_each_level_lets_through_only_what_it_names(bank: ClipBank, tmp_path: Path) -> None:
    scenarios = [
        ("a.json", scenario("registers", "registers", caller="A One")),
        ("b.json", scenario("books", "books", caller="B Two")),
        ("c.json", scenario("moves", "moves", caller="C Three")),
    ]
    registers = await run_batch(
        scenarios, agent, bank, tmp_path / "r", allow_mutating="registers", turn_settings=FAST
    )
    assert registers.ran == 1
    everything = await run_batch(
        scenarios, agent, bank, tmp_path / "all", allow_mutating="all", turn_settings=FAST
    )
    assert everything.ran == 3 and not everything.blocked


async def test_a_scenario_with_a_missing_recording_is_not_run(
    bank: ClipBank, tmp_path: Path
) -> None:
    needs = scenario("needs a recording", missing_clips=["an out-of-scope reason"])
    result = await run_batch(
        [("n.json", needs)], agent, bank, tmp_path / "runs", turn_settings=FAST
    )
    assert result.ran == 0
    assert "missing clips" in result.blocked[0].reason


def test_the_name_budget_refuses_the_call_past_the_limit_and_recovers_with_time() -> None:
    now = [0.0]
    budget = NameBudget(limit=2, window_s=900, clock=lambda: now[0])
    assert budget.take("Alice Wonderland")
    assert budget.take("alice  wonderland")  # same person, however it is typed
    assert not budget.take("Alice Wonderland")
    assert budget.take("Ben Bromley")  # a different name has its own allowance
    now[0] = 901.0
    assert budget.take("Alice Wonderland")


def test_the_name_budget_can_be_switched_off() -> None:
    budget = NameBudget(limit=0)
    assert all(budget.take("Alice Wonderland") for _ in range(20))


async def test_a_batch_stops_calling_a_name_that_is_out_of_budget(
    bank: ClipBank, tmp_path: Path
) -> None:
    result = await run_batch(
        [("lookup.json", scenario("lookup only"))],
        agent,
        bank,
        tmp_path / "runs",
        repeats=3,
        budget=NameBudget(limit=2),
        turn_settings=FAST,
    )
    assert result.ran == 2
    assert len(result.blocked) == 1 and "name budget" in result.blocked[0].reason


async def test_a_failed_reset_stops_that_call_and_is_reported(
    bank: ClipBank, tmp_path: Path
) -> None:
    hooks = ShellHooks(before='python -c "import sys; sys.exit(3)"')
    result = await run_batch(
        [("lookup.json", scenario("lookup only"))],
        agent,
        bank,
        tmp_path / "runs",
        hooks=hooks,
        turn_settings=FAST,
    )
    assert result.ran == 0
    assert "before hook" in result.blocked[0].reason


async def test_hooks_are_told_which_scenario_they_are_resetting_for(
    bank: ClipBank, tmp_path: Path
) -> None:
    seen = tmp_path / "seen.txt"
    command = (
        "python -c \"import os; open(os.environ['SEEN'], 'a').write("
        "os.environ['SYNTHIQ_PHASE'] + ':' + os.environ['SYNTHIQ_MUTATES'] + ' ')\""
    )
    import os

    os.environ["SEEN"] = str(seen)
    try:
        await run_batch(
            [("lookup.json", scenario("lookup only"))],
            agent,
            bank,
            tmp_path / "runs",
            hooks=ShellHooks(before=command, after=command),
            turn_settings=FAST,
        )
    finally:
        del os.environ["SEEN"]
    assert seen.read_text().split() == ["before:none", "after:none"]


# --- the batch itself and its report ---------------------------------------------


async def test_a_batch_over_the_scripted_agent_grades_and_reports(
    bank: ClipBank, tmp_path: Path
) -> None:
    out = tmp_path / "runs"
    stored = iter(["20th of February, 2002", "26th of February, 2002"])

    def make() -> tuple[Leg, TraceReader | None]:
        return agent(next(stored))

    scenarios = [
        ("good.json", scenario("heard right", caller="Alice Wonderland")),
        ("bad.json", scenario("heard wrong", caller="Ben Bromley")),
        ("books.json", scenario("books a slot", "books", caller="Claire Collins")),
    ]
    result = await run_batch(scenarios, make, bank, out, turn_settings=FAST)

    assert result.ran == 2
    summary = summarise(out)
    assert (summary.passed, summary.failed, summary.ungraded) == (1, 1, 0)
    assert summary.by_scenario == {"heard right": [1, 1], "heard wrong": [0, 1]}
    tally = summary.fields["date_of_birth"]
    # With no read-back, the right value is "sent", not "stored".
    assert (tally.answered, tally.sent_correctly, tally.misheard, tally.mis_stored) == (
        2,
        1,
        1,
        1,
    )
    assert tally.stored_correctly == 0
    failure = summary.failures[0]
    assert failure.scenario == "heard wrong"
    assert "NP_CHECK" in failure.failing_step
    assert failure.checks == ["'date_of_birth' reaches a tool as the caller said it"]
    assert len(summary.blocked) == 1

    text = render(summary)
    assert "passed: 1 of 2 (50%; 95% interval" in text
    assert "mutation guard" in text
    assert "date_of_birth" in text


def test_a_refused_tool_is_named_as_the_failing_step(tmp_path: Path) -> None:
    payload = {
        "record": {"timeline": [{"kind": "agent", "text": "That didn't go through."}], "ended": ""},
        "trace": {
            "step_path": ["BK_PREP", "MB", "SAY_BOOK_FAILED"],
            "tool_calls": [
                {"name": "prepare_book", "ok": True, "step_id": "BK_PREP"},
                {"name": "book", "ok": False, "step_id": "MB"},
            ],
        },
        "grade": {
            "scenario": "books a slot",
            "outcome": "failed",
            "checks": [{"what": "tool 'book' is called and succeeds", "passed": False}],
            "fields": [],
        },
    }
    (tmp_path / "run-001.json").write_text(json.dumps(payload), encoding="utf-8")
    summary = summarise(tmp_path)
    assert summary.failures[0].failing_step == "tool book refused at step MB"


def test_the_shipped_scenarios_parse_and_say_what_they_change() -> None:
    scenarios = load_scenarios(SCENARIOS)
    assert len(scenarios) == 20
    by_level: dict[str, int] = {}
    for file, loaded in scenarios:
        assert loaded.mutates != "unknown", file
        assert loaded.expect.tools or loaded.expect.agent_says or loaded.expect.fields, file
        by_level[loaded.mutates] = by_level.get(loaded.mutates, 0) + 1
    assert by_level == {"books": 7, "moves": 2, "registers": 3, "none": 8}
    # The two that need a recording say so, and name no clip that could be mistaken for one.
    needing = [file for file, loaded in scenarios if loaded.missing_clips]
    assert needing == ["19-out-of-scope-then-shoulder.json", "20-booking-for-a-son.json"]


async def test_stored_runs_can_be_graded_again_without_calling(
    bank: ClipBank, tmp_path: Path
) -> None:
    out = tmp_path / "runs"
    wrong_fact = scenario(
        "lookup", persona={"facts": {"name": "A B", "date_of_birth": "2002-02-26"}}
    )
    await run_batch([("lookup.json", wrong_fact)], agent, bank, out, turn_settings=FAST)
    assert summarise(out).failed == 1

    made: list[int] = []
    corrected = scenario("lookup")  # the persona's date of birth is 20 Feb, as the agent stored
    assert regrade(out, [("lookup.json", corrected)]) == 1
    assert not made
    assert summarise(out).passed == 1
    assert regrade(out, [("lookup.json", corrected)]) == 0  # nothing left to change


class EmptyThenReady:
    """A trace reader whose first answers are empty, as a call's record is just after it ends."""

    def __init__(self, empty_reads: int) -> None:
        self.empty_reads = empty_reads
        self.reads = 0

    async def read(self, call_ref: str) -> Trace:
        self.reads += 1
        if self.reads <= self.empty_reads:
            return Trace(call_ref=call_ref, termination="Client disconnected: 1000")
        return await FixedTrace("20th of February, 2002").read(call_ref)


async def test_an_empty_trace_is_read_again_until_it_has_the_call_in_it(
    bank: ClipBank, tmp_path: Path
) -> None:
    reader = EmptyThenReady(empty_reads=2)
    waits: list[float] = []

    async def no_wait(seconds: float) -> None:
        waits.append(seconds)

    def make() -> tuple[Leg, TraceReader | None]:
        return agent()[0], reader

    out = tmp_path / "runs"
    await run_batch(
        [("lookup.json", scenario("lookup"))], make, bank, out, turn_settings=FAST, wait=no_wait
    )

    assert reader.reads == 3 and waits == [3.0, 3.0]
    assert summarise(out).passed == 1


async def test_a_trace_that_stays_empty_leaves_the_call_ungraded_not_failed(
    bank: ClipBank, tmp_path: Path
) -> None:
    async def no_wait(seconds: float) -> None:
        return None

    def make() -> tuple[Leg, TraceReader | None]:
        return agent()[0], EmptyThenReady(empty_reads=99)

    out = tmp_path / "runs"
    await run_batch(
        [("lookup.json", scenario("lookup"))], make, bank, out, turn_settings=FAST, wait=no_wait
    )

    summary = summarise(out)
    assert (summary.passed, summary.failed, summary.ungraded) == (0, 0, 1)
    payload = json.loads((out / "run-001.json").read_text(encoding="utf-8"))
    assert payload["trace"] is None
    assert "still empty" in payload["trace_error"]
