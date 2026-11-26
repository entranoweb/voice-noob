"""Tools that reported success for something that did not happen (audit 2026-11).

Each test here failed on main; the PR body records the failure and the revert check.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from test_clip_consent import forget_args, forget_world
from test_forget_review import realistic_result

from synthiq_caller import cli, completeness
from synthiq_caller.jsonio import write_json


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def stamped_result(world: dict[str, Path]) -> Path:
    path = world["results"] / "run-001.json"
    data = {**realistic_result("a1"), "grade": {"outcome": "failed", "checks": []}, "batch": "b001"}
    write_json(
        world["results"] / "manifest.json",
        {
            "version": 1,
            "entries": [
                {"id": "b001", "kind": "batch", "plan": {"units": []}, "status": "finished"}
            ],
        },
    )
    write_json(path, completeness.stamp(data))
    return path


def snapshot(world: dict[str, Path]) -> dict[str, bytes]:
    return {str(p): p.read_bytes() for d in world.values() if d.is_dir() for p in d.rglob("*")}


def altered_world(tmp_path: Path, how: str) -> tuple[dict[str, Path], Path, Any]:
    world = forget_world(tmp_path)
    path = stamped_result(world)
    data = read(path)
    data["grade"]["outcome"] = "passed"  # the hand edit
    data["notes"] = "Zebediah said this"  # a field the rig does not write
    if how == "stripped":
        del data["integrity"]
    write_json(path, data)
    return world, path, data.get("integrity")


@pytest.mark.parametrize("how", ["edited", "stripped"])
def test_forget_refuses_by_default_and_changes_nothing_when_a_result_was_altered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], how: str
) -> None:
    world, path, _ = altered_world(tmp_path, how)
    before = snapshot(world)

    code = cli.main(forget_args(world, "a1"))
    out = capsys.readouterr().out

    assert code == 1
    assert snapshot(world) == before  # byte for byte: no audio, scenario, row or value touched
    assert "REFUSED" in out and "run-001.json" in out and "--erase-altered" in out
    assert ("digest does not match" if how == "edited" else "digest was removed") in out
    assert not (world["bank"] / "removals.jsonl").exists()  # not even a started record
    # a dry run says the same and changes nothing
    assert cli.main([*forget_args(world, "a1"), "--dry-run"]) == 1
    assert snapshot(world) == before


@pytest.mark.parametrize("how", ["edited", "stripped"])
def test_forget_with_erase_altered_blanks_clears_hand_fields_and_leaves_the_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], how: str
) -> None:
    world, path, digest = altered_world(tmp_path, how)

    code = cli.main([*forget_args(world, "a1"), "--erase-altered"])
    out = capsys.readouterr().out

    after = read(path)
    text = path.read_text(encoding="utf-8")
    assert "Zebediah" not in text and after["clips_removed"]  # values blanked, hand field gone
    assert "notes" not in after
    assert after.get("integrity") == digest  # digest untouched: still altered / still stripped
    assert completeness.altered(after) is (True if how == "edited" else None)
    assert code == 1  # the run that found altered files stays nonzero
    assert "cleared hand-added field(s) from" in out and "notes" in out
    record = [json.loads(x) for x in (world["bank"] / "removals.jsonl").read_text().splitlines()]
    assert record[-1]["outcome"] == "complete" and record[-1]["failed"] == []
    assert record[-1]["erase_altered"] is True
    assert record[-1]["cleared_fields"] == {"results/run-001.json": ["notes"]}
    assert "run-001.json" in record[-1]["digest_untouched"][0]
    # a rerun has nothing to do and claims nothing
    assert cli.main(forget_args(world, "a1")) == 0
    assert read(path).get("integrity") == digest


def test_forget_still_restamps_an_intact_result(tmp_path: Path) -> None:
    world = forget_world(tmp_path)
    path = stamped_result(world)
    assert cli.main(forget_args(world, "a1")) == 0
    assert completeness.altered(read(path)) is False
    # the override flag changes nothing for an intact file: nothing is cleared, it is restamped


# --- baseline: held-back calls and empty baselines -----------------------------------------

from test_completeness import baseline_of, drop_run, planned  # noqa: E402


def drop_held(out: Path, *numbers: int) -> None:
    """Remove the run files of calls the rig held back, and say so in batch.json as it does."""
    for n in numbers:
        drop_run(out, n)
    remaining = len(list(out.glob("run-*.json")))
    write_json(
        out / "batch.json", {"out": str(out), "ran": remaining, "blocked": [{}] * len(numbers)}
    )


def held(*numbers: int) -> list[dict[str, Any]]:
    return [
        {"file": f"s{n:03d}.json", "repeat": 1, "reason": "clip past retention"} for n in numbers
    ]


def test_a_baseline_of_a_directory_where_every_call_was_held_back_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = planned(tmp_path / "all", ["passed"] * 3, held_back=held(0, 1, 2))
    drop_held(out, 1, 2, 3)
    stored = tmp_path / "b.json"
    assert cli.main(["baseline", str(out), "--out", str(stored)]) == 1
    assert not stored.exists()
    assert "HELD BACK" in capsys.readouterr().out


def test_a_baseline_of_part_of_a_plan_held_back_is_refused_unless_allowed_and_then_says_so(
    tmp_path: Path,
) -> None:
    out = planned(tmp_path / "part", ["passed"] * 3, held_back=held(2))
    drop_held(out, 3)
    stored = tmp_path / "b.json"
    assert cli.main(["baseline", str(out), "--out", str(stored)]) == 1
    assert not stored.exists()
    assert cli.main(["baseline", str(out), "--out", str(stored), "--allow-held-back"]) == 0
    assert read(stored)["held_back"] == {"allowed": True, "count": 1}


def test_a_baseline_with_nothing_in_it_is_refused_and_cannot_pass_a_compare(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty-baseline.json"
    write_json(
        empty,
        {"version": 2, "created": "", "source": "", "completeness": "complete",
         "scenarios": {}, "chains": {}},
    )  # fmt: skip
    run = planned(tmp_path / "run", ["passed"] * 3)
    for fail_on in ("any", "beyond-noise", "never"):
        assert cli.main(["compare", str(run), "--baseline", str(empty), "--fail-on", fail_on]) == 1
    assert "no graded call" in capsys.readouterr().out
    # and a directory with no runs at all does not become one
    bare = tmp_path / "bare"
    bare.mkdir()
    assert cli.main(["baseline", str(bare), "--out", str(tmp_path / "x.json")]) == 1
    assert not (tmp_path / "x.json").exists()


# --- gate and acceptance see a forgotten clip ----------------------------------------------


def test_gate_and_acceptance_say_how_many_calls_used_a_forgotten_clip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = planned(tmp_path, ["passed"] * 4)
    for n in (1, 2):
        path = out / f"run-{n:03d}.json"
        data = read(path)
        data["clips_removed"] = [{"clip": "a1", "removal": "rm_x", "removed_on": "2026-11-01"}]
        write_json(path, completeness.stamp(data))
    cli.main(["gate", str(out)])
    assert "2 counted call(s) (2 in all) played a clip since removed" in capsys.readouterr().out
    cli.main(["acceptance", str(out), "--out", str(tmp_path / "r.html")])
    page = (tmp_path / "r.html").read_text(encoding="utf-8")
    assert (
        "2 call(s) (2 of them counted in the verdict) played a clip that has since been removed"
        in page
    )


# --- validate --strict after forget ---------------------------------------------------------


def test_validate_strict_fails_on_a_scenario_whose_clip_was_forgotten(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = forget_world(tmp_path)
    assert cli.main(forget_args(world, "a1")) == 0
    capsys.readouterr()
    args = ["validate", str(world["scenarios"]), "--clips", str(world["bank"])]
    assert "MISSING CLIP" in (cli.main(args), capsys.readouterr().out)[1]
    # a scenario that states expectations and lost its clip is the case that passed before
    for path in world["scenarios"].glob("*.json"):
        data = read(path)
        if "expect" in data:
            data["expect"] = {"agent_says": ["hello"]}
            write_json(path, data)
    assert cli.main([*args, "--strict"]) == 1
    out = capsys.readouterr().out
    # one.json is also ungradeable; the missing clip must be named by its own line
    assert "ERROR: one.json: MISSING CLIP" in out


# --- no tracebacks for a mistake in an argument or a scenario -------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["gate", "{d}", "--rate", "50"],
        ["gate", "{d}", "--confidence", "95"],
        ["acceptance", "{d}", "--claim", "50", "--out", "{o}"],
        ["acceptance", "{d}", "--confidence", "95", "--out", "{o}"],
    ],
)
def test_a_percentage_where_a_fraction_is_wanted_is_a_clean_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    out = planned(tmp_path, ["passed"] * 3)
    argv = [a.format(d=out, o=tmp_path / "r.html") for a in argv]
    with pytest.raises(SystemExit) as stop:  # argparse's own usage error, not a traceback
        cli.main(argv)
    assert stop.value.code == 2
    err = capsys.readouterr().err
    assert "fraction between 0 and 1" in err and "0.95" in err
    assert not (tmp_path / "r.html").exists()


def test_a_batch_with_an_unknown_mutates_value_names_the_file_and_places_no_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sets = tmp_path / "set"
    sets.mkdir()
    write_json(
        sets / "bad.json",
        {"name": "bad", "mutates": "sometimes", "steps": [{"when": "hello", "dtmf": "1"}]},
    )
    with pytest.raises(SystemExit) as stop:
        cli.main(
            [
                "batch",
                str(sets),
                "--leg",
                "fake",
                "--clips",
                str(tmp_path),
                "--out",
                str(tmp_path / "o"),
            ]
        )
    assert "bad.json" in str(stop.value) and "mutates must be one of" in str(stop.value)
    assert not (tmp_path / "o").exists()


# --- review of #63: all-ungraded baselines, blocked calls with no plan -----------------------


def test_a_baseline_where_no_call_was_graded_is_refused_and_so_is_one_already_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = planned(tmp_path / "voids", ["void"] * 3)
    stored = tmp_path / "b.json"
    assert cli.main(["baseline", str(out), "--out", str(stored)]) == 1
    assert not stored.exists()
    assert "no graded call" in capsys.readouterr().out

    # a baseline file with only zero totals (written by an older build) is no baseline either
    good = baseline_of(planned(tmp_path / "good", ["passed"] * 3), tmp_path)
    zero = read(good)
    zero["scenarios"] = {"lookup": [0, 0]}
    zero["chains"] = {}
    write_json(tmp_path / "zero.json", zero)
    for mode in ("any", "beyond-noise"):
        code = cli.main(
            ["compare", str(out), "--baseline", str(tmp_path / "zero.json"), "--fail-on", mode]
        )
        assert code == 1
    assert "no graded call" in capsys.readouterr().out


def test_baseline_counts_blocked_calls_in_batch_json_when_there_is_no_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = planned(tmp_path / "noplan", ["passed"] * 3)
    (out / "manifest.json").unlink()
    write_json(out / "batch.json", {"out": str(out), "ran": 3, "blocked": [{}, {}]})
    stored = tmp_path / "b.json"
    assert cli.main(["baseline", str(out), "--out", str(stored)]) == 1
    assert "HELD BACK" in capsys.readouterr().out and not stored.exists()
    assert cli.main(["baseline", str(out), "--out", str(stored), "--allow-held-back"]) == 0
    assert read(stored)["held_back"] == {"allowed": True, "count": 2}


def test_forget_on_an_intact_file_with_a_hand_added_field_is_unchanged(tmp_path: Path) -> None:
    """Intact files behave as before, flag or not: only altered files have fields cleared."""
    world = forget_world(tmp_path)
    path = stamped_result(world)
    data = read(path)
    data["notes"] = "Zebediah said this"
    write_json(path, completeness.stamp(data))
    assert cli.main([*forget_args(world, "a1"), "--erase-altered"]) == 0
    assert read(path)["notes"] == "Zebediah said this"  # unchanged scope: intact files
    assert completeness.altered(read(path)) is False
