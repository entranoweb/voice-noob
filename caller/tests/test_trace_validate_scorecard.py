"""What a lookup returned, prerequisites that were never checked, readable run files, the scorecard."""

import io
import json
import sys
import wave
from pathlib import Path
from typing import Any

import httpx
import pytest

from synthiq_caller.batch import CallInfo, ShellHooks, reread_missing_traces, run_batch
from synthiq_caller.clips import ClipBank, ClipExcluded
from synthiq_caller.director import RunRecord
from synthiq_caller.external import run_command
from synthiq_caller.grading import grade
from synthiq_caller.jsonio import utf8_console, write_json
from synthiq_caller.leg import Leg
from synthiq_caller.legs.fake import RATE, AgentLine, FakeLeg, tone
from synthiq_caller.prereq import CommandChecker, HttpChecker
from synthiq_caller.scenario import parse_scenario
from synthiq_caller.scorecard import build, load_previous, render_html
from synthiq_caller.traces.base import ToolCall, Trace, TraceReader
from synthiq_caller.traces.elevenlabs import (
    ElevenLabsTraceReader,
    TraceNotFinal,
    parse_conversation,
)
from synthiq_caller.turns import TurnSettings
from synthiq_caller.validation import validate_directory

FIXTURES = Path(__file__).parent / "fixtures"
FAST = TurnSettings(hangover_s=0.2, audio_start_wait_s=0.6, min_speech_s=0.06)
LOOKUP = "sophie_v2_identify_returning_patient"


def stored_lookup() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (FIXTURES / "elevenlabs_lookup_not_found.json").read_text(encoding="utf-8")
    )
    return data


# --- 1. what the lookup returned ---------------------------------------------------


def test_a_lookup_that_answers_only_success_still_says_not_found() -> None:
    """The stored result body is ``{"success": true}``; the answer is in the variables it set."""
    trace = parse_conversation(stored_lookup())

    assert [c.name for c in trace.tool_calls] == [LOOKUP]
    call = trace.tool_calls[0]
    assert call.arguments == {"fullName": "Mark Marshall", "intent": "mri"}
    assert call.result == {"success": True}
    assert call.outputs["status"] == "not_found"
    assert call.outputs["postcodeOnFile"] == "False"
    assert call.request == {"method": "POST", "url": "https://target.example/api/identify-patient"}
    assert call.step_id == "T1"
    assert trace.step_path[-1] == "SAY_BEEN_BEFORE"


def lookup_scenario(status: str) -> Any:
    return parse_scenario(
        {
            "name": "unknown caller",
            "mutates": "none",
            "steps": [{"when": "name", "clips": ["name.wav"]}],
            "expect": {"tools": [{"name_contains": "identify", "outputs": {"status": status}}]},
        }
    )


def test_found_and_not_found_are_graded_from_what_the_tool_reported() -> None:
    trace = parse_conversation(stored_lookup())
    record = RunRecord(
        scenario="unknown caller", call_ref="c1", ended="caller hung up: scenario end"
    )

    right = grade(lookup_scenario("not_found"), record, trace)
    wrong = grade(lookup_scenario("unique"), record, trace)

    assert right.outcome == "passed"
    assert wrong.outcome == "failed"
    assert [(c.what, c.passed) for c in wrong.checks] == [
        ("tool 'identify' is called and succeeds", True),
        ("tool 'identify' reports status=unique", False),
    ]


def test_a_trace_saved_before_outputs_existed_still_loads() -> None:
    old = {
        "call_ref": "c",
        "tool_calls": [
            {
                "name": "t",
                "arguments": {},
                "result": {},
                "ok": True,
                "step_id": "",
                "t": None,
                "error": "",
            }
        ],
        "step_path": [],
        "termination": "",
    }
    assert Trace.from_json(old).tool_calls[0].outputs == {}


def reader_over(responses: list[httpx.Response]) -> tuple[ElevenLabsTraceReader, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return responses[min(len(seen), len(responses)) - 1]

    reader = ElevenLabsTraceReader(
        "key", wait_s=0.2, poll_s=0.01, transport=httpx.MockTransport(handler)
    )
    return reader, seen


async def test_the_reader_waits_until_the_platform_has_finished_writing_the_call() -> None:
    done = stored_lookup()
    reader, seen = reader_over(
        [
            httpx.Response(404),
            httpx.Response(200, json={**done, "status": "processing", "transcript": []}),
            httpx.Response(200, json=done),
        ]
    )

    trace = await reader.read("conv_x")

    assert len(seen) == 3 and seen[0].endswith("/convai/conversations/conv_x")
    assert trace.tool_calls[0].outputs["status"] == "not_found"


async def test_a_call_that_never_becomes_final_is_not_passed_off_as_a_trace() -> None:
    reader, _ = reader_over([httpx.Response(200, json={"status": "processing", "transcript": []})])
    with pytest.raises(TraceNotFinal):
        await reader.read("conv_x")


class Richer:
    """A reader that now reports what the tool said, where the stored trace did not."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def read(self, call_ref: str) -> Trace:
        if self.fail:
            raise RuntimeError("gone")
        call = ToolCall("identify", {}, {"success": True}, True, outputs={"status": "not_found"})
        return Trace(call_ref=call_ref, tool_calls=[call], step_path=["T1"])


async def test_stored_runs_can_be_read_again_to_gain_what_the_reader_now_keeps(
    tmp_path: Path,
) -> None:
    old = Trace("c", [ToolCall("identify", {}, {"success": True}, True)], ["T1"])
    payload = {
        "record": RunRecord(scenario="s", call_ref="c").to_json(),
        "trace": old.to_json(),
        "trace_error": "",
    }
    write_json(tmp_path / "run-001.json", payload)

    assert await reread_missing_traces(tmp_path, Richer()) == 0  # nothing missing
    assert await reread_missing_traces(tmp_path, Richer(fail=True), every=True) == 0
    kept = json.loads((tmp_path / "run-001.json").read_text(encoding="utf-8"))
    assert kept["trace"]["tool_calls"][0]["name"] == "identify"  # a failed fetch loses nothing

    assert await reread_missing_traces(tmp_path, Richer(), every=True) == 1
    fresh = json.loads((tmp_path / "run-001.json").read_text(encoding="utf-8"))
    assert fresh["trace"]["tool_calls"][0]["outputs"] == {"status": "not_found"}


# --- 2. prerequisites ---------------------------------------------------------------


def write_scenario(directory: Path, name: str, requires: str = "") -> None:
    data = {
        "name": name,
        "mutates": "none",
        "requires": requires,
        "steps": [{"when": "name", "clips": ["name.wav"]}],
    }
    (directory / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")


def exit_with(code: int) -> list[str]:
    return [sys.executable, "-c", f"import sys; sys.exit({code})"]


def test_a_prerequisite_nobody_checked_is_reported_as_unchecked_not_ok(tmp_path: Path) -> None:
    write_scenario(tmp_path, "a-needs-patient", requires="Mark Marshall is on file")
    write_scenario(tmp_path, "b-needs-nothing")

    rows = validate_directory(tmp_path, None, None)

    assert [(r.file, r.requires_state) for r in rows] == [
        ("a-needs-patient.json", "unchecked"),
        ("b-needs-nothing.json", "none"),
    ]
    assert not any(r.problem for r in rows)
    assert "requires UNCHECKED (Mark Marshall is on file)" in rows[0].line()
    assert " ok" not in rows[0].line()


def test_a_chain_with_a_prerequisite_is_unchecked_too(tmp_path: Path) -> None:
    call = {
        "steps": [{"when": "name", "clips": ["name.wav"]}],
        "expect": {"tools": [{"name_contains": "t"}]},
    }
    chain = {
        "name": "book then cancel",
        "requires": "the diary has a free slot",
        "chain": [
            {"id": "book", "mutates": "books", **call},
            {"id": "cancel", "mutates": "moves", **call},
        ],
    }
    (tmp_path / "chain.json").write_text(json.dumps(chain), encoding="utf-8")

    rows = validate_directory(tmp_path, None, None)

    assert (rows[0].requires_state, rows[0].mutates) == ("unchecked", "moves")


def test_a_command_checker_decides_met_or_unmet(tmp_path: Path) -> None:
    write_scenario(tmp_path, "a", requires="Mark Marshall is on file")

    met = validate_directory(tmp_path, None, CommandChecker(exit_with(0)))
    unmet = validate_directory(tmp_path, None, CommandChecker(exit_with(1)))
    broken = validate_directory(tmp_path, None, CommandChecker(exit_with(7)))

    assert met[0].requires_state == "met" and not met[0].problem
    assert unmet[0].requires_state == "unmet" and unmet[0].problem
    assert broken[0].requires_state == "error" and broken[0].problem


def test_the_command_checker_is_told_which_scenario_and_what_it_requires(tmp_path: Path) -> None:
    write_scenario(tmp_path, "a", requires="Mark Marshall is on file")
    script = (
        "import os,sys; "
        "sys.exit(0 if os.environ['SYNTHIQ_REQUIRES']=='Mark Marshall is on file' "
        "and os.environ['SYNTHIQ_SCENARIO_FILE']=='a.json' else 1)"
    )
    rows = validate_directory(tmp_path, None, CommandChecker([sys.executable, "-c", script]))
    assert rows[0].requires_state == "met"


def test_an_http_checker_decides_met_or_unmet(tmp_path: Path) -> None:
    write_scenario(tmp_path, "a", requires="Mark Marshall is on file")
    asked: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(
            {"body": json.loads(request.content), "secret": request.headers.get("x-synthiq-secret")}
        )
        return httpx.Response(200, json={"met": False, "detail": "no such patient"})

    checker = HttpChecker(
        "https://target.example/prereq", secret="s3", transport=httpx.MockTransport(handler)
    )
    rows = validate_directory(tmp_path, None, checker)

    assert rows[0].requires_state == "unmet" and rows[0].requires_detail == "no such patient"
    assert asked[0]["secret"] == "s3"
    assert asked[0]["body"]["requires"] == "Mark Marshall is on file"
    assert "s3" not in repr(checker)


def test_an_http_checker_refuses_to_send_its_secret_in_the_clear() -> None:
    with pytest.raises(ValueError, match="https"):
        HttpChecker("http://target.example/prereq", secret="s3")


def test_the_validate_command_fails_only_on_a_real_problem(tmp_path: Path) -> None:
    from synthiq_caller.cli import main

    write_scenario(tmp_path, "a", requires="Mark Marshall is on file")
    assert main(["validate", str(tmp_path)]) == 0  # unchecked is said, not failed


# --- 3. run files a person can read ---------------------------------------------------


def test_run_files_keep_the_agents_own_characters(tmp_path: Path) -> None:
    line = "I’m sorry you’re in pain. The self-pay fee is £250."
    path = tmp_path / "run.json"
    write_json(path, {"text": line})

    raw = path.read_bytes()
    assert line.encode("utf-8") in raw
    assert b"\\u2019" not in raw and b"\\u00a3" not in raw and b"\r\n" not in raw
    assert "£250".encode() in raw
    assert json.loads(raw.decode("utf-8"))["text"] == line


def test_a_report_redirected_on_windows_is_still_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    """A redirected stdout there takes the legacy code page unless told otherwise."""
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))

    utf8_console()
    print("the fee is £250, I’m afraid")
    sys.stdout.flush()

    assert raw.getvalue().decode("utf-8").strip() == "the fee is £250, I’m afraid"


async def test_what_a_command_prints_in_utf8_comes_back_as_itself() -> None:
    script = "import sys; sys.stdout.buffer.write('£250 I’m'.encode('utf-8'))"
    assert await run_command([sys.executable, "-c", script], {}, 30) == "£250 I’m"


class NoCalls:
    async def read(self, call_ref: str) -> Trace:
        return Trace(call_ref=call_ref, tool_calls=[ToolCall("t", {}, {}, True)], step_path=["A"])


async def test_a_batch_writes_the_agents_apostrophes_as_they_were_spoken(tmp_path: Path) -> None:
    bank_dir = tmp_path / "bank"
    bank_dir.mkdir()
    with wave.open(str(bank_dir / "name.wav"), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(tone(0.25))
    scenario = parse_scenario(
        {
            "name": "pain",
            "mutates": "none",
            "steps": [{"when": "name", "clips": ["name.wav"]}],
            "hangup_after": "sorry",
            "max_seconds": 20,
            "expect": {"tools": [{"name_contains": "t"}]},
        }
    )

    def make() -> tuple[Leg, TraceReader | None]:
        leg = FakeLeg(
            [
                AgentLine("May I take your name?", speech_s=0.3),
                AgentLine("I’m sorry you’re in pain. That is £250.", speech_s=0.3),
            ]
        )
        return leg, NoCalls()

    out = tmp_path / "runs"
    await run_batch(
        [("pain.json", scenario)],
        make,
        ClipBank(bank_dir),
        out,
        turn_settings=FAST,
        allow_unverified_clips=True,  # a bank with no manifest: nobody has checked it
    )

    raw = (out / "run-001.json").read_bytes()
    assert "I’m sorry".encode() in raw and b"\\u2019" not in raw
    assert "£250".encode() in raw and b"\\u00a3" not in raw


# --- 3b. the after-call hook knows which call it is for -------------------------------------


def one_tone(directory: Path, name: str) -> None:
    with wave.open(str(directory / name), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(tone(0.25))


def greeting_scenario(clip: str = "name.wav") -> Any:
    return parse_scenario(
        {
            "name": "greeting",
            "mutates": "none",
            "steps": [{"when": "name", "clips": [clip]}],
            "hangup_after": "thank you",
            "max_seconds": 20,
            "expect": {"tools": [{"name_contains": "t"}]},
        }
    )


def greeting_agent() -> tuple[Leg, TraceReader | None]:
    leg = FakeLeg(
        [AgentLine("May I take your name?", speech_s=0.3), AgentLine("Thank you.", speech_s=0.3)]
    )
    return leg, NoCalls()


async def test_the_after_call_hook_is_given_the_conversation_and_the_stored_run(
    tmp_path: Path,
) -> None:
    bank_dir = tmp_path / "bank"
    bank_dir.mkdir()
    one_tone(bank_dir, "name.wav")
    seen = tmp_path / "seen.txt"
    script = tmp_path / "adopt.py"
    script.write_text(
        "import json, os, sys\n"
        "stored = json.load(open(os.path.join(os.environ['SYNTHIQ_OUT_DIR'],"
        " os.environ['SYNTHIQ_RUN_FILE']), encoding='utf-8'))\n"
        f"open(r'{seen}', 'w').write('|'.join([sys.argv[1], sys.argv[2],"
        " os.environ['SYNTHIQ_CALL_REF'], os.environ['SYNTHIQ_RUN_ID'],"
        " os.environ['SYNTHIQ_OUTCOME'], stored['record']['call_ref']]))\n",
        encoding="utf-8",
    )
    hooks = ShellHooks(after=f'"{sys.executable}" "{script}" {{call_ref}} {{run_file}}')
    out = tmp_path / "runs"

    await run_batch(
        [("g.json", greeting_scenario())],
        greeting_agent,
        ClipBank(bank_dir),
        out,
        hooks=hooks,
        turn_settings=FAST,
        allow_unverified_clips=True,
    )

    stored = json.loads((out / "run-001.json").read_text(encoding="utf-8"))
    ref = stored["record"]["call_ref"]
    assert stored["hook_error"] == "" and ref
    assert seen.read_text().split("|") == [ref, "run-001.json", ref, "run-001", "passed", ref]


async def test_a_hook_that_fails_is_written_into_the_run_it_was_for(tmp_path: Path) -> None:
    bank_dir = tmp_path / "bank"
    bank_dir.mkdir()
    one_tone(bank_dir, "name.wav")
    hooks = ShellHooks(after=f'"{sys.executable}" -c "import sys; sys.exit(4)"')
    out = tmp_path / "runs"

    await run_batch(
        [("g.json", greeting_scenario())],
        greeting_agent,
        ClipBank(bank_dir),
        out,
        hooks=hooks,
        turn_settings=FAST,
        allow_unverified_clips=True,
    )

    stored = json.loads((out / "run-001.json").read_text(encoding="utf-8"))
    assert "after hook" in stored["hook_error"] and "exited 4" in stored["hook_error"]


async def test_a_before_hook_cannot_ask_for_a_call_that_has_not_happened() -> None:
    hooks = ShellHooks(before="adopt {call_ref}")
    with pytest.raises(RuntimeError, match="does not have"):
        await hooks.before_call("g.json", greeting_scenario(), 1)


async def test_a_value_that_is_not_plain_is_never_put_on_a_command_line() -> None:
    hooks = ShellHooks(after="adopt {call_ref}")
    call = CallInfo("conv_1; rm -rf x", "run-001.json", "out", "passed")
    with pytest.raises(RuntimeError, match="not safe"):
        await hooks.after_call("g.json", greeting_scenario(), 1, call)


# --- 3c. clips a person has ruled out, or relabelled ----------------------------------------


def bank_with_overrides(tmp_path: Path, overrides: dict[str, Any]) -> Path:
    bank_dir = tmp_path / "bank"
    bank_dir.mkdir()
    for clip in ("call1_04_reason.wav", "call1_02_name.wav"):
        one_tone(bank_dir, clip)
    (bank_dir / "manifest.csv").write_text(
        "clip,call,category,start,end,secs,said,intended,value,question,review,checked_by_human\n"
        "call1_04_reason,call1,reason,1,2,1,Knee pain.,Knee pain.,,What brings you in?,,\n"
        "call1_02_name,call1,name,0,1,1,Mark Marshal.,Mark Marshal.,Mark Marshal,Your name?,,\n",
        encoding="utf-8",
    )
    (bank_dir / "overrides.json").write_text(json.dumps({"clips": overrides}), encoding="utf-8")
    return bank_dir


WRONG_AUDIO = {
    "exclude": True,
    "reason": "labelled 'Knee pain.' but plays 'Thank you. Bye.'",
    "date": "2026-11-25",
}


def test_an_excluded_clip_cannot_be_played_and_says_why(tmp_path: Path) -> None:
    bank = ClipBank(bank_with_overrides(tmp_path, {"call1_04_reason": WRONG_AUDIO}))

    with pytest.raises(ClipExcluded, match="Thank you. Bye."):
        bank.path("call1_04_reason.wav")
    with pytest.raises(ClipExcluded):
        bank.pcm("call1_04_reason", RATE)
    assert bank.path("call1_02_name.wav").exists()
    assert bank.excluded_in(["call1_02_name.wav", "call1_04_reason.wav"]) == ["call1_04_reason.wav"]


def test_a_relabelled_clip_reports_what_a_person_heard(tmp_path: Path) -> None:
    fixed = {"label": "Mark Marshall.", "reason": "manifest spelling", "date": "2026-11-25"}
    bank = ClipBank(bank_with_overrides(tmp_path, {"call1_02_name": fixed}))

    info = bank.info("call1_02_name.wav")
    assert info is not None and info.said == "Mark Marshall." and info.checked_by_human
    assert bank.path("call1_02_name").exists()


@pytest.mark.parametrize(
    "entry",
    [
        {"exclude": True, "date": "2026-11-25"},
        {"exclude": True, "reason": "wrong audio"},
        {"exclude": True, "reason": "wrong audio", "date": "25 Nov"},
        {"reason": "nothing asked", "date": "2026-11-25"},
        {"exclude": True, "label": "both", "reason": "x", "date": "2026-11-25"},
    ],
)
def test_an_override_without_a_reason_a_date_or_a_decision_is_refused(
    tmp_path: Path, entry: dict[str, Any]
) -> None:
    with pytest.raises(ValueError, match="override|either"):
        ClipBank(bank_with_overrides(tmp_path, {"call1_04_reason": entry}))


async def test_a_batch_does_not_run_a_scenario_that_names_an_excluded_clip(tmp_path: Path) -> None:
    bank = ClipBank(bank_with_overrides(tmp_path, {"call1_04_reason": WRONG_AUDIO}))
    made: list[int] = []

    def make() -> tuple[Leg, TraceReader | None]:
        made.append(1)
        return greeting_agent()

    result = await run_batch(
        [("g.json", greeting_scenario("call1_04_reason.wav"))],
        make,
        bank,
        tmp_path / "runs",
        turn_settings=FAST,
    )

    assert result.ran == 0 and not made
    assert "excluded clips" in result.blocked[0].reason


def test_validate_and_a_generated_scenario_both_leave_the_excluded_clip_out(tmp_path: Path) -> None:
    from synthiq_caller.clipbank.from_call import scenario_from_call

    bank_dir = bank_with_overrides(tmp_path, {"call1_04_reason": WRONG_AUDIO})
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    data = {
        "name": "s",
        "mutates": "none",
        "steps": [{"when": "brings", "clips": ["call1_04_reason.wav"]}],
    }
    (scenarios / "s.json").write_text(json.dumps(data), encoding="utf-8")

    rows = validate_directory(scenarios, ClipBank(bank_dir), None)
    generated = scenario_from_call(bank_dir, "call1")

    assert rows[0].problem and "excluded from bank" in rows[0].line()
    assert [s["clips"] for s in generated["steps"]] == [["call1_02_name"]]
    assert any("excluded" in note for note in generated["notes"]["clips_not_placed"])


# --- 4. scorecard -----------------------------------------------------------------------


def run_payload(
    scenario: str,
    outcome: str,
    *,
    step: str = "DONE",
    fields: list[dict[str, Any]] | None = None,
    trace_error: str = "",
    last_line: str = "Goodbye.",
) -> dict[str, Any]:
    checks = [{"what": "tool 'book' is called and succeeds", "passed": outcome == "passed"}]
    return {
        "scenario_file": f"{scenario}.json",
        "mutates": "none",
        "repeat": 1,
        "record": {
            "scenario": scenario,
            "call_ref": "c",
            "ended": "hangup",
            "duration_s": 30.0,
            "timeline": [{"t": 1.0, "kind": "agent", "text": last_line}],
            "said": [],
        },
        "trace": None
        if outcome == "ungraded"
        else {"call_ref": "c", "tool_calls": [], "step_path": ["I1", step], "termination": ""},
        "trace_error": trace_error,
        "hook_error": "",
        "grade": {
            "scenario": scenario,
            "call_ref": "c",
            "outcome": outcome,
            "checks": [] if outcome == "ungraded" else checks,
            "fields": fields or [],
        },
    }


def field_row(
    field: str, said: str, heard: str | None, stored: str | None, source: str = "tool call"
) -> dict[str, Any]:
    return {
        "stored_source": source if stored is not None else "",
        "field": field,
        "said": said,
        "heard": heard,
        "stored": stored,
        "heard_matches": None if heard is None else heard == said,
        "stored_matches": None if stored is None else stored == said,
        "verdict": "",
    }


def batch_dir(root: Path, name: str, payloads: list[dict[str, Any]]) -> Path:
    directory = root / name
    directory.mkdir()
    for index, payload in enumerate(payloads, start=1):
        write_json(directory / f"run-{index:03d}.json", payload)
    write_json(
        directory / "batch.json",
        {
            "out": str(directory),
            "ran": len(payloads),
            "blocked": [
                {
                    "scenario_file": "z.json",
                    "scenario": "z",
                    "repeat": 1,
                    "reason": "mutation guard: x",
                }
            ],
        },
    )
    return directory


@pytest.fixture
def today(tmp_path: Path) -> Path:
    dob = "1987-07-05"
    return batch_dir(
        tmp_path,
        "today",
        [
            run_payload("books", "passed", fields=[field_row("date_of_birth", dob, dob, dob)]),
            run_payload("books", "passed", fields=[field_row("date_of_birth", dob, dob, dob)]),
            run_payload(
                "moves",
                "failed",
                step="SAY_VERIFY_FAIL",
                fields=[field_row("date_of_birth", dob, "1987-07-06", "1987-07-06", "read back")],
            ),
            run_payload("moves", "failed", step="SAY_VERIFY_FAIL"),
            run_payload("lookup", "failed", step="SAY_BEEN_BEFORE", last_line="<script>x</script>"),
            run_payload("silent", "ungraded", trace_error="RuntimeError('no trace')"),
        ],
    )


def test_the_scorecard_counts_and_groups(today: Path) -> None:
    faults = {"today/run-005.json": "fixture"}
    card = build([today], faults=faults)

    assert (card["calls"], card["passed"], card["failed"], card["ungraded"]) == (6, 2, 3, 1)
    assert card["graded"] == 5 and round(card["rate"], 2) == 0.40
    assert 0.11 < card["interval"][0] < 0.12 and 0.76 < card["interval"][1] < 0.78
    assert {s["scenario"]: (s["passed"], s["graded"]) for s in card["scenarios"]} == {
        "books": (2, 2),
        "moves": (0, 2),
        "lookup": (0, 1),
        "silent": (0, 0),
    }
    groups = {(g["fault"], g["last_step"]): g["count"] for g in card["failure_groups"]}
    assert groups == {("unclassified", "SAY_VERIFY_FAIL"): 2, ("fixture", "SAY_BEEN_BEFORE"): 1}
    assert card["not_graded"][0]["fault"] == "rig"
    dob = next(f for f in card["fields"] if f["field"] == "date_of_birth")
    assert (dob["answered"], dob["misheard"], dob["mis_stored"]) == (3, 1, 1)
    assert dob["read_back"] == 1
    wrong = dob["disagreements"][0]
    assert (wrong["heard"], wrong["stored_source"]) == ("1987-07-06", "read back")
    assert card["not_run"][0]["reason"].startswith("mutation guard")


def test_two_directories_are_added_together(today: Path, tmp_path: Path) -> None:
    more = batch_dir(tmp_path, "more", [run_payload("books", "failed", step="OFFER")])
    card = build([today, more])

    assert (card["calls"], card["passed"], card["graded"]) == (7, 2, 6)
    books = next(s for s in card["scenarios"] if s["scenario"] == "books")
    assert (books["passed"], books["graded"]) == (2, 3)
    assert card["not_run"][0]["count"] == 2


def test_an_unknown_fault_class_is_refused(today: Path) -> None:
    with pytest.raises(ValueError, match="fault class"):
        build([today], faults={"today/run-005.json": "gremlins"})


def test_the_page_is_one_file_with_no_outside_assets_and_escapes_what_agents_say(
    today: Path,
) -> None:
    html = render_html(build([today], title="Evening run"))

    assert html.startswith("<!doctype html>")
    assert "prefers-color-scheme: dark" in html
    assert "<script>x</script>" not in html and "&lt;script&gt;x&lt;/script&gt;" in html
    for outside in ('src="http', 'href="http', "@import", "url(http"):
        assert outside not in html
    assert "2 of 5" in html and "SAY_VERIFY_FAIL" in html and "Evening run" in html


def test_a_scorecard_is_compared_with_the_one_before(today: Path, tmp_path: Path) -> None:
    before_dir = batch_dir(
        tmp_path,
        "before",
        [
            run_payload("books", "failed", step="OFFER"),
            run_payload("moves", "passed"),
            run_payload("gone", "passed"),
        ],
    )
    before_page = tmp_path / "before.html"
    before_page.write_text(render_html(build([before_dir])), encoding="utf-8")

    card = build([today], previous=load_previous(before_page))
    change = card["comparison"]

    # Like for like: only "books" and "moves" were run both times.
    assert (change["before"], change["now"]) == ([1, 2], [2, 4])
    assert {c["name"]: c["verdict"] for c in change["changes"]} == {
        "books": "improved (within noise)",
        "moves": "regressed (within noise)",
        "lookup": "new",
        "silent": "new",
        "gone": "missing",
    }
    assert (change["regressions"], change["beyond_noise"]) == (1, 0)
    html = render_html(card)
    assert "regressed (within noise)" in html and "repeat to be sure" in html


def test_a_stored_baseline_file_can_be_the_thing_compared_with(today: Path, tmp_path: Path) -> None:
    from synthiq_caller.compare import make_baseline, save_baseline
    from synthiq_caller.report import summarise

    path = tmp_path / "baseline.json"
    save_baseline(make_baseline(summarise(today), source="today"), path)

    card = build([today], previous=load_previous(path))

    assert {c["verdict"] for c in card["comparison"]["changes"]} <= {"same", "no graded calls"}


def test_the_command_writes_the_page_and_its_data(today: Path, tmp_path: Path) -> None:
    from synthiq_caller.cli import main

    out = tmp_path / "card.html"
    assert main(["scorecard", str(today), "--out", str(out), "--title", "T"]) == 0
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert load_previous(out).scenarios["books"] == [2, 2]
    assert json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))["calls"] == 6
