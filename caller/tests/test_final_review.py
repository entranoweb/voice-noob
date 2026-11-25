"""P8 final review: each finding was run before it was fixed, and each test here fails without
its fix."""

import json
from pathlib import Path

import pytest
from test_batch_and_report import FAST, agent, scenario
from test_clip_consent import FULL, forget_args, forget_world, write_bank, write_wav
from test_forget_review import SENTINEL, realistic_result

from synthiq_caller import cli, removals
from synthiq_caller.batch import regrade, run_batch
from synthiq_caller.clips import ClipBank


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
