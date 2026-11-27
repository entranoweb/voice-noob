"""F6: a write the scenario did not declare is flagged, never passed, and cleaned up after."""

import json
import sys
import wave
from pathlib import Path
from typing import Any

import pytest
from conftest import mark_checked

from synthiq_caller.batch import ShellHooks, run_batch
from synthiq_caller.clips import ClipBank
from synthiq_caller.legs.fake import RATE, AgentLine, FakeLeg, tone
from synthiq_caller.scenario import Scenario, parse_scenario
from synthiq_caller.traces.base import ToolCall, Trace
from synthiq_caller.turns import TurnSettings

FAST = TurnSettings(hangover_s=0.2, audio_start_wait_s=0.6, min_speech_s=0.06)


def read_scenario(mutates: str = "none") -> Scenario:
    return parse_scenario(
        {
            "name": "read only",
            "mutates": mutates,
            "persona": {"facts": {"name": "Alice Wonderland"}},
            "steps": [{"when": "how can i help", "clips": ["opener.wav"]}],
            "hangup_after": "anything else",
            "max_seconds": 20,
            "expect": {"tools": [{"name_contains": "lookup_hours"}]},
        }
    )


@pytest.fixture
def bank(tmp_path: Path) -> ClipBank:
    directory = tmp_path / "bank"
    directory.mkdir()
    with wave.open(str(directory / "opener.wav"), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(tone(0.25))
    mark_checked(directory)
    return ClipBank(directory)


class Calls:
    def __init__(self, *calls: ToolCall) -> None:
        self.calls = list(calls)

    async def read(self, call_ref: str) -> Trace:
        return Trace(call_ref=call_ref, tool_calls=self.calls, final=True)


def leg() -> FakeLeg:
    return FakeLeg(
        [
            AgentLine("Hi, how can I help you today?", speech_s=0.3),
            AgentLine("Is there anything else?", speech_s=0.3),
        ]
    )


HOURS = ToolCall("lookup_hours", {}, {}, True)
BOOKED = ToolCall("book_appointment", {"slot": "x"}, {"id": "A1"}, True)
FAILED_BOOKING = ToolCall("book_appointment", {"slot": "x"}, {}, False, error="slot taken")


async def one(
    bank: ClipBank,
    out: Path,
    reader: Calls,
    *,
    write_tools: list[str] | None,
    hooks: ShellHooks | None = None,
    scenario: Scenario | None = None,
) -> dict[str, Any]:
    scenario = scenario or read_scenario()
    await run_batch(
        [("r.json", scenario)],
        lambda: (leg(), reader),
        bank,
        out,
        turn_settings=FAST,
        write_tools=write_tools,
        hooks=hooks,
    )
    return json.loads((out / "run-001.json").read_text())


async def test_successful_write_in_a_none_scenario_is_flagged_not_passed(
    bank: ClipBank, tmp_path: Path
) -> None:
    run = await one(bank, tmp_path / "o", Calls(HOURS, BOOKED), write_tools=["book_appointment"])
    assert run["grade"]["outcome"] == "failed"
    assert any(not c["passed"] and "book_appointment" in c["what"] for c in run["grade"]["checks"])
    assert run["undeclared_writes"]["tools"] == ["book_appointment"]


async def test_clean_read_only_call_still_passes(bank: ClipBank, tmp_path: Path) -> None:
    run = await one(bank, tmp_path / "o", Calls(HOURS), write_tools=["book_appointment"])
    assert run["grade"]["outcome"] == "passed"
    assert run["undeclared_writes"]["tools"] == []


async def test_a_refused_write_is_not_a_successful_write(bank: ClipBank, tmp_path: Path) -> None:
    run = await one(
        bank, tmp_path / "o", Calls(HOURS, FAILED_BOOKING), write_tools=["book_appointment"]
    )
    assert run["grade"]["outcome"] == "passed"
    assert run["undeclared_writes"]["tools"] == []


async def test_declared_mutation_is_not_flagged(bank: ClipBank, tmp_path: Path) -> None:
    run = await one(
        bank,
        tmp_path / "o",
        Calls(HOURS, BOOKED),
        write_tools=["book_appointment"],
    )
    assert run["undeclared_writes"]["tools"]  # control: flagged for none ...
    batch_out = tmp_path / "p"
    await run_batch(
        [("r.json", read_scenario("registers"))],
        lambda: (leg(), Calls(HOURS, BOOKED)),
        bank,
        batch_out,
        turn_settings=FAST,
        write_tools=["book_appointment"],
        allow_mutating="registers",
    )
    declared = json.loads((batch_out / "run-001.json").read_text())
    assert declared["grade"]["outcome"] == "passed"  # ... but not once declared
    assert declared["undeclared_writes"]["tools"] == []


async def test_no_write_list_says_so_and_does_not_guess(bank: ClipBank, tmp_path: Path) -> None:
    run = await one(bank, tmp_path / "o", Calls(HOURS, BOOKED), write_tools=None)
    watch = run["undeclared_writes"]
    assert watch["checked"] is False
    assert "write_tools" in watch["note"]
    assert run["grade"]["outcome"] == "passed"  # nothing is asserted that was not checked


async def test_tool_outcomes_names_serve_when_no_list(bank: ClipBank, tmp_path: Path) -> None:
    scenario = parse_scenario(
        {
            "name": "r",
            "mutates": "none",
            "persona": {"facts": {"name": "Alice Wonderland"}},
            "steps": [{"when": "how can i help", "clips": ["opener.wav"]}],
            "hangup_after": "anything else",
            "max_seconds": 20,
            "expect": {"tools": [{"name_contains": "lookup_hours"}]},
            "tool_outcomes": {"book_appointment": {"path": "id", "success": ["*"]}},
        }
    )
    run = await one(bank, tmp_path / "o", Calls(HOURS, BOOKED), write_tools=None, scenario=scenario)
    assert run["grade"]["outcome"] == "failed"
    assert run["undeclared_writes"]["tools"] == ["book_appointment"]


async def test_cleanup_hook_still_skipped_for_a_clean_none_call(
    bank: ClipBank, tmp_path: Path
) -> None:
    marker = tmp_path / "ran.txt"
    script = f"import pathlib; pathlib.Path(r'{marker}').write_text('x')"
    hooks = ShellHooks(after=[sys.executable, "-c", script], after_only_when_mutating=True)
    await one(bank, tmp_path / "o", Calls(HOURS), write_tools=["book_appointment"], hooks=hooks)
    assert not marker.exists()


async def test_regrade_keeps_the_stray_write_flag(bank: ClipBank, tmp_path: Path) -> None:
    from synthiq_caller.batch import regrade

    out = tmp_path / "o"
    await one(bank, out, Calls(HOURS, BOOKED), write_tools=["book_appointment"])
    assert regrade(out, [("r.json", read_scenario())]) == 0  # nothing to change
    run = json.loads((out / "run-001.json").read_text())
    assert run["grade"]["outcome"] == "failed"


def marker_hooks(tmp_path: Path) -> tuple[Path, ShellHooks]:
    marker = tmp_path / "ran.txt"
    script = f"import pathlib; pathlib.Path(r'{marker}').write_text('x')"
    return marker, ShellHooks(after=[sys.executable, "-c", script], after_only_when_mutating=True)


async def test_cleanup_hook_is_not_run_when_the_batch_may_not_mutate(
    bank: ClipBank, tmp_path: Path
) -> None:
    marker, hooks = marker_hooks(tmp_path)
    run = await one(  # `one` runs with the default allowance: none
        bank, tmp_path / "o", Calls(HOURS, BOOKED), write_tools=["book_appointment"], hooks=hooks
    )
    assert not marker.exists()
    assert run["grade"]["outcome"] == "failed"  # still flagged
    assert (
        "cleanup not run: mutation not allowed, residue may remain"
        in (run["undeclared_writes"]["cleanup"])
    )
    assert "cleanup not run: mutation not allowed" in run["grade"]["reason"]


async def test_cleanup_hook_runs_when_the_batch_may_mutate(bank: ClipBank, tmp_path: Path) -> None:
    marker, hooks = marker_hooks(tmp_path)
    out = tmp_path / "o"
    await run_batch(
        [("r.json", read_scenario("none"))],
        lambda: (leg(), Calls(HOURS, BOOKED)),
        bank,
        out,
        turn_settings=FAST,
        write_tools=["book_appointment"],
        allow_mutating="registers",
        hooks=hooks,
    )
    assert marker.exists()
    run = json.loads((out / "run-001.json").read_text())
    assert "cleanup not run" not in run["undeclared_writes"]["cleanup"]


async def test_unknown_write_outcome_is_a_possible_write_not_clean(
    bank: ClipBank, tmp_path: Path
) -> None:
    withheld = ToolCall("book_appointment", {"slot": "x"}, {}, True)
    scenario = parse_scenario(
        {
            "name": "r",
            "mutates": "none",
            "persona": {"facts": {"name": "Alice Wonderland"}},
            "steps": [{"when": "how can i help", "clips": ["opener.wav"]}],
            "hangup_after": "anything else",
            "max_seconds": 20,
            "expect": {"tools": [{"name_contains": "lookup_hours"}]},
            "tool_outcomes": {"book_appointment": {"path": "id", "success": ["*"]}},
        }
    )
    run = await one(
        bank, tmp_path / "o", Calls(HOURS, withheld), write_tools=None, scenario=scenario
    )
    watch = run["undeclared_writes"]
    assert watch["possible"] == ["book_appointment"]
    assert watch["checked"] is False  # never "checked, nothing written"
    assert run["grade"]["outcome"] != "passed"
    assert any("may have written" in c["what"] for c in run["grade"]["checks"])


async def test_errored_write_outcome_is_a_possible_write(bank: ClipBank, tmp_path: Path) -> None:
    timed_out = ToolCall("book_appointment", {"slot": "x"}, {}, False, error="connection timeout")
    run = await one(bank, tmp_path / "o", Calls(HOURS, timed_out), write_tools=["book_appointment"])
    assert run["undeclared_writes"]["possible"] == ["book_appointment"]
    assert run["grade"]["outcome"] != "passed"


async def test_report_and_scorecard_show_not_checked_and_possible(
    bank: ClipBank, tmp_path: Path
) -> None:
    from synthiq_caller.mutation_watch import CLEANUP_NOT_ALLOWED, describe, tally

    out = tmp_path / "o"
    await one(bank, out, Calls(HOURS, BOOKED), write_tools=None)  # not checked
    lines = "\n".join(describe(tally([json.loads((out / "run-001.json").read_text())])))
    assert "1 not checked" in lines
    assert "write_tools" in lines
    out2 = tmp_path / "p"
    await one(bank, out2, Calls(HOURS, BOOKED), write_tools=["book_appointment"])
    lines = "\n".join(describe(tally([json.loads((out2 / "run-001.json").read_text())])))
    assert "1 with a successful write" in lines
    assert CLEANUP_NOT_ALLOWED in lines


async def test_chain_calls_are_watched_like_any_other_call(tmp_path: Path) -> None:
    from test_reset_chains_readback import BOOKED as BOOK_SLOT
    from test_reset_chains_readback import agents, chain_data
    from test_reset_chains_readback import bank as chain_bank_fixture

    from synthiq_caller.chain import parse_chain

    data = chain_data()
    data["chain"][0]["mutates"] = "none"  # declared read-only, yet the agent books
    out = tmp_path / "out"
    await run_batch(
        [],
        agents([[BOOK_SLOT]]),
        chain_bank_fixture.__wrapped__(tmp_path),
        out,
        chains=[("c.json", parse_chain(data))],
        allow_mutating="all",
        turn_settings=FAST,
        write_tools=["book_slot"],
    )
    first = json.loads((out / "run-001.json").read_text(encoding="utf-8"))
    assert first["undeclared_writes"]["tools"] == ["book_slot"]
    assert first["grade"]["outcome"] == "failed"
    chain = json.loads((out / "chain-001.json").read_text(encoding="utf-8"))
    assert chain["outcome"] != "passed"


def test_forget_leaves_agent_speech_events_alone_and_chain_captures_ignore_them() -> None:
    from synthiq_caller.forget import _redacted_result

    event = {"t": 1.25, "kind": "agent_speech"}
    data = {"record": {"timeline": [event, {"t": 2.0, "kind": "heard", "text": "secret words"}]}}
    out, _found = _redacted_result(data, "r1")
    assert out["record"]["timeline"][0] == event  # timing only: nothing to redact
    assert "secret" not in json.dumps(out)


async def test_scorecard_html_shows_the_write_watch(bank: ClipBank, tmp_path: Path) -> None:
    from synthiq_caller import scorecard

    out = tmp_path / "o"
    await one(bank, out, Calls(HOURS, BOOKED), write_tools=["book_appointment"])
    card = scorecard.build([out])
    assert card["undeclared_writes"]["stray"] == 1
    html = scorecard.render_html(card)
    assert "Undeclared writes" in html
    assert "cleanup not run: mutation not allowed, residue may remain" in html
