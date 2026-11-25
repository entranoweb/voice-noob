"""P8 final review: each finding was run before it was fixed, and each test here fails without
its fix."""

import json
from pathlib import Path

from test_clip_consent import FULL, forget_args, write_bank
from test_forget_review import SENTINEL, realistic_result

from synthiq_caller import cli


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
