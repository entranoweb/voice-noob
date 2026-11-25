"""P8 final review: each finding was run before it was fixed, and each test here fails without
its fix."""

import json
import os
import shutil
from pathlib import Path

import pytest
from test_batch_and_report import FAST, agent, scenario
from test_capture_ingest import captured, no_ffmpeg  # noqa: F401 - the autouse fixture
from test_clip_consent import FULL, forget_args, forget_world, write_bank, write_wav
from test_forget_review import SENTINEL, realistic_result
from test_reset_chains_readback import BOOKED, CANCELLED, agents, chain_data

from synthiq_caller import acceptance, cli, removals, scorecard
from synthiq_caller.batch import load_chains, regrade, run_batch
from synthiq_caller.clips import ClipBank
from synthiq_caller.ingest import ingest
from synthiq_caller.report import render, summarise


def bare_world(tmp_path: Path, rows: dict[str, dict[str, str]] | None = None) -> dict[str, Path]:
    bank = write_bank(tmp_path / "bank", rows or {"opener": FULL, "dob": FULL})
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    results = tmp_path / "out"
    results.mkdir()
    return {"bank": bank, "scenarios": scenarios, "results": results}


def test_a_phone_run_file_is_redacted_like_any_other_result(tmp_path: Path) -> None:
    world = bare_world(tmp_path)
    run = realistic_result("opener")
    path = world["results"] / "phone-run-001.json"
    path.write_text(
        json.dumps({"record": run["record"], "phone": {}, "recordings": []}), encoding="utf-8"
    )
    assert SENTINEL in path.read_text(encoding="utf-8")
    assert cli.main(forget_args(world, "opener")) == 0
    assert SENTINEL not in path.read_text(encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8"))["clips_removed"]


def test_a_result_file_with_a_new_name_is_still_redacted(tmp_path: Path) -> None:
    world = bare_world(tmp_path)
    path = world["results"] / "call-2026-11-25.json"
    path.write_text(json.dumps({"record": realistic_result("opener")["record"]}), encoding="utf-8")
    assert cli.main(forget_args(world, "opener")) == 0
    assert SENTINEL not in path.read_text(encoding="utf-8")


async def test_a_keep_text_result_is_not_regraded_into_another_outcome(tmp_path: Path) -> None:
    world = bare_world(tmp_path)
    await run_batch(
        [("s.json", scenario("s"))], agent, ClipBank(world["bank"]), world["results"],
        turn_settings=FAST,
    )  # fmt: skip
    path = world["results"] / "run-001.json"
    before_outcome = json.loads(path.read_text(encoding="utf-8"))["grade"]["outcome"]
    args = forget_args(world, "opener", "--reason", "retention_expired", "--keep-text")
    assert cli.main(args) == 0
    stored = path.read_bytes()
    harder = scenario("s", expect={"tools": [{"name_contains": "nonexistent"}]})
    assert regrade(world["results"], [("s.json", harder)]) == 0
    assert path.read_bytes() == stored
    assert json.loads(stored)["grade"]["outcome"] == before_outcome


def test_an_incomplete_removal_stays_open_until_a_later_one_completes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = forget_world(tmp_path)
    locked = world["bank"] / "locked"
    write_wav(locked / "a1.wav")  # a second copy where nothing can be deleted
    locked.chmod(0o555)
    try:
        assert cli.main(forget_args(world, "a1")) == 1
        assert removals.read_log(world["bank"] / removals.LOG_FILE)[-1]["outcome"] == "incomplete"
        # it is still open: listed, reported by validate, and a different forget exits non-zero
        assert len(removals.unfinished([world["bank"]])) == 1
        capsys.readouterr()
        assert cli.main(["validate", str(world["scenarios"]), "--clips", str(world["bank"])]) == 1
        assert "UNFINISHED REMOVAL" in capsys.readouterr().out
        assert cli.main(forget_args(world, "b1")) == 1
        assert "UNFINISHED REMOVAL" in capsys.readouterr().out
    finally:
        locked.chmod(0o755)
    assert cli.main(forget_args(world, "a1")) == 0
    assert removals.unfinished([world["bank"]]) == []


async def run_chain(tmp_path: Path) -> dict[str, Path]:
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
    return {"bank": bank, "scenarios": directory, "results": out}


async def test_calls_after_a_forgotten_one_lose_the_values_it_captured(tmp_path: Path) -> None:
    world = await run_chain(tmp_path)
    second = world["results"] / "run-002.json"
    assert "apt-77" in second.read_text(encoding="utf-8")  # carried from call 1
    assert cli.main(forget_args(world, "opener")) == 0
    for file in sorted(p for p in world["results"].rglob("*.json")):
        assert "apt-77" not in file.read_text(encoding="utf-8"), file.name
        assert "Wednesday 3rd" not in file.read_text(encoding="utf-8"), file.name
    assert json.loads(second.read_text(encoding="utf-8"))["clips_removed"][0]["redacted"] is True


async def test_consent_record_missing_is_said_in_every_page_a_person_reads(tmp_path: Path) -> None:
    bank = ClipBank(
        write_bank(tmp_path / "bank", {"opener": {**FULL, "consent_ref": ""}, "dob": FULL}),
        allow_missing_consent=True,
    )
    out = tmp_path / "out"
    await run_batch(
        [("a.json", scenario("a"))], agent, bank, out, turn_settings=FAST,
        allow_missing_consent=True,
    )  # fmt: skip
    stored = json.loads((out / "run-001.json").read_text(encoding="utf-8"))
    assert stored["consent_missing"]
    assert "consent record missing" in render(summarise(str(out)))
    card = scorecard.build([out])
    assert "Consent record missing" in scorecard.render_html(card)
    report = acceptance.build([out], allow_mixed=True, allow_no_manifest=True)
    assert report["clips"]["consent_missing"] == ["opener.wav"]
    page = acceptance.render_html(report)
    assert "Consent record missing" in page
    assert any("consent record missing" in line for line in report["not_shown"])


def test_a_symlinked_clip_takes_its_target_with_it_when_inside_the_bank(tmp_path: Path) -> None:
    world = bare_world(tmp_path)
    store = world["bank"] / "store"
    store.mkdir()
    shutil.move(world["bank"] / "opener.wav", store / "opener.wav")
    os.symlink(store / "opener.wav", world["bank"] / "opener.wav")
    assert cli.main(forget_args(world, "opener")) == 0
    assert not (store / "opener.wav").exists()
    assert not (world["bank"] / "opener.wav").is_symlink()


def test_a_symlink_to_outside_the_bank_is_reported_and_never_called_complete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = bare_world(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.move(world["bank"] / "opener.wav", elsewhere / "opener.wav")
    os.symlink(elsewhere / "opener.wav", world["bank"] / "opener.wav")
    assert cli.main(forget_args(world, "opener")) == 1
    out = capsys.readouterr().out
    assert f"not deleted: {elsewhere / 'opener.wav'} is outside the bank" in out
    assert "complete" not in out.replace("incomplete", "").replace("INCOMPLETE", "")
    assert (elsewhere / "opener.wav").exists()
    assert removals.read_log(world["bank"] / removals.LOG_FILE)[-1]["outcome"] == "incomplete"


def ingested(tmp_path: Path) -> tuple[Path, dict[str, Path], str]:
    case = captured(tmp_path)
    ingest(case)
    first = sorted((case / "clips").glob("*.wav"))[0].stem
    world = {
        "bank": case / "clips",
        "scenarios": case / "scenarios",
        "results": tmp_path / "out",
    }
    world["results"].mkdir()
    return case, world, first


def test_forgetting_an_ingested_clip_removes_the_whole_call_it_came_from(tmp_path: Path) -> None:
    case, world, first = ingested(tmp_path)
    assert list(case.glob("audio.*")) and (case / "transcript.txt").exists()
    assert cli.main(forget_args(world, first)) == 0
    assert not list(case.glob("audio.*"))
    assert not (case / "transcript.txt").exists()
    assert not (case / "conversation.json").exists()


def test_keep_text_keeps_an_ingested_calls_text_and_lists_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case, world, first = ingested(tmp_path)
    args = forget_args(world, first, "--reason", "retention_expired", "--keep-text")
    assert cli.main(args) == 0
    assert not list(case.glob("audio.*"))
    assert (case / "transcript.txt").exists() and (case / "conversation.json").exists()
    out = capsys.readouterr().out
    assert str(case / "transcript.txt") in out and "KEPT" in out
