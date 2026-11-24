"""Reset, read-back, chains of calls and the baseline comparison. Scripted agent only."""

import json
import sys
import wave
from pathlib import Path
from typing import Any

import httpx
import pytest

from synthiq_caller.batch import load_chains, load_scenarios, regrade_chains, run_batch
from synthiq_caller.chain import (
    MissingVariable,
    chain_outcome,
    parse_chain,
    render,
)
from synthiq_caller.cli import main
from synthiq_caller.clips import ClipBank
from synthiq_caller.compare import compare, load_baseline, make_baseline, save_baseline
from synthiq_caller.director import RunRecord
from synthiq_caller.grading import grade
from synthiq_caller.leg import Leg
from synthiq_caller.legs.elevenlabs_socket import ElevenLabsSocketLeg
from synthiq_caller.legs.fake import RATE, AgentLine, FakeLeg, tone
from synthiq_caller.readback import (
    CommandReadBack,
    HttpReadBack,
    NoReadBack,
    ReadBackContext,
    ReadBackFailed,
)
from synthiq_caller.report import Summary, summarise
from synthiq_caller.report import render as render_report
from synthiq_caller.reset import (
    CommandReset,
    HttpReset,
    NoReset,
    ResetContext,
    ResetFailed,
)
from synthiq_caller.scenario import Scenario, ScenarioError, parse_scenario
from synthiq_caller.traces.base import ToolCall, Trace, TraceReader
from synthiq_caller.turns import TurnSettings

FAST = TurnSettings(hangover_s=0.2, audio_start_wait_s=0.6, min_speech_s=0.06)
CONTEXT = ResetContext("a.json", "a scenario", "books", 1)
PY = sys.executable


def single(name: str = "one call", **extra: Any) -> Scenario:
    data: dict[str, Any] = {
        "name": name,
        "mutates": "none",
        "persona": {"facts": {"name": "Alice Wonderland", "date_of_birth": "2002-02-20"}},
        "steps": [
            {"when": "how can i help", "clips": ["opener.wav"]},
            {"when": "date of birth", "clips": ["dob.wav"], "field": "date_of_birth"},
        ],
        "hangup_after": "mobile number",
        "max_seconds": 20,
        "expect": {"tools": [{"name_contains": "check"}], "fields": ["date_of_birth"]},
    }
    data.update(extra)
    return parse_scenario(data)


@pytest.fixture
def bank(tmp_path: Path) -> ClipBank:
    directory = tmp_path / "bank"
    (directory / "days").mkdir(parents=True)
    for name in ("opener.wav", "dob.wav", "cancel.wav", "days/wednesday-3rd.wav"):
        with wave.open(str(directory / name), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(RATE)
            wav.writeframes(tone(0.25))
    return ClipBank(directory)


class Traces:
    """Hands out one prepared trace per call, in order."""

    def __init__(self, traces: list[list[ToolCall]]) -> None:
        self.traces = list(traces)

    async def read(self, call_ref: str) -> Trace:
        return Trace(call_ref=call_ref, tool_calls=self.traces.pop(0), step_path=["S1"])


def lines() -> list[AgentLine]:
    return [
        AgentLine("Hi, how can I help you today?", speech_s=0.3),
        AgentLine("Could I take your date of birth?", speech_s=0.3, hears="20th February 2002"),
        AgentLine("And the best mobile number?", speech_s=0.3),
    ]


def agents(traces: list[list[ToolCall]], made: list[int] | None = None) -> Any:
    reader = Traces(traces)

    def make() -> tuple[Leg, TraceReader | None]:
        if made is not None:
            made.append(1)
        return FakeLeg(lines()), reader

    return make


CHECK_OK = ToolCall("patient_check", {"dateOfBirth": "2002-02-20"}, {}, True)


# --- reset ----------------------------------------------------------------------


async def test_the_command_reset_passes_on_exit_0_and_fails_on_anything_else() -> None:
    await CommandReset(
        [PY, "-c", "import os; assert os.environ['SYNTHIQ_PHASE'] == 'reset'"]
    ).reset(CONTEXT)
    with pytest.raises(ResetFailed, match="exited 3"):
        await CommandReset([PY, "-c", "import sys; sys.exit(3)"]).reset(CONTEXT)
    with pytest.raises(ResetFailed, match="ran past"):
        await CommandReset([PY, "-c", "import time; time.sleep(5)"], timeout_s=0.3).reset(CONTEXT)
    with pytest.raises(ResetFailed, match="could not start"):
        await CommandReset(["no-such-program-synthiq"]).reset(CONTEXT)


async def test_the_http_reset_sends_the_secret_and_fails_without_leaking_it() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204 if request.headers.get("X-Synthiq-Secret") == "s3" else 403)

    transport = httpx.MockTransport(handler)
    await HttpReset("https://target.example/reset", secret="s3", transport=transport).reset(CONTEXT)
    assert json.loads(seen[0].content)["scenario_file"] == "a.json"
    with pytest.raises(ResetFailed) as refused:
        await HttpReset("https://target.example/reset", secret="wrong", transport=transport).reset(
            CONTEXT
        )
    assert "403" in str(refused.value) and "wrong" not in str(refused.value)
    assert "wrong" not in repr(HttpReset("https://target.example/reset", secret="wrong"))


def test_a_secret_is_never_sent_in_the_clear_to_another_machine() -> None:
    with pytest.raises(ValueError, match="https"):
        HttpReset("http://target.example/reset", secret="s3")
    with pytest.raises(ValueError, match="https"):
        HttpReadBack("http://target.example/state", secret="s3")
    HttpReset("http://localhost:8000/reset", secret="s3")


class CountingReset:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[ResetContext] = []
        self.fail = fail

    async def reset(self, context: ResetContext) -> None:
        self.calls.append(context)
        if self.fail:
            raise ResetFailed("reset command exited 1: boom")


async def test_only_scenarios_that_ask_for_a_clean_state_are_reset(
    bank: ClipBank, tmp_path: Path
) -> None:
    reset = CountingReset()
    scenarios = [("a.json", single("needs it", clean_state=True)), ("b.json", single("does not"))]
    result = await run_batch(
        scenarios,
        agents([[CHECK_OK], [CHECK_OK]]),
        bank,
        tmp_path / "out",
        reset=reset,
        turn_settings=FAST,
    )
    assert result.ran == 2
    assert [c.name for c in reset.calls] == ["needs it"]


async def test_a_failed_reset_means_not_run_and_never_failed(
    bank: ClipBank, tmp_path: Path
) -> None:
    made: list[int] = []
    out = tmp_path / "out"
    result = await run_batch(
        [("a.json", single(clean_state=True))],
        agents([], made),
        bank,
        out,
        reset=CountingReset(fail=True),
        turn_settings=FAST,
    )
    assert made == [] and result.ran == 0
    assert "reset command exited 1" in result.blocked[0].reason
    summary = summarise(out)
    assert (summary.failed, summary.runs, len(summary.blocked)) == (0, 0, 1)


async def test_with_no_reset_configured_a_clean_state_scenario_is_not_run(
    bank: ClipBank, tmp_path: Path
) -> None:
    made: list[int] = []
    result = await run_batch(
        [("a.json", single(clean_state=True))],
        agents([], made),
        bank,
        tmp_path / "out",
        turn_settings=FAST,
    )
    assert made == [] and "no reset configured" in result.blocked[0].reason
    # The explicit no-op is the operator saying the system is already clean.
    again = await run_batch(
        [("a.json", single(clean_state=True))],
        agents([[CHECK_OK]]),
        bank,
        tmp_path / "out2",
        reset=NoReset(),
        turn_settings=FAST,
    )
    assert again.ran == 1


async def test_a_batch_that_resets_refuses_to_run_calls_in_parallel(
    bank: ClipBank, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="parallel"):
        await run_batch(
            [("a.json", single(clean_state=True))],
            agents([]),
            bank,
            tmp_path / "out",
            reset=NoReset(),
            max_parallel=2,
        )


# --- read-back -------------------------------------------------------------------


def record_with_answer() -> RunRecord:
    from synthiq_caller.director import Said

    said = Said(1.0, 1, "date_of_birth", "", ["dob.wav"], "", 0.3, False, "dob?", ["20 Feb 2002"])
    return RunRecord(scenario="x", call_ref="c1", said=[said], timeline=[])


def test_stored_comes_from_the_read_back_when_there_is_one_and_says_so() -> None:
    trace = Trace("c1", [CHECK_OK], ["S1"])
    # The tool was sent the right date; the system kept a different one.
    graded = grade(single(), record_with_answer(), trace, {"date_of_birth": "2002-02-21"})
    row = graded.fields[0]
    assert (row.stored, row.stored_source, row.stored_matches) == ("2002-02-21", "read back", False)
    assert graded.outcome == "failed"

    fallback = grade(single(), record_with_answer(), trace, None).fields[0]
    assert (fallback.stored, fallback.stored_source) == ("2002-02-20", "tool call")

    # A read-back that has nothing for this field also falls back, and says so.
    partial = grade(single(), record_with_answer(), trace, {"postcode": "AB1 2CD"}).fields[0]
    assert partial.stored_source == "tool call"


def test_a_tool_saying_booked_is_not_proof_without_a_read_back() -> None:
    scenario = single(
        expect={"tools": [{"name_contains": "check"}], "read_back": {"status": "booked"}}
    )
    trace = Trace("c1", [CHECK_OK], ["S1"])
    assert grade(scenario, record_with_answer(), trace, None).outcome == "ungraded"
    assert grade(scenario, record_with_answer(), trace, {"status": "Booked"}).outcome == "passed"
    assert grade(scenario, record_with_answer(), trace, {"status": "held"}).outcome == "failed"
    assert grade(scenario, record_with_answer(), trace, {}).outcome == "failed"


async def test_the_command_and_http_read_backs_return_the_stored_values() -> None:
    context = ReadBackContext("a.json", "a", 1, "call-9", {"name": "Alice Wonderland"})
    script = (
        "import os, json; print(json.dumps({'ref': os.environ['SYNTHIQ_CALL_REF'], "
        "'who': os.environ['SYNTHIQ_FACT_NAME'], 'empty': None, 'nested': {'a': 1}}))"
    )
    assert await CommandReadBack([PY, "-c", script]).read(context) == {
        "ref": "call-9",
        "who": "Alice Wonderland",
    }
    with pytest.raises(ReadBackFailed, match="JSON"):
        await CommandReadBack([PY, "-c", "print('not json')"]).read(context)
    with pytest.raises(ReadBackFailed, match="exited 2"):
        await CommandReadBack([PY, "-c", "import sys; sys.exit(2)"]).read(context)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.headers.get("X-Synthiq-Secret") != "s3":
            return httpx.Response(401)
        return httpx.Response(200, json={"status": "booked", "call": body["call_ref"]})

    transport = httpx.MockTransport(handler)
    held = await HttpReadBack("https://t.example/state", secret="s3", transport=transport).read(
        context
    )
    assert held == {"status": "booked", "call": "call-9"}
    with pytest.raises(ReadBackFailed, match="401"):
        await HttpReadBack("https://t.example/state", transport=transport).read(context)
    assert await NoReadBack().read(context) == {}


class FixedReadBack:
    def __init__(self, values: dict[str, str] | None) -> None:
        self.values = values
        self.asked: list[ReadBackContext] = []

    async def read(self, context: ReadBackContext) -> dict[str, str]:
        self.asked.append(context)
        if self.values is None:
            raise ReadBackFailed("read-back call answered 500")
        return self.values


async def test_a_batch_records_where_stored_came_from(bank: ClipBank, tmp_path: Path) -> None:
    out = tmp_path / "out"
    reader = FixedReadBack({"date_of_birth": "2002-02-20"})
    await run_batch(
        [("a.json", single())],
        agents([[CHECK_OK]]),
        bank,
        out,
        read_back=reader,
        turn_settings=FAST,
    )
    payload = json.loads((out / "run-001.json").read_text(encoding="utf-8"))
    assert payload["read_back"] == {"date_of_birth": "2002-02-20"}
    assert payload["grade"]["fields"][0]["stored_source"] == "read back"
    assert reader.asked[0].call_ref == "fake-call"
    assert "read back from the system in 1 of 1" in render_report(summarise(out))

    failing = tmp_path / "failing"
    await run_batch(
        [("a.json", single())],
        agents([[CHECK_OK]]),
        bank,
        failing,
        read_back=FixedReadBack(None),
        turn_settings=FAST,
    )
    payload = json.loads((failing / "run-001.json").read_text(encoding="utf-8"))
    assert payload["read_back"] is None and "500" in payload["read_back_error"]
    assert payload["grade"]["fields"][0]["stored_source"] == "tool call"
    assert payload["grade"]["outcome"] == "passed"


# --- chains ----------------------------------------------------------------------


def chain_data(**extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "book then cancel",
        "chain": [
            {
                "id": "book",
                "mutates": "books",
                "persona": {"facts": {"name": "Alice Wonderland", "date_of_birth": "2002-02-20"}},
                "steps": [
                    {"when": "how can i help", "clips": ["opener.wav"]},
                    {"when": "date of birth", "clips": ["dob.wav"], "field": "date_of_birth"},
                ],
                "hangup_after": "mobile number",
                "max_seconds": 20,
                "expect": {"tools": [{"name_contains": "book"}]},
                "capture": {
                    "appointment": {"tool": "book", "result": "appointment.id"},
                    "day": {"tool": "book", "argument": "day"},
                },
            },
            {
                "id": "cancel",
                "mutates": "moves",
                "persona": {"facts": {"name": "Alice Wonderland"}},
                "steps": [
                    {"when": "how can i help", "clips": ["cancel.wav"]},
                    {"when": "date of birth", "clips": ["days/{{day|slug}}.wav"]},
                ],
                "hangup_after": "mobile number",
                "max_seconds": 20,
                "expect": {
                    "tools": [
                        {
                            "name_contains": "cancel",
                            "arguments": {"appointmentId": "{{appointment}}"},
                        }
                    ]
                },
            },
        ],
    }
    data.update(extra)
    return data


BOOKED = ToolCall("book_slot", {"day": "Wednesday 3rd"}, {"appointment": {"id": "apt-77"}}, True)
CANCELLED = ToolCall("cancel_appointment", {"appointmentId": "apt-77"}, {}, True)
CANCELLED_WRONG = ToolCall("cancel_appointment", {"appointmentId": "apt-12"}, {}, True)
REFUSED = ToolCall("book_slot", {"day": "Wednesday 3rd"}, {}, False)


def test_variables_fill_in_with_filters_and_a_missing_one_is_an_error() -> None:
    values = {"day": "Wednesday 3rd", "ref": "a.b"}
    assert render({"clips": ["days/{{ day | slug }}.wav"]}, values) == {
        "clips": ["days/wednesday-3rd.wav"]
    }
    assert render("ref {{ref|re}}", values) == r"ref a\.b"
    with pytest.raises(MissingVariable):
        render("{{nobody}}", values)


def test_a_chain_is_checked_when_it_is_read() -> None:
    chain = parse_chain(chain_data())
    assert chain.mutates == "moves" and [c.id for c in chain.calls] == ["book", "cancel"]

    backwards = chain_data()
    backwards["chain"].reverse()
    with pytest.raises(ScenarioError, match="no earlier call captures"):
        parse_chain(backwards)

    silent = chain_data()
    del silent["chain"][0]["expect"]
    with pytest.raises(ScenarioError, match="expects nothing"):
        parse_chain(silent)

    reset_inside = chain_data()
    reset_inside["chain"][1]["clean_state"] = True
    with pytest.raises(ScenarioError, match="belongs on the chain"):
        parse_chain(reset_inside)

    unsaid = chain_data()
    del unsaid["chain"][1]["mutates"]
    assert parse_chain(unsaid).mutates == "unknown"


async def test_a_chain_carries_what_the_first_call_stored_into_the_second(
    bank: ClipBank, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    reset = CountingReset()
    result = await run_batch(
        [],
        agents([[BOOKED], [CANCELLED]]),
        bank,
        out,
        chains=[("c.json", parse_chain(chain_data(clean_state=True)))],
        allow_mutating="all",
        reset=reset,
        turn_settings=FAST,
    )
    assert (result.ran, result.chains, result.blocked) == (2, 1, [])
    assert len(reset.calls) == 1 and reset.calls[0].chain  # once, before the first call only
    stored = json.loads((out / "chain-001.json").read_text(encoding="utf-8"))
    assert stored["outcome"] == "passed"
    assert stored["variables"] == {"appointment": "apt-77", "day": "Wednesday 3rd"}
    assert [c["outcome"] for c in stored["calls"]] == ["passed", "passed"]
    second = json.loads((out / "run-002.json").read_text(encoding="utf-8"))
    assert second["variables"]["appointment"] == "apt-77"
    assert second["record"]["said"][1]["clips"] == ["days/wednesday-3rd.wav"]
    assert "appointmentId=apt-77" in second["grade"]["checks"][0]["what"]
    summary = summarise(out)
    assert summary.chains == {"book then cancel": [1, 1, 0]}
    assert "chains (each graded as one unit)" in render_report(summary)


async def test_cancelling_the_wrong_appointment_fails_the_chain(
    bank: ClipBank, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    await run_batch(
        [],
        agents([[BOOKED], [CANCELLED_WRONG]]),
        bank,
        out,
        chains=[("c.json", parse_chain(chain_data()))],
        allow_mutating="all",
        turn_settings=FAST,
    )
    stored = json.loads((out / "chain-001.json").read_text(encoding="utf-8"))
    assert stored["outcome"] == "failed"
    assert [c["outcome"] for c in stored["calls"]] == ["passed", "failed"]
    assert "call 'cancel' failed" in render_report(summarise(out))


async def test_when_the_first_call_fails_the_rest_are_not_run(
    bank: ClipBank, tmp_path: Path
) -> None:
    made: list[int] = []
    out = tmp_path / "out"
    result = await run_batch(
        [],
        agents([[REFUSED]], made),
        bank,
        out,
        chains=[("c.json", parse_chain(chain_data()))],
        allow_mutating="all",
        turn_settings=FAST,
    )
    assert len(made) == 1 and result.ran == 1
    stored = json.loads((out / "chain-001.json").read_text(encoding="utf-8"))
    assert stored["outcome"] == "failed"
    assert [c["outcome"] for c in stored["calls"]] == ["failed", "not run"]
    assert result.blocked[0].scenario == "book then cancel / cancel"
    summary = summarise(out)
    assert (summary.failed, summary.runs) == (1, 1)  # the unplaced call is not a failure


async def test_the_mutation_guard_stops_a_whole_chain_before_its_first_call(
    bank: ClipBank, tmp_path: Path
) -> None:
    for allow in ("none", "books"):  # "books" would let the booking through but not the cancel
        made: list[int] = []
        out = tmp_path / allow
        result = await run_batch(
            [],
            agents([], made),
            bank,
            out,
            chains=[("c.json", parse_chain(chain_data()))],
            allow_mutating=allow,
            turn_settings=FAST,
        )
        assert made == [] and result.ran == 0
        assert (
            all("mutation guard" in b.reason for b in result.blocked) and len(result.blocked) == 2
        )
        stored = json.loads((out / "chain-001.json").read_text(encoding="utf-8"))
        assert stored["outcome"] == "not run"


async def test_a_value_the_first_call_did_not_give_stops_the_chain_ungraded(
    bank: ClipBank, tmp_path: Path
) -> None:
    no_id = ToolCall("book_slot", {"day": "Wednesday 3rd"}, {"ok": True}, True)
    made: list[int] = []
    out = tmp_path / "out"
    await run_batch(
        [],
        agents([[no_id]], made),
        bank,
        out,
        chains=[("c.json", parse_chain(chain_data()))],
        allow_mutating="all",
        turn_settings=FAST,
    )
    stored = json.loads((out / "chain-001.json").read_text(encoding="utf-8"))
    assert len(made) == 1 and stored["outcome"] == "ungraded"
    assert "appointment" in stored["calls"][0]["reason"]
    assert summarise(out).chains == {"book then cancel": [0, 0, 1]}


async def test_a_chain_can_carry_a_value_from_the_read_back(bank: ClipBank, tmp_path: Path) -> None:
    data = chain_data()
    data["chain"][0]["capture"]["appointment"] = {"read_back": "appointment_id"}
    out = tmp_path / "out"
    await run_batch(
        [],
        agents([[BOOKED], [ToolCall("cancel_x", {"appointmentId": "sys-5"}, {}, True)]]),
        bank,
        out,
        chains=[("c.json", parse_chain(data))],
        allow_mutating="all",
        read_back=FixedReadBack({"appointment_id": "sys-5"}),
        turn_settings=FAST,
    )
    stored = json.loads((out / "chain-001.json").read_text(encoding="utf-8"))
    assert stored["outcome"] == "passed" and stored["variables"]["appointment"] == "sys-5"


async def test_chain_files_and_single_files_load_separately_and_regrade(
    bank: ClipBank, tmp_path: Path
) -> None:
    directory = tmp_path / "scenarios"
    directory.mkdir()
    (directory / "chain.json").write_text(json.dumps(chain_data()), encoding="utf-8")
    plain = {"name": "plain", "mutates": "none", "steps": [{"when": "a", "clips": ["opener.wav"]}]}
    (directory / "plain.json").write_text(json.dumps(plain), encoding="utf-8")
    assert [f for f, _ in load_scenarios(directory)] == ["plain.json"]
    assert [f for f, _ in load_chains(directory)] == ["chain.json"]

    out = tmp_path / "out"
    await run_batch(
        [],
        agents([[BOOKED], [CANCELLED_WRONG]]),
        bank,
        out,
        chains=load_chains(directory),
        allow_mutating="all",
        turn_settings=FAST,
    )
    # The expectation was too strict: any successful cancel will do. Regrade, no new calls.
    relaxed = chain_data()
    relaxed["chain"][1]["expect"] = {"tools": [{"name_contains": "cancel"}]}
    (directory / "chain.json").write_text(json.dumps(relaxed), encoding="utf-8")
    assert regrade_chains(out, load_chains(directory)) == 1
    stored = json.loads((out / "chain-001.json").read_text(encoding="utf-8"))
    assert stored["outcome"] == "passed"
    assert main(["validate", str(directory), "--clips", str(bank.directory)]) == 0


def test_one_verdict_for_a_chain() -> None:
    assert chain_outcome(["passed", "passed"]) == "passed"
    assert chain_outcome(["passed", "failed"]) == "failed"
    assert chain_outcome(["failed", "not run"]) == "failed"
    assert chain_outcome(["not run", "not run"]) == "not run"
    assert chain_outcome(["ungraded", "not run"]) == "ungraded"
    assert chain_outcome(["passed", "not run"]) == "incomplete"


# --- baseline and compare ---------------------------------------------------------


def summary_of(scenarios: dict[str, list[int]], chains: dict[str, list[int]]) -> Summary:
    return Summary(by_scenario=scenarios, chains={k: [*v, 0] for k, v in chains.items()})


def test_compare_lists_regressions_and_says_which_are_beyond_noise(tmp_path: Path) -> None:
    before = summary_of(
        {
            "steady": [5, 5],
            "slipped": [3, 3],
            "collapsed": [20, 20],
            "better": [1, 5],
            "gone": [2, 2],
        },
        {"book then cancel": [4, 4]},
    )
    path = tmp_path / "baseline.json"
    save_baseline(make_baseline(before, source="runs/monday"), path)
    after = summary_of(
        {
            "steady": [5, 5],
            "slipped": [2, 3],
            "collapsed": [2, 20],
            "better": [5, 5],
            "fresh": [1, 1],
        },
        {"book then cancel": [0, 4]},
    )
    changes = {c.name: c for c in compare(load_baseline(path), after)}
    assert changes["steady"].verdict == "same"
    assert changes["slipped"].verdict == "regressed (within noise)"
    assert changes["collapsed"].verdict == "regressed" and changes["collapsed"].beyond_noise
    assert changes["better"].verdict.startswith("improved")
    assert (changes["gone"].verdict, changes["fresh"].verdict) == ("missing", "new")
    assert changes["book then cancel"].kind == "chain" and changes["book then cancel"].regressed


async def test_baseline_and_compare_from_the_command_line(
    bank: ClipBank, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good, bad = tmp_path / "good", tmp_path / "bad"
    wrong = ToolCall("patient_check", {"dateOfBirth": "2002-02-21"}, {}, True)
    await run_batch([("a.json", single())], agents([[CHECK_OK]]), bank, good, turn_settings=FAST)
    await run_batch([("a.json", single())], agents([[wrong]]), bank, bad, turn_settings=FAST)
    baseline = tmp_path / "baseline.json"
    assert main(["baseline", str(good), "--out", str(baseline)]) == 0
    assert main(["compare", str(good), "--baseline", str(baseline), "--fail-on", "any"]) == 0
    assert main(["compare", str(bad), "--baseline", str(baseline), "--fail-on", "any"]) == 1
    # One call each way is not beyond noise, and the report says so.
    assert (
        main(["compare", str(bad), "--baseline", str(baseline), "--fail-on", "beyond-noise"]) == 0
    )
    printed = capsys.readouterr().out
    assert "regressions: 1" in printed and "within noise" in printed and "95% intervals" in printed


# --- keypad over the socket ---------------------------------------------------------


class RecordingSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))


async def test_socket_keypad_digits_go_as_a_marked_message_only_when_asked() -> None:
    plain = ElevenLabsSocketLeg("key", "agent")
    plain._ws = RecordingSocket()
    with pytest.raises(NotImplementedError, match="no tones"):
        await plain.send_dtmf("1234#")

    leg = ElevenLabsSocketLeg("key", "agent", keypad_as_message=True)
    leg._ws = RecordingSocket()
    await leg.send_dtmf("1234#")
    assert leg._ws.sent == [{"type": "user_message", "text": "1234#", "source_medium": "dtmf"}]
