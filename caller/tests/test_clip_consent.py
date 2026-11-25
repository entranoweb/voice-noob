"""Clip consent and provenance: the manifest fields, the gates, and ``forget``.

No audio of a person anywhere: clips are a quarter second of tone, speaker refs
are made up, and nothing here names a real person or number.
"""

import csv
import hashlib
import json
import wave
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from test_batch_and_report import FAST, agent, scenario
from test_clipbank import words

from synthiq_caller import cli, removals
from synthiq_caller.batch import run_batch
from synthiq_caller.clipbank.build import MANIFEST_COLUMNS, build
from synthiq_caller.clips import ClipBank
from synthiq_caller.consent import Consent
from synthiq_caller.legs.fake import RATE, tone
from synthiq_caller.report import render, summarise
from synthiq_caller.validation import validate_bank, validate_directory

SPK_A = "spk_a1b2c3d4e5f6"
SPK_B = "spk_9z8y7x6w5v4u"
REV = "rev_k3m9q2w7x1"
OP = "op_t4n8r2v6c1"
TODAY = date(2026, 11, 25)

FULL = {
    "source_kind": "call",
    "source_ref": "call-2026-0001",
    "speaker_ref": SPK_A,
    "provenance": "real",
    "lawful_basis": "consent",
    "consent_ref": "consent-0042",
    "recorded_on": "2026-10-01",
    "retain_until": "2027-10-01",
    "label_confirmed_by": REV,
    "label_confirmed_on": "2026-10-05",
}


def write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(tone(0.25))


def write_bank(directory: Path, rows: dict[str, dict[str, str]]) -> Path:
    """A bank whose manifest has every column; each row's audio is written."""
    columns = ["clip", "said", "checked_by_human", *FULL]
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "manifest.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for clip, fields in rows.items():
            write_wav(directory / f"{clip}.wav")
            writer.writerow(
                {"clip": clip, "said": f"said {clip}", "checked_by_human": "x", **fields}
            )
    return directory


def tree(root: Path) -> dict[str, str]:
    """Every file under ``root`` and a hash of what it holds."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# --- the fields ------------------------------------------------------------------


def test_every_field_parses_and_a_complete_row_has_no_problems() -> None:
    consent = Consent.from_row(dict(FULL))
    assert consent.kind == "real"
    assert consent.speaker_ref == SPK_A and consent.lawful_basis == "consent"
    assert consent.problems() == [] and consent.gaps() == []
    assert not consent.expired(TODAY)


@pytest.mark.parametrize(
    ("name", "value", "fragment"),
    [
        ("source_kind", "voicemail", "source_kind"),
        ("source_ref", "a@b.example", "source_ref"),
        ("speaker_ref", "Jane Smith", "opaque id"),
        ("speaker_ref", "jane", "opaque id"),
        ("speaker_ref", "spk_janesmith", "opaque id"),
        ("speaker_ref", "07700900123", "phone number"),
        ("speaker_ref", "+44 7700 900123", "phone number"),
        ("speaker_ref", "spk_07700900123", "phone number"),
        ("provenance", "maybe", "provenance"),
        ("lawful_basis", "because", "lawful_basis"),
        ("lawful_basis", "vital_interest", "lawful_basis"),
        ("consent_ref", "jane@example.test", "consent_ref"),
        ("consent_ref", "has spaces", "consent_ref"),
        ("recorded_on", "01/10/2026", "recorded_on"),
        ("retain_until", "next year", "retain_until"),
        ("retain_until", "2026-09-01", "before recorded_on"),
        ("label_confirmed_by", "Sam", "opaque id"),
        ("label_confirmed_on", "yesterday", "label_confirmed_on"),
    ],
)
def test_each_bad_field_is_named(name: str, value: str, fragment: str) -> None:
    problems = Consent.from_row({**FULL, name: value}).problems()
    assert any(fragment in p for p in problems), problems


def test_consent_as_the_basis_needs_a_record_and_a_confirmation_needs_a_date() -> None:
    no_record = Consent.from_row({**FULL, "consent_ref": ""}).problems()
    assert any("no consent_ref" in p for p in no_record)
    no_date = Consent.from_row({**FULL, "label_confirmed_on": ""}).problems()
    assert any("without label_confirmed_on" in p for p in no_date)
    no_who = Consent.from_row({**FULL, "label_confirmed_by": ""}).problems()
    assert any("without label_confirmed_by" in p for p in no_who)


def test_every_lawful_basis_in_the_set_is_accepted_and_nothing_else() -> None:
    for basis in (
        "consent",
        "contract",
        "legal_obligation",
        "vital_interests",
        "public_task",
        "legitimate_interests",
    ):
        assert Consent.from_row({**FULL, "lawful_basis": basis}).problems() == []


def test_the_bank_reads_the_fields_and_a_confirmer_counts_as_confirmed(tmp_path: Path) -> None:
    bank_dir = write_bank(
        tmp_path / "bank",
        {
            "c1": {**FULL, "checked_by_human": ""},
            "c2": {
                **FULL,
                "checked_by_human": "",
                "label_confirmed_by": "",
                "label_confirmed_on": "",
            },
        },
    )
    bank = ClipBank(bank_dir)
    info = bank.info("c1")
    assert info is not None and info.consent.speaker_ref == SPK_A and info.provenance == "real"
    assert bank.verified("c1") and not bank.verified("c2")


# --- old manifests ---------------------------------------------------------------


def test_a_manifest_from_before_these_fields_still_loads_with_provenance_unknown(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "old"
    write_wav(directory / "call1_01_name.wav")
    (directory / "manifest.csv").write_text(
        "clip,call,category,start,end,secs,said,intended,value,question,review,checked_by_human\n"
        "call1_01_name,call1,name,0,1,1.0,Jo.,Jo.,jo,Your name?,,sam\n",
        encoding="utf-8",
    )
    bank = ClipBank(directory)
    info = bank.info("call1_01_name")
    assert info is not None and info.said == "Jo." and info.checked_by_human
    assert info.provenance == "unknown" and not info.synthetic
    assert bank.provenance_unknown(["call1_01_name.wav"]) == [
        "call1_01_name.wav: no provenance in the manifest"
    ]
    assert bank.verified("call1_01_name")  # the old confirmation is still one


# --- build writes them -----------------------------------------------------------


def test_build_writes_the_provenance_column_it_used_to_leave_out(tmp_path: Path) -> None:
    recordings = tmp_path / "rec"
    recordings.mkdir()
    (recordings / "call1.wav").write_bytes(b"")  # write_audio=False never opens it
    transcript = words(
        ("agent", 0.0, "Could I take your full name, please?"),
        ("caller", 3.0, "Elliot Allen."),
    )
    (recordings / "call1.stt.json").write_text(
        json.dumps({"words": [{**w, "type": "word"} for w in transcript]}), encoding="utf-8"
    )
    assert "provenance" in MANIFEST_COLUMNS
    out = tmp_path / "bank"
    build(
        recordings,
        out,
        write_audio=False,
        consent={"speaker_ref": SPK_A, "lawful_basis": "consent", "consent_ref": "consent-0042"},
    )
    with (out / "manifest.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["provenance"] == "real" and rows[0]["source_kind"] == "call"
    assert rows[0]["source_ref"] == "call1" and rows[0]["speaker_ref"] == SPK_A
    assert ClipBank(out).info(rows[0]["clip"]).provenance == "real"  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="opaque id"):
        build(
            recordings, tmp_path / "bad", write_audio=False, consent={"speaker_ref": "Jane Smith"}
        )


# --- validate --------------------------------------------------------------------


def scenario_dir(tmp_path: Path, clips: list[str]) -> Path:
    directory = tmp_path / "scenarios"
    directory.mkdir(exist_ok=True)
    (directory / "a.json").write_text(
        json.dumps(
            {
                "name": "a",
                "mutates": "none",
                "steps": [{"when": "name", "clips": clips}],
                "expect": {"tools": [], "fields": []},
            }
        ),
        encoding="utf-8",
    )
    return directory


def test_validate_refuses_a_clip_past_its_retention_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bank_dir = write_bank(
        tmp_path / "bank",
        {
            "old": {**FULL, "retain_until": "2026-11-24"},
            "today": {**FULL, "retain_until": TODAY.isoformat()},
        },
    )
    scenarios = scenario_dir(tmp_path, ["old.wav"])
    (row,) = validate_directory(scenarios, ClipBank(bank_dir), None, today=TODAY)
    assert row.problem and row.expired_clips == ["old.wav: retain_until 2026-11-24"]
    assert "PAST RETENTION" in row.line()
    findings = validate_bank(ClipBank(bank_dir), TODAY)
    assert [(f.clip, f.kind) for f in findings if f.problem] == [("old", "past retention")]
    # The last day is still a day the clip may be used.
    kept = scenario_dir(tmp_path, ["today.wav"])
    (row,) = validate_directory(kept, ClipBank(bank_dir), None, today=TODAY)
    assert not row.problem
    # And the command's exit code says so, whichever day it runs on.
    stale = write_bank(tmp_path / "stale", {"old": {**FULL, "retain_until": "2020-01-01"}})
    assert (
        cli.main(["validate", str(scenario_dir(tmp_path, ["old.wav"])), "--clips", str(stale)]) == 1
    )
    assert "PAST RETENTION" in capsys.readouterr().out


def test_validate_reports_clips_with_no_provenance_without_failing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bank_dir = write_bank(tmp_path / "bank", {"nameless": {**FULL, "provenance": ""}})
    scenarios = scenario_dir(tmp_path, ["nameless.wav"])
    (row,) = validate_directory(scenarios, ClipBank(bank_dir), None, today=TODAY)
    assert row.unknown_provenance and not row.problem
    assert "provenance unknown" in row.line()
    assert [f.kind for f in validate_bank(ClipBank(bank_dir), TODAY)] == ["no provenance"]
    assert cli.main(["validate", str(scenarios), "--clips", str(bank_dir)]) == 0
    assert "no provenance" in capsys.readouterr().out


def test_validate_fails_on_a_field_that_is_written_and_wrong(tmp_path: Path) -> None:
    bank_dir = write_bank(tmp_path / "bank", {"c": {**FULL, "speaker_ref": "Jane Smith"}})
    assert (
        cli.main(["validate", str(scenario_dir(tmp_path, ["c.wav"])), "--clips", str(bank_dir)])
        == 1
    )


# --- the batch -------------------------------------------------------------------


def two_clip_bank(tmp_path: Path, **fields: str) -> ClipBank:
    row = {**FULL, **fields}
    return ClipBank(write_bank(tmp_path / "bank", {"opener": row, "dob": row}))


async def test_a_batch_refuses_a_clip_with_no_provenance_unless_allowed(tmp_path: Path) -> None:
    bank = two_clip_bank(tmp_path, provenance="")
    refused = await run_batch(
        [("a.json", scenario("a"))], agent, bank, tmp_path / "no", turn_settings=FAST
    )
    assert refused.ran == 0
    assert "provenance not recorded" in refused.blocked[0].reason
    assert not list((tmp_path / "no").glob("run-*.json"))

    allowed = await run_batch(
        [("a.json", scenario("a"))],
        agent,
        bank,
        tmp_path / "yes",
        turn_settings=FAST,
        allow_unknown_provenance=True,
    )
    assert allowed.ran == 1 and not allowed.blocked
    stored = json.loads((tmp_path / "yes" / "run-001.json").read_text(encoding="utf-8"))
    assert [c.split(":")[0] for c in stored["provenance_unknown"]] == ["opener.wav", "dob.wav"]
    assert "provenance unknown: 1 call(s)" in render(summarise(str(tmp_path / "yes")))


async def test_a_batch_that_knows_every_provenance_says_nothing_about_it(tmp_path: Path) -> None:
    bank = two_clip_bank(tmp_path)
    done = await run_batch(
        [("a.json", scenario("a"))], agent, bank, tmp_path / "ok", turn_settings=FAST
    )
    assert done.ran == 1
    stored = json.loads((tmp_path / "ok" / "run-001.json").read_text(encoding="utf-8"))
    assert stored["provenance_unknown"] == []
    assert "provenance unknown" not in render(summarise(str(tmp_path / "ok")))


async def test_a_clip_past_retention_is_refused_and_no_flag_allows_it(tmp_path: Path) -> None:
    bank = two_clip_bank(tmp_path, retain_until="2026-01-01")
    result = await run_batch(
        [("a.json", scenario("a"))],
        agent,
        bank,
        tmp_path / "out",
        turn_settings=FAST,
        allow_unknown_provenance=True,
        allow_unverified_clips=True,
    )
    assert result.ran == 0 and "past their retention date" in result.blocked[0].reason


# --- forget ----------------------------------------------------------------------

SAID = "my name is Zed Quux and I live at 1 Nowhere Lane"


def forget_world(tmp_path: Path) -> dict[str, Path]:
    bank = write_bank(
        tmp_path / "bank",
        {
            "a1": {**FULL, "speaker_ref": SPK_A, "said": SAID},
            "a2": {**FULL, "speaker_ref": SPK_A},
            "b1": {**FULL, "speaker_ref": SPK_B},
        },
    )
    (bank / "overrides.json").write_text(
        json.dumps({"clips": {"a2": {"label": SAID, "reason": "heard it", "date": "2026-11-01"}}}),
        encoding="utf-8",
    )
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    (scenarios / "one.json").write_text(
        json.dumps(
            {
                "name": "one",
                "mutates": "none",
                "steps": [
                    {"when": "name", "clips": ["a1.wav"]},
                    {"when": "date", "clips": ["b1.wav"]},
                ],
                "fallback": {"clips": ["a2"]},
                "expect": {"tools": [], "fields": []},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    expects = {"tools": [{"name_contains": "x"}], "fields": []}
    (scenarios / "chain.json").write_text(
        json.dumps(
            {
                "name": "c",
                "mutates": "none",
                "chain": [
                    {
                        "id": "first",
                        "mutates": "none",
                        "steps": [{"when": "name", "clips": ["a1.wav"]}],
                        "expect": expects,
                    },
                    {
                        "id": "second",
                        "mutates": "none",
                        "steps": [{"when": "name", "clips": ["b1.wav"]}],
                        "expect": expects,
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (scenarios / "other.json").write_text(
        json.dumps(
            {
                "name": "other",
                "mutates": "none",
                "steps": [{"when": "name", "clips": ["b1.wav"]}],
                "expect": {"tools": [], "fields": []},
            }
        ),
        encoding="utf-8",
    )
    results = tmp_path / "results"
    results.mkdir()

    def run_json(clip: str) -> dict[str, Any]:
        return {
            "scenario_file": "one.json",
            "record": {"said": [{"clips": [f"{clip}.wav"], "synthetic_clips": []}]},
            "labels_unverified": [f"{clip}.wav: not checked by a person"],
            "grade": {"outcome": "passed"},
        }

    (results / "run-001.json").write_text(json.dumps(run_json("a1")), encoding="utf-8")
    (results / "run-002.json").write_text(json.dumps(run_json("b1")), encoding="utf-8")
    (results / "chain-001.json").write_text(
        json.dumps({"chain": "c", "calls": [{"id": "first", "run": "run-001.json"}]}),
        encoding="utf-8",
    )
    (results / "batch.json").write_text(
        json.dumps({"blocked": [{"reason": "excluded clips: ['a2']"}], "ran": 2}),
        encoding="utf-8",
    )
    (results / "upload.json").write_text(json.dumps({"run_id": "x"}), encoding="utf-8")
    return {"bank": bank, "scenarios": scenarios, "results": results}


def forget_args(world: dict[str, Path], target: str, *extra: str) -> list[str]:
    return [
        "forget",
        target,
        "--clips",
        str(world["bank"]),
        "--scenarios",
        str(world["scenarios"]),
        "--results",
        str(world["results"]),
        "--operator",
        OP,
        *extra,
    ]


def test_forget_removes_the_audio_breaks_scenarios_marks_results_and_writes_the_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = forget_world(tmp_path)
    assert cli.main(forget_args(world, SPK_A, "--reason", "consent_withdrawn")) == 0
    out = capsys.readouterr().out
    bank_dir = world["bank"]

    # the audio of the speaker's clips is gone, another speaker's is not
    assert not (bank_dir / "a1.wav").exists() and not (bank_dir / "a2.wav").exists()
    assert (bank_dir / "b1.wav").exists()
    # and so are their manifest rows and the override that held a transcript
    with (bank_dir / "manifest.csv").open(encoding="utf-8", newline="") as handle:
        assert [r["clip"] for r in csv.DictReader(handle)] == ["b1"]
    assert json.loads((bank_dir / "overrides.json").read_text(encoding="utf-8")) == {"clips": {}}
    assert SAID not in (bank_dir / "manifest.csv").read_text(encoding="utf-8")

    # scenarios and chains that played them are "missing clip"; the others are untouched
    one = json.loads((world["scenarios"] / "one.json").read_text(encoding="utf-8"))
    assert one["steps"][0]["clips"] == ["missing/a1"] and one["steps"][1]["clips"] == ["b1.wav"]
    assert one["fallback"]["clips"] == ["missing/a2"]
    assert len(one["missing_clips"]) == 2
    chain = json.loads((world["scenarios"] / "chain.json").read_text(encoding="utf-8"))
    assert chain["chain"][0]["steps"][0]["clips"] == ["missing/a1"]
    assert chain["chain"][0]["missing_clips"] and "missing_clips" not in chain["chain"][1]
    assert "missing/" not in (world["scenarios"] / "other.json").read_text(encoding="utf-8")
    rows = {r.file: r for r in validate_directory(world["scenarios"], ClipBank(bank_dir), None)}
    assert rows["one.json"].needs_recordings == 2 and "MISSING CLIP" in rows["one.json"].line()
    assert rows["chain.json"].needs_recordings == 1 and rows["other.json"].needs_recordings == 0

    # every stored result that used a removed clip says so; the grade is as it was
    marked = {
        name: json.loads((world["results"] / name).read_text(encoding="utf-8"))
        for name in ("run-001.json", "run-002.json", "chain-001.json", "batch.json")
    }
    assert [m["clip"] for m in marked["run-001.json"]["clips_removed"]] == ["a1"]
    assert marked["run-001.json"]["grade"] == {"outcome": "passed"}
    assert "clips_removed" not in marked["run-002.json"]
    assert [m["clip"] for m in marked["chain-001.json"]["clips_removed"]] == ["a1"]
    assert [m["clip"] for m in marked["batch.json"]["clips_removed"]] == ["a2"]

    # one record, with what, when, why, who, and no name, transcript or audio
    log = bank_dir / "removals.jsonl"
    started, line = log.read_text(encoding="utf-8").splitlines()
    assert json.loads(started)["outcome"] == "started"
    record = json.loads(line)
    assert record["outcome"] == "complete"
    assert record["clips"] == ["a1", "a2"] and record["speakers"] == [SPK_A]
    assert record["why"] == "consent_withdrawn" and record["operator"] == OP
    assert record["removed_at"] and record["removal"].startswith("rm_")
    assert "Zed" not in line and "Nowhere" not in line

    # the bank now rules those clips out even if a file is put back
    write_wav(bank_dir / "a1.wav")
    assert ClipBank(bank_dir).excluded("a1.wav") is not None

    # the dashboard gap is named, every time
    assert "NOT DONE" in out and "upload --replace" in out
    assert f"uploaded directories: {world['results']}" in out


def test_forget_dry_run_lists_everything_and_touches_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = forget_world(tmp_path)
    before = tree(tmp_path)
    assert cli.main(forget_args(world, SPK_A, "--dry-run")) == 0
    out = capsys.readouterr().out
    assert tree(tmp_path) == before  # not a byte, and no log file made
    assert not (world["bank"] / "removals.jsonl").exists()
    assert "DRY RUN" in out
    for expected in ("a1.wav", "a2.wav", "manifest.csv", "overrides.json", "one.json"):
        assert expected in out
    assert "chain.json" in out and "run-001.json" in out and "chain-001.json" in out
    assert "run-002.json" not in out and "other.json" not in out
    assert "NOT DONE" in out


def test_forgetting_twice_is_safe_and_the_second_time_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = forget_world(tmp_path)
    assert cli.main(forget_args(world, "a1")) == 0
    after_first = tree(tmp_path)
    capsys.readouterr()
    assert cli.main(forget_args(world, "a1")) == 0
    assert tree(tmp_path) == after_first
    assert len((world["bank"] / "removals.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    assert "nothing to change" in capsys.readouterr().out
    # the speaker's other clip is still there, and is removed by asking for the speaker
    assert (world["bank"] / "a2.wav").exists()
    assert cli.main(forget_args(world, SPK_A)) == 0
    assert not (world["bank"] / "a2.wav").exists()
    again = tree(tmp_path)
    assert cli.main(forget_args(world, SPK_A)) == 0
    assert tree(tmp_path) == again


def test_a_second_forget_catches_up_a_results_directory_it_was_not_given_before(
    tmp_path: Path,
) -> None:
    world = forget_world(tmp_path)
    assert cli.main(forget_args(world, "a1")) == 0
    late = tmp_path / "late"
    late.mkdir()
    (late / "run-001.json").write_text(
        json.dumps({"record": {"said": [{"clips": ["a1.wav"]}]}, "grade": {}}), encoding="utf-8"
    )
    assert cli.main([*forget_args(world, "a1")[:-2], "--results", str(late), "--operator", OP]) == 0
    assert json.loads((late / "run-001.json").read_text(encoding="utf-8"))["clips_removed"]
    assert len(removals.read_log(world["bank"] / "removals.jsonl")) == 4


def test_forget_refuses_what_it_cannot_name_and_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = forget_world(tmp_path)
    before = tree(tmp_path)
    assert cli.main(forget_args(world, "nobody_1")) == 2
    assert cli.main(forget_args(world, "spk_zzzzzzzz9")) == 2
    assert "nothing was changed" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="operator"):
        cli.main([*forget_args(world, "a1")[:-2]])
    with pytest.raises(SystemExit, match="opaque id"):
        cli.main([*forget_args(world, "a1")[:-1], "Jane Smith"])
    assert tree(tmp_path) == before


async def test_a_real_run_marked_by_forget_says_so_in_its_report(tmp_path: Path) -> None:
    bank = two_clip_bank(tmp_path)
    await run_batch([("a.json", scenario("a"))], agent, bank, tmp_path / "out", turn_settings=FAST)
    before = json.loads((tmp_path / "out" / "run-001.json").read_text(encoding="utf-8"))
    code = cli.main(
        ["forget", "dob", "--clips", str(bank.directory), "--results", str(tmp_path / "out")]
        + ["--operator", OP, "--reason", "retention_expired", "--keep-text"]
    )
    assert code == 0
    after = json.loads((tmp_path / "out" / "run-001.json").read_text(encoding="utf-8"))
    assert after.pop("clips_removed")[0]["clip"] == "dob"
    assert after == before  # nothing else about the stored call changed
    assert "clips removed: 1 call(s)" in render(summarise(str(tmp_path / "out")))


def test_a_corrupt_removal_log_stops_the_bank_loading(tmp_path: Path) -> None:
    world = forget_world(tmp_path)
    (world["bank"] / "removals.jsonl").write_text("not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="append-only"):
        ClipBank(world["bank"])
