"""Review round on P8: glob safety, the phone path, fail-closed dates, partial failure,
matching rules and erasure redaction."""

import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest
from test_batch_and_report import FAST, agent, scenario
from test_clip_consent import (
    FULL,
    TODAY,
    forget_args,
    forget_world,
    scenario_dir,
    tree,
    write_bank,
    write_wav,
)
from test_phone_leg import scenario_file

from synthiq_caller import cli, consent, removals
from synthiq_caller.batch import run_batch
from synthiq_caller.clips import ClipBank, ClipExcluded
from synthiq_caller.consent import Consent, reference_problem
from synthiq_caller.phone_server import main as phone_main
from synthiq_caller.validation import validate_directory

# --- 1. forget never globs on what it is given ------------------------------------


@pytest.mark.parametrize("target", ["*", "?", "[ab]", "a*", "*.wav", "**", "a?", "[a-z]*"])
def test_forget_never_treats_its_target_as_a_pattern(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], target: str
) -> None:
    world = forget_world(tmp_path)
    before = tree(tmp_path)
    assert cli.main(forget_args(world, target)) == 2
    assert cli.main(forget_args(world, target, "--dry-run")) == 2
    assert tree(tmp_path) == before
    assert "nothing was changed" in capsys.readouterr().out


def test_a_target_with_a_path_in_it_is_refused(tmp_path: Path) -> None:
    world = forget_world(tmp_path)
    before = tree(tmp_path)
    assert cli.main(forget_args(world, "../bank/a1")) == 2
    assert tree(tmp_path) == before


# --- 2. the phone path ------------------------------------------------------------


def phone_args(bank: Path, *extra: str) -> list[str]:
    return [
        "--scenario",
        str(bank / "scenario.json"),
        "--clips",
        str(bank),
        "--out",
        str(bank / "runs"),
        "--dry-run",
        *extra,
    ]


def test_a_phone_dry_run_will_not_play_a_clip_past_retention(tmp_path: Path) -> None:
    write_bank(tmp_path, {"opener": {**FULL, "retain_until": "2020-01-01"}})
    scenario_file(tmp_path)
    with pytest.raises(SystemExit):
        phone_main(phone_args(tmp_path), env={})
    assert not (tmp_path / "runs").exists()


def test_a_phone_dry_run_needs_the_flag_for_unknown_provenance(tmp_path: Path) -> None:
    write_bank(tmp_path, {"opener": {**FULL, "provenance": ""}})
    scenario_file(tmp_path)
    with pytest.raises(SystemExit):
        phone_main(phone_args(tmp_path), env={})
    assert phone_main(phone_args(tmp_path, "--allow-unknown-provenance"), env={}) == 0


def test_a_phone_dry_run_reads_a_second_bank(tmp_path: Path) -> None:
    extra = write_bank(tmp_path / "extra", {"opener": FULL})
    main_bank = tmp_path / "bank"
    main_bank.mkdir()
    scenario_file(main_bank)
    assert phone_main(phone_args(main_bank, "--clips-extra", str(extra)), env={}) == 0


def test_the_bank_itself_refuses_playback_whoever_asks(tmp_path: Path) -> None:
    bank_dir = write_bank(
        tmp_path / "bank",
        {
            "late": {**FULL, "retain_until": "2020-01-01"},
            "typo": {**FULL, "retain_until": "next year"},
            "blank": {**FULL, "provenance": ""},
            "fine": FULL,
        },
    )
    bank = ClipBank(bank_dir)
    for name in ("late", "typo"):
        with pytest.raises(ClipExcluded):
            bank.pcm(f"{name}.wav", 8000)
    with pytest.raises(ClipExcluded):
        bank.joined(["fine.wav", "blank.wav"], 8000)
    assert bank.pcm("fine.wav", 8000)
    bank.allow_unknown_provenance = True
    assert bank.pcm("blank.wav", 8000)
    with pytest.raises(ClipExcluded):  # no flag lifts retention
        bank.pcm("late.wav", 8000)


# --- 3. a date nobody can read fails closed; one clock; phone numbers ---------------


@pytest.mark.parametrize("value", ["next year", "2026-13-45", "31/12/2026", "2026-1-1x"])
def test_an_unreadable_retain_until_counts_as_expired(value: str) -> None:
    assert Consent.from_row({**FULL, "retain_until": value}).expired(TODAY)


async def test_an_unreadable_retain_until_stops_validate_and_batch(tmp_path: Path) -> None:
    bank_dir = write_bank(tmp_path / "bank", {"opener": {**FULL, "retain_until": "soon"}})
    (row,) = validate_directory(scenario_dir(tmp_path, ["opener.wav"]), ClipBank(bank_dir), None)
    assert row.problem and row.expired_clips
    result = await run_batch(
        [("a.json", scenario("a"))],
        agent,
        ClipBank(bank_dir),
        tmp_path / "out",
        turn_settings=FAST,
        allow_unknown_provenance=True,
        allow_unverified_clips=True,
    )
    assert result.ran == 0 and "retention" in result.blocked[0].reason


def test_one_clock_is_used_for_expiry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bank = ClipBank(write_bank(tmp_path / "bank", {"c": {**FULL, "retain_until": "2026-11-24"}}))
    monkeypatch.setattr(consent, "today_utc", lambda: date(2026, 11, 24))
    assert bank.expired_in(["c.wav"]) == []
    monkeypatch.setattr(consent, "today_utc", lambda: date(2026, 11, 25))
    assert bank.expired_in(["c.wav"]) != []


@pytest.mark.parametrize(
    "value", ["0770-090-0123", "07700 900 123", "(0770) 090-0123", "+44 (0)7700-900-123"]
)
def test_a_phone_number_is_caught_whatever_separates_its_digits(value: str) -> None:
    assert "phone number" in reference_problem(value, "consent_ref")
    problems = Consent.from_row({**FULL, "speaker_ref": f"spk_{value}"}).problems()
    assert any("phone number" in p for p in problems), problems


# --- 4. a failure part way is said, recorded and exits non-zero ----------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_a_failure_part_way_is_recorded_as_incomplete_and_exits_non_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = forget_world(tmp_path)
    locked = world["bank"] / "locked"
    write_wav(locked / "a1.wav")  # a second copy of the clip, where nothing can be deleted
    locked.chmod(0o555)
    (world["results"] / "run-001.json").chmod(0o444)
    try:
        code = cli.main(forget_args(world, "a1"))
    finally:
        locked.chmod(0o755)
        (world["results"] / "run-001.json").chmod(0o644)
    out = capsys.readouterr().out
    assert code == 1 and "INCOMPLETE" in out
    # the work that could be done was done
    assert not (world["bank"] / "a1.wav").exists()
    one = json.loads((world["scenarios"] / "one.json").read_text(encoding="utf-8"))
    assert one["steps"][0]["clips"] == ["missing/a1"]
    last = removals.read_log(world["bank"] / removals.LOG_FILE)[-1]
    assert last["outcome"] == "incomplete"
    failed = " ".join(last["failed"])
    assert "a1.wav" in failed and "run-001.json" in failed
    # run again with the files writable: it finishes, and says complete
    assert cli.main(forget_args(world, "a1")) == 0
    assert removals.read_log(world["bank"] / removals.LOG_FILE)[-1]["outcome"] == "complete"
    assert not (locked / "a1.wav").exists()


# --- 5. matching ------------------------------------------------------------------


def test_forgetting_a_clip_leaves_one_whose_name_continues_with_a_dot(tmp_path: Path) -> None:
    world = forget_world(tmp_path)
    bank = write_bank(
        tmp_path / "bank", {"a": FULL, "a.v2": FULL, "a1": FULL, "a2": FULL, "b1": FULL}
    )
    (world["scenarios"] / "dots.json").write_text(
        json.dumps(
            {
                "name": "dots",
                "mutates": "none",
                "steps": [
                    {"when": "name", "clips": ["a.wav"]},
                    {"when": "date", "clips": ["a.v2.wav"]},
                ],
                "expect": {"tools": [], "fields": []},
            }
        ),
        encoding="utf-8",
    )
    assert cli.main(forget_args(world, "a")) == 0
    assert not (bank / "a.wav").exists() and (bank / "a.v2.wav").exists()
    kept = ClipBank(bank)
    assert kept.info("a.v2.wav") is not None and kept.excluded("a.v2.wav") is None
    assert kept.excluded("a.wav") is not None
    dots = json.loads((world["scenarios"] / "dots.json").read_text(encoding="utf-8"))
    assert [s["clips"] for s in dots["steps"]] == [["missing/a"], ["a.v2.wav"]]
    assert cli.main(forget_args(world, "a.v2")) == 0
    assert not (bank / "a.v2.wav").exists()
    assert (bank / "a1.wav").exists()


def test_audio_in_a_folder_of_the_bank_is_found_and_deleted(tmp_path: Path) -> None:
    world = forget_world(tmp_path)
    nested = world["bank"] / "takes" / "day2"
    write_wav(nested / "a2.wav")
    assert cli.main(forget_args(world, "a2", "--dry-run")) == 0
    assert (nested / "a2.wav").exists()
    assert cli.main(forget_args(world, "a2")) == 0
    assert not (nested / "a2.wav").exists() and not (world["bank"] / "a2.wav").exists()


def test_yaml_scenarios_are_rewritten_too(tmp_path: Path) -> None:
    yaml = pytest.importorskip("yaml")
    world = forget_world(tmp_path)
    (world["scenarios"] / "y.yaml").write_text(
        "name: y\nmutates: none\nsteps:\n  - when: name\n    clips: [a1.wav]\n"
        "expect: {tools: [], fields: []}\n",
        encoding="utf-8",
    )
    assert cli.main(forget_args(world, "a1")) == 0
    data = yaml.safe_load((world["scenarios"] / "y.yaml").read_text(encoding="utf-8"))
    assert data["steps"][0]["clips"] == ["missing/a1"] and data["missing_clips"]
    rows = {
        r.file: r for r in validate_directory(world["scenarios"], ClipBank(world["bank"]), None)
    }
    assert rows["y.yaml"].needs_recordings == 1


def test_a_yaml_scenario_that_cannot_be_read_is_a_failure_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = forget_world(tmp_path)
    (world["scenarios"] / "y.yml").write_text(
        "name: y\nsteps:\n  - clips: [a1.wav]\n", encoding="utf-8"
    )
    monkeypatch.setitem(sys.modules, "yaml", None)  # as if PyYAML were not installed
    assert cli.main(forget_args(world, "a1")) == 1
    assert "y.yml" in capsys.readouterr().out
    assert removals.read_log(world["bank"] / removals.LOG_FILE)[-1]["outcome"] == "incomplete"


def test_the_source_recordings_are_named_and_listed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = forget_world(tmp_path)
    source = tmp_path / "recordings"
    source.mkdir()
    write_wav(source / "call-2026-0001.wav")
    (source / "call-2026-0001.stt.json").write_text("{}", encoding="utf-8")
    write_wav(source / "other-call.wav")
    before = tree(source)
    assert cli.main(forget_args(world, "a1", "--recordings", str(source))) == 0
    out = capsys.readouterr().out
    assert "call-2026-0001" in out and "SOURCE RECORDINGS" in out
    assert "call-2026-0001.wav" in out and "other-call" not in out
    assert tree(source) == before  # listed, never deleted


# --- 6. redaction -----------------------------------------------------------------


def stored_said(world: dict[str, Path], name: str = "run-001.json") -> list[dict[str, object]]:
    data = json.loads((world["results"] / name).read_text(encoding="utf-8"))
    return data["record"]["said"]  # type: ignore[no-any-return]


def with_text(world: dict[str, Path]) -> None:
    for name, clip in (("run-001.json", "a1"), ("run-002.json", "b1")):
        path = world["results"] / name
        data = json.loads(path.read_text(encoding="utf-8"))
        data["record"]["said"][0].update(
            {"intended": f"words of {clip}", "heard": [f"heard {clip}"], "answering": "Your name?"}
        )
        path.write_text(json.dumps(data), encoding="utf-8")


def test_forget_redacts_what_was_said_and_heard_and_keeps_the_grade(tmp_path: Path) -> None:
    world = forget_world(tmp_path)
    with_text(world)
    assert cli.main(forget_args(world, "a1")) == 0
    removal = removals.read_log(world["bank"] / removals.LOG_FILE)[-1]["removal"]
    (said,) = stored_said(world)
    assert said["intended"] == f"[redacted {removal}]"
    assert said["heard"] == [f"[redacted {removal}]"]
    assert said["answering"] == "Your name?" and said["clips"] == ["a1.wav"]
    data = json.loads((world["results"] / "run-001.json").read_text(encoding="utf-8"))
    assert data["grade"] == {"outcome": "passed"}
    assert data["clips_removed"][0]["redacted"] is True
    (other,) = stored_said(world, "run-002.json")
    assert other["intended"] == "words of b1" and other["heard"] == ["heard b1"]
    assert "words of a1" not in (world["results"] / "run-001.json").read_text(encoding="utf-8")


def test_keep_text_is_only_allowed_for_an_expired_retention(tmp_path: Path) -> None:
    world = forget_world(tmp_path)
    with_text(world)
    before = tree(tmp_path)
    for reason in ("erasure_request", "consent_withdrawn", "other"):
        with pytest.raises(SystemExit, match="keep-text"):
            cli.main(forget_args(world, "a1", "--keep-text", "--reason", reason))
    assert tree(tmp_path) == before
    assert cli.main(forget_args(world, "a1", "--keep-text", "--reason", "retention_expired")) == 0
    (said,) = stored_said(world)
    assert said["intended"] == "words of a1"
    data = json.loads((world["results"] / "run-001.json").read_text(encoding="utf-8"))
    assert data["clips_removed"][0]["redacted"] is False
    # asking again, this time for the default, redacts what was kept
    assert cli.main(forget_args(world, "a1")) == 0
    assert str(stored_said(world)[0]["intended"]).startswith("[redacted rm_")


def test_redaction_is_idempotent(tmp_path: Path) -> None:
    world = forget_world(tmp_path)
    with_text(world)
    assert cli.main(forget_args(world, "a1")) == 0
    after = tree(tmp_path)
    assert cli.main(forget_args(world, "a1")) == 0
    assert tree(tmp_path) == after
