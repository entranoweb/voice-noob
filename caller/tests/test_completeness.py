"""A directory of runs is the calls that were planned, or it says it is not.

Each test here failed on main: a tool reported success (an exit 0, "met", "clips ok")
for something that did not happen. No call is placed and no network is used; runs are
rehearsals through the real command line, or hand-built directories.
"""

import asyncio
import json
import os
import signal
import wave
from pathlib import Path
from typing import Any

import pytest
from test_acceptance import run_directory
from test_manifest import LOOKUP, rehearse
from test_post_merge_review import write_clips

from synthiq_caller import batch, cli, completeness, manifest
from synthiq_caller.jsonio import write_json


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def planned(
    root: Path,
    outcomes: list[str],
    *,
    status: str = "finished",
    held_back: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> Path:
    """A hand-built directory whose manifest carries the plan the rig would have written."""
    extra = {n: {"repeat": 1} for n in range(len(outcomes))}
    out = run_directory(root, outcomes, extra=extra, **kwargs)
    document = read(out / "manifest.json")
    entry = document["entries"][0]
    entry["plan"] = {
        "units": [
            {"file": f"s{n:03d}.json", "repeat": 1, "kind": "run"} for n in range(len(outcomes))
        ]
    }
    entry["status"] = status
    entry["held_back"] = held_back or []
    write_json(out / "manifest.json", document)
    return out


def planned_more(out: Path, extra_units: int) -> None:
    """Add to the plan calls that never happened."""
    document = read(out / "manifest.json")
    units = document["entries"][0]["plan"]["units"]
    for n in range(extra_units):
        units.append({"file": f"never{n}.json", "repeat": 1, "kind": "run"})
    write_json(out / "manifest.json", document)


def drop_run(out: Path, number: int) -> None:
    (out / f"run-{number:03d}.json").unlink()


def baseline_of(directory: Path, tmp_path: Path) -> Path:
    stored = tmp_path / "baseline.json"
    assert cli.main(["baseline", str(directory), "--out", str(stored)]) == 0
    return stored


# --- A: compare --fail-on any over nothing -------------------------------------------------


@pytest.mark.parametrize("shape", ["empty", "batch-json-only"])
def test_compare_fails_on_any_when_nothing_of_the_baseline_ran(tmp_path: Path, shape: str) -> None:
    good = planned(tmp_path / "good", ["passed"] * 4)
    stored = baseline_of(good, tmp_path)
    nothing = tmp_path / "nothing"
    nothing.mkdir()
    if shape == "batch-json-only":
        write_json(nothing / "batch.json", {"out": str(nothing), "ran": 0, "blocked": []})

    assert cli.main(["compare", str(nothing), "--baseline", str(stored), "--fail-on", "any"]) == 1
    assert (
        cli.main(["compare", str(nothing), "--baseline", str(stored), "--fail-on", "beyond-noise"])
        == 1
    )
    # "never" is for a person reading; it does not fail a pipeline on content.
    assert cli.main(["compare", str(nothing), "--baseline", str(stored), "--fail-on", "never"]) == 0


def test_compare_of_a_missing_directory_is_an_error_not_a_pass(tmp_path: Path) -> None:
    stored = baseline_of(planned(tmp_path / "good", ["passed"] * 4), tmp_path)
    code = cli.main(
        ["compare", str(tmp_path / "nope"), "--baseline", str(stored), "--fail-on", "any"]
    )
    assert code == 2


def test_compare_of_an_honest_run_still_passes(tmp_path: Path) -> None:
    good = planned(tmp_path / "good", ["passed"] * 4)
    stored = baseline_of(good, tmp_path)
    assert cli.main(["compare", str(good), "--baseline", str(stored), "--fail-on", "any"]) == 0


def test_a_baseline_is_not_made_of_part_of_a_run(tmp_path: Path) -> None:
    part = planned(tmp_path / "part", ["passed"] * 4)
    drop_run(part, 2)
    assert cli.main(["baseline", str(part), "--out", str(tmp_path / "b.json")]) == 1
    assert not (tmp_path / "b.json").exists()


# --- B: a batch that was stopped -----------------------------------------------------------


def stopped_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str) -> tuple[int, Path]:
    """A real ``batch`` command whose run is cut off after it has written the plan."""
    bank = write_clips(tmp_path / "bank")
    (tmp_path / "set").mkdir()
    (tmp_path / "set" / "lookup.json").write_text(json.dumps(LOOKUP), encoding="utf-8")
    (tmp_path / "agent.json").write_text("{}", encoding="utf-8")
    out = tmp_path / "runs"
    seen: dict[str, Any] = {}

    async def cut_off(*args: Any, **kwargs: Any) -> None:
        # What the manifest holds at the moment the first call would be placed.
        seen["plan"] = read(out / "manifest.json")["entries"][0].get("plan")
        if how == "sigterm":
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(5)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_batch", cut_off)
    code = cli.main(
        ["batch", str(tmp_path / "set"), "--leg", "fake", "--clips", str(bank), "--out", str(out)]
        + ["--fake-agent", str(tmp_path / "agent.json"), "--repeats", "3", "--settle", "0"]
    )
    assert seen["plan"], "the plan must be on disk before the first call"
    assert len(seen["plan"]["units"]) == 3
    return code, out


@pytest.mark.parametrize(("how", "code"), [("sigint", 130), ("sigterm", 143)])
def test_a_stopped_batch_says_so_everywhere_it_is_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    how: str,
    code: int,
) -> None:
    got, out = stopped_batch(tmp_path, monkeypatch, how)
    assert got == code
    assert "INTERRUPTED" in capsys.readouterr().out
    assert read(out / "manifest.json")["entries"][0]["status"] == "interrupted"
    assert completeness.check(out).status == completeness.INCOMPLETE
    assert cli.main(["report", str(out), "--fail-under", "0"]) == 1
    assert cli.main(["gate", str(out)]) == 1
    assert "was interrupted" in capsys.readouterr().out


def test_a_batch_stopped_half_way_does_not_meet_the_gate_on_the_calls_it_made(
    tmp_path: Path,
) -> None:
    out = planned(tmp_path, ["passed"] * 59, status="interrupted")
    planned_more(out, 35)  # 94 were planned; 59 ran, and all of those passed
    code, text = (
        cli.main(["acceptance", str(out), "--claim", "0.95", "--out", str(tmp_path / "r.html")]),
        (tmp_path / "r.html").read_text(encoding="utf-8") if (tmp_path / "r.html").exists() else "",
    )
    assert code == 1
    assert "The claim is met" not in text


# --- C: a deleted run file -----------------------------------------------------------------


def test_deleting_the_one_failed_run_does_not_turn_the_gate_into_a_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = planned(tmp_path, ["passed"] * 59 + ["failed"])
    assert cli.main(["gate", str(out)]) == 1  # 59 of 60: honest, not met
    drop_run(out, 60)
    assert cli.main(["gate", str(out)]) == 1
    assert "removed or lost" in capsys.readouterr().out
    assert cli.main(["acceptance", str(out), "--out", str(tmp_path / "r.html")]) == 1
    assert "The claim is met" not in (tmp_path / "r.html").read_text(encoding="utf-8")
    stored = tmp_path / "base.json"
    full = planned(tmp_path / "full", ["passed"] * 59 + ["failed"])
    assert cli.main(["baseline", str(full), "--out", str(stored)]) == 0
    assert cli.main(["compare", str(out), "--baseline", str(stored), "--fail-on", "any"]) == 1


def test_a_deleted_run_is_seen_even_without_a_plan_by_what_batch_json_counted(
    tmp_path: Path,
) -> None:
    out = run_directory(tmp_path, ["passed"] * 59 + ["failed"], with_manifest=False)
    drop_run(out, 60)
    found = completeness.check(out)
    assert found.status == completeness.INCOMPLETE
    assert "1 removed or lost" in " ".join(found.problems)
    assert cli.main(["report", str(out), "--fail-under", "0.5"]) == 1


def test_a_held_back_call_is_accounted_for_and_an_honest_batch_stays_complete(
    tmp_path: Path,
) -> None:
    out = planned(
        tmp_path,
        ["passed"] * 3,
        held_back=[{"file": "s002.json", "repeat": 1, "reason": "name budget"}],
    )
    drop_run(out, 3)  # that call was held back, not run: nothing is missing
    write_json(out / "batch.json", {"out": str(out), "ran": 2, "blocked": [{}]})
    found = completeness.check(out)
    assert found.status == completeness.COMPLETE
    assert found.held_back == 1


# --- D: gate and mixed scenario versions ---------------------------------------------------


def two_versions(tmp_path: Path) -> Path:
    out = run_directory(tmp_path, ["passed"] * 59)
    document = read(out / "manifest.json")
    second = json.loads(json.dumps(document["entries"][0]))
    second["id"] = "b002"
    second["scenarios"]["files"]["s000.json"] = "f" * 64
    document["entries"].append(second)
    write_json(out / "manifest.json", document)
    run = read(out / "run-002.json")
    run["batch"] = "b002"
    run["scenario_file"] = "s000.json"  # the same scenario file, placed under another version
    write_json(out / "run-002.json", run)
    return out


def test_gate_refuses_runs_of_two_versions_of_a_scenario_as_acceptance_does(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = two_versions(tmp_path)
    assert cli.main(["gate", str(out)]) == 1
    assert "different versions of the same scenario file" in capsys.readouterr().out
    # Told to go on, it judges the numbers (the plan of the second batch is a different matter).
    cli.main(["gate", str(out), "--allow-mixed-scenarios"])
    assert "different versions" not in capsys.readouterr().out


# --- E: a manifest with no entries ---------------------------------------------------------


def test_a_manifest_with_no_entries_is_no_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = run_directory(tmp_path, ["passed"] * 59)
    write_json(out / "manifest.json", {"version": 1, "entries": []})
    report = tmp_path / "r.html"
    assert cli.main(["acceptance", str(out), "--out", str(report)]) == 2
    assert "no run manifest" in capsys.readouterr().out
    assert not report.exists()
    assert cli.main(["acceptance", str(out), "--out", str(report), "--allow-no-manifest"]) == 0


# --- G/I: nothing to check, nothing to run -------------------------------------------------


@pytest.mark.parametrize("strict", [[], ["--strict"]])
def test_validate_of_an_empty_or_missing_directory_is_not_a_pass(
    tmp_path: Path, strict: list[str]
) -> None:
    (tmp_path / "empty").mkdir()
    assert cli.main(["validate", str(tmp_path / "empty"), *strict]) == 1
    assert cli.main(["validate", str(tmp_path / "missing"), *strict]) == 1


def test_batch_of_nothing_stops_and_places_no_call(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    (tmp_path / "agent.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="no scenario or chain files"):
        cli.main(
            [
                "batch",
                str(tmp_path / "empty"),
                "--leg",
                "fake",
                "--fake-agent",
                str(tmp_path / "agent.json"),
                "--clips",
                str(write_clips(tmp_path / "bank")),
            ]
            + ["--out", str(tmp_path / "out")]
        )
    assert not (tmp_path / "out").exists()


def test_batch_where_no_call_could_be_placed_does_not_exit_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = {**LOOKUP, "expect": {"tools": [], "fields": []}}  # asks nothing: held back
    out = rehearse_expecting(tmp_path, scenario)
    assert out != 0
    assert "NO CALL WAS PLACED" in capsys.readouterr().out


def rehearse_expecting(root: Path, scenario: dict[str, Any]) -> int:
    bank = write_clips(root / "bank")
    (root / "set").mkdir()
    (root / "set" / "lookup.json").write_text(json.dumps(scenario), encoding="utf-8")
    (root / "agent.json").write_text("{}", encoding="utf-8")
    return cli.main(
        [
            "batch",
            str(root / "set"),
            "--leg",
            "fake",
            "--clips",
            str(bank),
            "--out",
            str(root / "runs"),
        ]
        + ["--fake-agent", str(root / "agent.json"), "--settle", "0"]
    )


# --- M: a file that cannot be read is a message, not a traceback and not skipped -----------

BROKEN = {"truncated": '{"scenario_file": "s0', "empty": "", "wrong shape": "[1, 2]"}


@pytest.mark.parametrize("kind", sorted(BROKEN))
@pytest.mark.parametrize("victim", ["run-002.json", "manifest.json", "batch.json"])
def test_an_unreadable_file_stops_every_reader_with_its_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], victim: str, kind: str
) -> None:
    out = planned(tmp_path / "x", ["passed"] * 4)
    stored = baseline_of(planned(tmp_path / "y", ["passed"] * 4), tmp_path)
    (out / victim).write_text(BROKEN[kind], encoding="utf-8")
    commands = [
        ["report", str(out)],
        ["gate", str(out)],
        ["acceptance", str(out), "--out", str(tmp_path / "r.html")],
        ["compare", str(out), "--baseline", str(stored), "--fail-on", "any"],
        ["scorecard", str(out), "--out", str(tmp_path / "s.html")],
        ["upload", str(out), "--bundle-out", str(tmp_path / "bundle.json")],
    ]
    for command in commands:
        assert cli.main(command) == 2, command
        text = capsys.readouterr().out
        assert victim in text and "Nothing was skipped" in text, command
    assert not (tmp_path / "bundle.json").exists()


def test_a_corrupt_run_stops_a_regrade_before_any_file_is_changed(tmp_path: Path) -> None:
    out = planned(tmp_path / "x", ["passed"] * 3)
    before = {p.name: p.read_bytes() for p in out.glob("run-*.json")}
    (out / "run-003.json").write_text("{", encoding="utf-8")
    with pytest.raises(completeness.Unreadable):
        batch.regrade(out, [])
    assert read_bytes_except(out, "run-003.json") == {
        k: v for k, v in before.items() if k != "run-003.json"
    }


def read_bytes_except(out: Path, skip: str) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in out.glob("run-*.json") if p.name != skip}


# --- N: a clip that is not audio, or is not there ------------------------------------------


def tiny_wav_bank(root: Path) -> Path:
    bank = write_clips(root / "bank")
    (bank / "dob.wav").write_bytes(b"RIFF" * 5)  # 20 bytes
    return bank


def lookup_set(root: Path) -> Path:
    (root / "set").mkdir(exist_ok=True)
    (root / "set" / "lookup.json").write_text(json.dumps(LOOKUP), encoding="utf-8")
    return root / "set"


def test_validate_does_not_call_a_20_byte_wav_a_clip_that_is_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bank = tiny_wav_bank(tmp_path)
    code = cli.main(["validate", str(lookup_set(tmp_path)), "--clips", str(bank), "--strict"])
    text = capsys.readouterr().out
    assert code == 1
    assert "CLIPS NOT USABLE" in text and "too short to be a WAV" in text
    assert "clips ok" not in text


@pytest.mark.parametrize("damage", ["empty", "truncated"])
def test_a_wav_with_no_audio_or_cut_short_is_not_usable(tmp_path: Path, damage: str) -> None:
    path = tmp_path / "a.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"" if damage == "empty" else b"\x01\x00" * 4000)
    if damage == "truncated":
        path.write_bytes(path.read_bytes()[:-3000])
    from synthiq_caller.clips import wav_problem

    assert wav_problem(path)


def test_a_good_wav_and_a_mulaw_wav_are_usable(tmp_path: Path) -> None:
    from synthiq_caller.clips import wav_problem

    bank = write_clips(tmp_path / "bank")
    assert wav_problem(bank / "dob.wav") == ""


def test_batch_blames_the_rig_not_the_agent_for_a_clip_that_is_not_there(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bank = write_clips(tmp_path / "bank")
    (bank / "dob.wav").unlink()
    (tmp_path / "agent.json").write_text("{}", encoding="utf-8")
    code = cli.main(
        ["batch", str(lookup_set(tmp_path)), "--leg", "fake", "--clips", str(bank)]
        + ["--out", str(tmp_path / "runs"), "--fake-agent", str(tmp_path / "agent.json")]
        + ["--settle", "0"]
    )
    text = capsys.readouterr().out
    assert code != 0
    assert "clip file missing: dob.wav" in text
    assert not list((tmp_path / "runs").glob("run-*.json")), "no call may be placed with it"


def test_batch_holds_back_a_clip_with_no_audio_with_a_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bank = tiny_wav_bank(tmp_path)
    (tmp_path / "agent.json").write_text("{}", encoding="utf-8")
    code = cli.main(
        ["batch", str(lookup_set(tmp_path)), "--leg", "fake", "--clips", str(bank)]
        + ["--out", str(tmp_path / "runs"), "--fake-agent", str(tmp_path / "agent.json")]
        + ["--settle", "0"]
    )
    assert code != 0
    assert "clip not usable: dob.wav" in capsys.readouterr().out


# --- O: an edited result -------------------------------------------------------------------


def edit_outcome(out: Path) -> Path:
    path = sorted(out.glob("run-*.json"))[0]
    payload = read(path)
    payload["grade"]["outcome"] = "passed" if payload["grade"]["outcome"] != "passed" else "failed"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_every_run_the_rig_writes_is_stamped_and_an_edit_of_it_is_seen(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = rehearse(tmp_path)
    assert all("integrity" in read(p) for p in out.glob("run-*.json"))
    assert completeness.check(out).status == completeness.COMPLETE
    edit_outcome(out)
    found = completeness.check(out)
    assert found.status == completeness.ALTERED
    assert cli.main(["report", str(out), "--fail-under", "0"]) == 1
    assert "ALTERED" in capsys.readouterr().out
    assert cli.main(["gate", str(out)]) == 1


def test_a_run_written_before_there_were_digests_is_counted_and_not_called_altered(
    tmp_path: Path,
) -> None:
    out = run_directory(tmp_path, ["passed"] * 3)
    found = completeness.check(out)
    assert found.unstamped == 3 and found.status != completeness.ALTERED


def test_regrade_recomputes_an_edited_grade_from_the_evidence_and_stamps_it_again(
    tmp_path: Path,
) -> None:
    out = rehearse(tmp_path)
    path = edit_outcome(out)
    assert completeness.check(out).status == completeness.ALTERED
    scenarios = batch.load_scenarios(str(tmp_path / "set"), None)
    batch.regrade(out, scenarios)
    assert completeness.check(out).status == completeness.COMPLETE
    assert "integrity" in read(path)


def test_the_digest_is_not_described_as_tamper_proofing() -> None:
    assert "not tamper-proofing" in " ".join((completeness.__doc__ or "").split())


# --- unknown (older directories) and the new flags -----------------------------------------


def test_older_runs_are_judged_on_what_is_there_and_say_completeness_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = run_directory(tmp_path, ["passed"] * 59)  # a manifest, but no plan
    assert completeness.check(out).status == completeness.UNKNOWN
    assert cli.main(["gate", str(out)]) == 0
    assert "completeness unknown" in capsys.readouterr().out
    report = tmp_path / "r.html"
    assert cli.main(["acceptance", str(out), "--out", str(report)]) == 0
    assert "completeness unknown" in report.read_text(encoding="utf-8")
    assert read(tmp_path / "r.json")["completeness"]["status"] == "unknown"


def test_require_complete_turns_unknown_into_a_refusal(tmp_path: Path) -> None:
    out = run_directory(tmp_path, ["passed"] * 59)
    assert cli.main(["gate", str(out), "--require-complete"]) == 1
    report = tmp_path / "r.html"
    # A refusal inside the gate: the claim is not met, and the page says why.
    assert cli.main(["acceptance", str(out), "--out", str(report), "--require-complete"]) == 1
    assert "completeness could not be shown" in report.read_text(encoding="utf-8")


def test_a_complete_planned_directory_meets_the_gate(tmp_path: Path) -> None:
    out = planned(tmp_path, ["passed"] * 59)
    assert completeness.check(out).status == completeness.COMPLETE
    assert cli.main(["gate", str(out), "--require-complete"]) == 0
    assert cli.main(["report", str(out), "--fail-under", "0.9"]) == 0


# --- gate says what acceptance says about the audio ----------------------------------------


def test_gate_prints_the_clip_warnings_acceptance_prints_and_does_not_change_its_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = planned(tmp_path, ["passed"] * 59)
    for path in out.glob("run-*.json"):
        run = read(path)
        run["consent_missing"] = ["dob.wav: no basis"]
        run["provenance_unknown"] = ["opener.wav: unknown"]
        run["labels_unverified"] = ["dob.wav: a recogniser's reading"]
        write_json(path, run)
    assert cli.main(["gate", str(out)]) == 0  # the verdict is the numbers', as before
    text = capsys.readouterr().out
    assert "consent record missing" in text
    assert "provenance unknown" in text
    assert "labels unverified" in text


# --- upload ---------------------------------------------------------------------------------


def test_upload_refuses_an_incomplete_directory_unless_told_and_never_changes_the_bundle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = planned(tmp_path, ["passed"] * 4)
    whole = tmp_path / "whole.json"
    assert cli.main(["upload", str(out), "--bundle-out", str(whole)]) == 0
    assert "completeness" not in read(whole)  # the format is not ours to change
    assert "completeness" not in read(whole)["summary"]
    drop_run(out, 2)
    part = tmp_path / "part.json"
    assert cli.main(["upload", str(out), "--bundle-out", str(part)]) == 1
    assert "--allow-incomplete" in capsys.readouterr().out
    assert not part.exists()
    assert cli.main(["upload", str(out), "--bundle-out", str(part), "--allow-incomplete"]) == 0
    assert part.exists()


# --- the plan --------------------------------------------------------------------------------


def test_the_plan_names_every_scenario_and_repeat_before_a_call_is_placed(tmp_path: Path) -> None:
    out = rehearse(tmp_path, "--repeats", "3")
    entry = read(out / "manifest.json")["entries"][0]
    assert entry["status"] == "finished"
    assert [(u["file"], u["repeat"]) for u in entry["plan"]["units"]] == [
        ("lookup.json", n) for n in (1, 2, 3)
    ]
    assert completeness.check(out).status == completeness.COMPLETE
    assert manifest.load(out) is not None
