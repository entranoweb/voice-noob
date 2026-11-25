"""Round 4 on P8: a chain result keeps none of a forgotten call's values, regrade leaves a
redacted result alone, and forget names an earlier unfinished removal it is finishing."""

import json
from pathlib import Path

import pytest
from test_batch_and_report import FAST, agent, scenario
from test_clip_consent import FULL, forget_args, forget_world, write_bank
from test_forget_review import SENTINEL, realistic_result
from test_reset_chains_readback import BOOKED, CANCELLED, agents, chain_data

from synthiq_caller import cli, removals
from synthiq_caller.batch import load_chains, regrade, regrade_chains, run_batch
from synthiq_caller.clips import ClipBank


def chain_with_sentinel(world: dict[str, Path]) -> Path:
    path = world["results"] / "chain-001.json"
    path.write_text(
        json.dumps(
            {
                "chain_file": "chain.json",
                "chain": "c",
                "outcome": "ungraded",
                "mutates": "none",
                "uncaptured": True,
                "reason": f"chain stopped: call 'first' gave no value for {SENTINEL}",
                "variables": {"patient": SENTINEL, "day": "Wednesday 3rd"},
                "calls": [
                    {
                        "id": "first",
                        "run": "run-001.json",
                        "outcome": "passed",
                        "reason": f"could not capture {SENTINEL}",
                    },
                    {"id": "second", "run": None, "outcome": "not run", "reason": ""},
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_a_chain_result_keeps_none_of_a_forgotten_calls_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = forget_world(tmp_path)
    (world["results"] / "run-001.json").write_text(
        json.dumps(realistic_result("a1")), encoding="utf-8"
    )
    path = chain_with_sentinel(world)
    assert cli.main(forget_args(world, "a1")) == 0
    out = capsys.readouterr().out
    # nothing under the whole results tree still holds it
    for file in sorted(p for p in world["results"].rglob("*") if p.is_file()):
        assert SENTINEL not in file.read_text(encoding="utf-8"), file.name
        assert "Zebediah" not in file.read_text(encoding="utf-8"), file.name
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data["variables"]) == {"patient", "day"}  # the names stay
    assert data["outcome"] == "ungraded" and data["calls"][0]["outcome"] == "passed"
    assert data["calls"][0]["run"] == "run-001.json"
    (mark,) = data["clips_removed"]
    assert mark["redacted"] is True
    assert mark["redacted_fields"] == ["calls[*].reason", "reason", "variables"]
    assert "variables" in out and "calls[*].reason" in out


def test_a_chain_result_is_kept_whole_under_keep_text(tmp_path: Path) -> None:
    world = forget_world(tmp_path)
    (world["results"] / "run-001.json").write_text(
        json.dumps(realistic_result("a1")), encoding="utf-8"
    )
    path = chain_with_sentinel(world)
    assert cli.main(forget_args(world, "a1", "--keep-text", "--reason", "retention_expired")) == 0
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["variables"]["patient"] == SENTINEL
    assert data["clips_removed"][0]["redacted"] is False


async def test_regrade_leaves_a_redacted_result_and_its_grade_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bank = write_bank(tmp_path / "bank", {"opener": FULL, "dob": FULL})
    out = tmp_path / "out"
    await run_batch(
        [("lookup.json", scenario("lookup"))], agent, ClipBank(bank), out, turn_settings=FAST
    )
    path = out / "run-001.json"
    assert json.loads(path.read_text(encoding="utf-8"))["grade"]["outcome"] == "passed"
    empty = tmp_path / "scenarios"
    empty.mkdir()
    world = {"bank": bank, "scenarios": empty, "results": out}
    assert cli.main(forget_args(world, "opener")) == 0
    before = path.read_bytes()
    capsys.readouterr()
    assert regrade(out, [("lookup.json", scenario("lookup"))]) == 0
    assert path.read_bytes() == before
    assert json.loads(before)["grade"]["outcome"] == "passed"
    assert "1 redacted results not regraded" in capsys.readouterr().out


async def test_regrade_chains_leaves_a_redacted_result_and_its_grade_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "bank" / "days").mkdir(parents=True)
    bank = write_bank(
        tmp_path / "bank",
        {"opener": FULL, "dob": FULL, "cancel": FULL, "days/wednesday-3rd": FULL},
    )
    out = tmp_path / "out"
    directory = tmp_path / "chains"
    directory.mkdir()
    (directory / "chain.json").write_text(json.dumps(chain_data()), encoding="utf-8")
    await run_batch(
        [],
        agents([[BOOKED], [CANCELLED]]),
        ClipBank(bank),
        out,
        chains=load_chains(directory),
        allow_mutating="all",
        turn_settings=FAST,
    )
    world = {"bank": bank, "scenarios": directory, "results": out}
    assert cli.main(forget_args(world, "opener")) == 0
    runs = sorted(out.glob("run-*.json"))
    before = {p.name: p.read_bytes() for p in runs}
    redacted = [
        p.name
        for p in runs
        if json.loads(p.read_text(encoding="utf-8")).get("clips_removed", [{}])[0].get("redacted")
    ]
    assert redacted
    capsys.readouterr()
    assert regrade_chains(out, load_chains(directory)) == 0
    assert {p.name: p.read_bytes() for p in runs} == before
    assert f"{len(redacted)} redacted results not regraded" in capsys.readouterr().out


def test_finishing_an_earlier_removal_is_not_called_nothing_to_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = forget_world(tmp_path)
    assert cli.main(forget_args(world, "a1")) == 0
    # everything is already removed; only a dangling start is left in the log
    log = world["bank"] / removals.LOG_FILE
    log.write_text("", encoding="utf-8")
    removals.append(
        log,
        {
            "removal": "rm_20260101T000000Z_abc123",
            "removed_at": "2026-01-01T00:00:00+00:00",
            "target": "a1",
            "clips": ["a1"],
            "outcome": "started",
        },
    )
    capsys.readouterr()
    assert cli.main(forget_args(world, "a1")) == 0
    out = capsys.readouterr().out
    assert "finishing earlier unfinished removal rm_20260101T000000Z_abc123" in out
    assert "nothing to change" not in out


def _recorded_run(world: dict[str, Path]) -> tuple[Path, Path, Path, Path]:
    results = world["results"]
    audio = results / "audio"
    audio.mkdir()
    caller = audio / "CA123.caller.wav"
    agent_track = audio / "CA123.agent.wav"
    lookalike = world["bank"] / "a1.caller.wav"  # beside the clips, not a clip
    for path in (caller, agent_track, lookalike):
        path.write_bytes(b"RIFF....WAVE")
    run = results / "run-001.json"
    data = realistic_result("a1")
    data["line"] = {"audio": {"caller": "audio/CA123.caller.wav", "agent": "audio/CA123.agent.wav"}}
    run.write_text(json.dumps(data), encoding="utf-8")
    return run, caller, agent_track, lookalike


def test_forget_deletes_the_caller_track_of_a_recorded_call_and_says_what_it_keeps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = forget_world(tmp_path)
    _run, caller, agent_track, lookalike = _recorded_run(world)
    assert cli.main(forget_args(world, "a1", "--dry-run")) == 0
    assert caller.exists()
    dry = capsys.readouterr().out
    assert "would delete caller-side call audio" in dry and "CA123.caller.wav" in dry
    assert cli.main(forget_args(world, "a1")) == 0
    out = capsys.readouterr().out
    assert not caller.exists()
    assert agent_track.exists() and "NOT deleted" in out and "CA123.agent.wav" in out
    assert lookalike.exists()  # exact-name matching never reaches a recording beside the clips


def test_keep_text_keeps_the_call_audio_and_lists_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = forget_world(tmp_path)
    _run, caller, _agent, _look = _recorded_run(world)
    assert cli.main(forget_args(world, "a1", "--reason", "retention_expired", "--keep-text")) == 0
    assert caller.exists()
    assert "CA123.caller.wav" in capsys.readouterr().out
