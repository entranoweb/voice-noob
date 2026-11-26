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


@pytest.mark.parametrize("how", ["edited", "stripped"])
def test_forget_does_not_restamp_a_result_that_was_already_altered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], how: str
) -> None:
    world = forget_world(tmp_path)
    path = stamped_result(world)
    data = read(path)
    data["grade"]["outcome"] = "passed"  # the hand edit
    if how == "stripped":
        del data["integrity"]
    write_json(path, data)
    before = data.get("integrity")

    code = cli.main(forget_args(world, "a1"))
    out = capsys.readouterr().out

    after = read(path)
    # the erasure still happens: the forgotten person's words are gone
    assert after["clips_removed"] and "Zebediah" not in path.read_text(encoding="utf-8")
    # but the file is NOT vouched for: it is exactly as untrustworthy as it was
    assert after.get("integrity") == before
    assert completeness.altered(after) is (True if how == "edited" else None)
    # and forget says so, and does not end complete
    assert code == 1
    assert "INCOMPLETE" in out and "run-001.json" in out
    assert "NOT restamped" in out


def test_forget_still_restamps_an_intact_result(tmp_path: Path) -> None:
    world = forget_world(tmp_path)
    path = stamped_result(world)
    assert cli.main(forget_args(world, "a1")) == 0
    assert completeness.altered(read(path)) is False


# --- baseline: held-back calls and empty baselines -----------------------------------------

from test_completeness import drop_run, planned  # noqa: E402


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
    assert "no scenarios" in capsys.readouterr().out
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
