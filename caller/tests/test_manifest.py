"""The run manifest: every result tied to the rig, scenarios, clips and target behind it."""

import json
from pathlib import Path
from typing import Any

import pytest
from test_post_merge_review import write_clips

from synthiq_caller import cli, manifest
from synthiq_caller.clips import ClipBank
from synthiq_caller.jsonio import write_json

LOOKUP: dict[str, Any] = {
    "name": "lookup",
    "mutates": "none",
    "persona": {"facts": {"date_of_birth": "2002-02-20"}},
    "steps": [
        {"when": "how can i help", "clips": ["opener.wav"]},
        {"when": "date of birth", "clips": ["dob.wav"], "field": "date_of_birth"},
    ],
    "hangup_after": "goodbye",
    "max_seconds": 20,
    "expect": {"tools": [{"name_contains": "check"}], "fields": ["date_of_birth"]},
}
SCRIPT = {
    "lines": [
        {"text": "Hi, how can I help?", "speech_s": 0.3},
        {"text": "Could I take your date of birth?", "speech_s": 0.3, "hears": "20 Feb 2002"},
        "Thank you, goodbye.",
    ],
    "trace": {
        "tool_calls": [
            {
                "name": "patient_check",
                "arguments": {"dateOfBirth": "2002-02-20"},
                "result": {"success": True},
                "ok": True,
            }
        ]
    },
}


def rehearse(root: Path, *extra: str, scenario: dict[str, Any] | None = None) -> Path:
    """One rehearsal batch into ``root/runs``; the set, the bank and the script live beside it."""
    bank = write_clips(root / "bank")
    (root / "set").mkdir(exist_ok=True)
    text = json.dumps(scenario or LOOKUP)
    (root / "set" / "lookup.json").write_text(text, encoding="utf-8")
    (root / "agent.json").write_text(json.dumps(SCRIPT), encoding="utf-8")
    out = root / "runs"
    arguments = ["batch", str(root / "set"), "--leg", "fake", "--clips", str(bank)]
    arguments += ["--out", str(out), "--settle", "0", "--fake-agent", str(root / "agent.json")]
    assert cli.main([*arguments, *extra]) == 0
    return out


def stored(directory: Path, name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((directory / name).read_text(encoding="utf-8"))
    return loaded


def test_a_batch_writes_down_what_it_was_made_of(tmp_path: Path) -> None:
    out = rehearse(tmp_path, "--target-label", "test target", "--target-version", "build 7")

    entries = stored(out, "manifest.json")["entries"]
    assert [e["id"] for e in entries] == ["b001"]
    entry = entries[0]
    assert entry["kind"] == "batch" and entry["rehearsal"] is True
    assert entry["started"] and entry["ended"] and entry["started"] <= entry["ended"]
    assert entry["argv"][0] == "batch" and "--target-label" in entry["argv"]
    assert entry["seed"] is None and "not shuffled" in entry["order"]
    # The rig: a version, and the commit of the checkout these tests run from.
    assert entry["rig"]["version"] and len(entry["rig"]["commit"]) == 40
    # The scenarios, each by hash, and the set as one.
    files = entry["scenarios"]["files"]
    assert files == {"lookup.json": manifest.file_hash(tmp_path / "set" / "lookup.json")}
    assert entry["scenarios"]["set_sha256"] == manifest.set_hash(files)
    # The clips: the bank's own sheet, and the audio the scenarios name.
    bank = entry["clips"]["banks"][0]
    assert bank["directory"] == "bank" and len(bank["manifest_sha256"]) == 64
    assert bank["overrides_sha256"] == "" and bank["clips"] == 5
    assert entry["clips"]["played"] == 2 and entry["clips"]["unavailable"] == []
    # The target, as the operator stated it.
    target = entry["target"]
    assert (target["label"], target["version"], target["leg"]) == ("test target", "build 7", "fake")
    # And the run says which entry it belongs to.
    assert stored(out, "run-001.json")["batch"] == "b001"


def test_a_second_batch_in_the_same_directory_is_its_own_entry(tmp_path: Path) -> None:
    out = rehearse(tmp_path)
    rehearse(tmp_path)

    assert [e["id"] for e in stored(out, "manifest.json")["entries"]] == ["b001", "b002"]
    assert stored(out, "run-001.json")["batch"] == "b001"
    assert stored(out, "run-002.json")["batch"] == "b002"
    assert manifest.mixed([out]) == {}


def test_a_text_file_has_one_hash_whatever_its_line_endings(tmp_path: Path) -> None:
    (tmp_path / "unix.json").write_bytes(b'{\n "a": 1\n}\n')
    (tmp_path / "windows.json").write_bytes(b'{\r\n "a": 1\r\n}\r\n')
    (tmp_path / "other.json").write_bytes(b'{\n "a": 2\n}\n')

    assert manifest.file_hash(tmp_path / "unix.json") == manifest.file_hash(
        tmp_path / "windows.json"
    )
    assert manifest.file_hash(tmp_path / "unix.json") != manifest.file_hash(tmp_path / "other.json")


def test_changed_audio_changes_the_clip_hash_and_a_missing_clip_is_listed(tmp_path: Path) -> None:
    folder = write_clips(tmp_path / "bank")
    before = manifest.clip_identity(ClipBank(folder), ["opener.wav", "dob.wav", "nowhere.wav"])
    assert before["played"] == 2 and before["unavailable"] == ["nowhere.wav"]

    (folder / "dob.wav").write_bytes((folder / "dob.wav").read_bytes() + b"\0\0")
    after = manifest.clip_identity(ClipBank(folder), ["opener.wav", "dob.wav", "nowhere.wav"])
    assert after["played_sha256"] != before["played_sha256"]
    assert after["banks"][0]["manifest_sha256"] == before["banks"][0]["manifest_sha256"]

    # A person's override changes what the bank is, without touching the audio.
    write_json(
        folder / "overrides.json",
        {
            "clips": {
                "dob": {"label": "the twentieth", "reason": "relabelled", "date": "2026-11-25"}
            }
        },
    )
    relabelled = manifest.clip_identity(ClipBank(folder), ["opener.wav", "dob.wav"])
    assert len(relabelled["banks"][0]["overrides_sha256"]) == 64


def test_the_commit_can_be_stated_when_there_is_no_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYNTHIQ_RIG_COMMIT", "release-2026-11-25")
    assert manifest.rig_identity()["commit"] == "release-2026-11-25"


def test_runs_made_before_there_was_a_manifest_are_unrecorded_not_mixed(tmp_path: Path) -> None:
    out = rehearse(tmp_path)
    (out / "manifest.json").unlink()

    assert manifest.load(out) is None
    assert manifest.run_hashes(out) == {"lookup.json": {manifest.UNRECORDED: 1}}
    assert manifest.mixed([out]) == {}


def test_regrade_refuses_runs_made_against_two_versions_of_a_scenario_unless_told_to(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = rehearse(tmp_path)
    first = manifest.file_hash(tmp_path / "set" / "lookup.json")
    rehearse(tmp_path, scenario={**LOOKUP, "max_seconds": 25})
    second = manifest.file_hash(tmp_path / "set" / "lookup.json")
    assert manifest.mixed([out]) == {"lookup.json": sorted([first, second])}

    regrade = ["regrade", str(out), "--scenarios", str(tmp_path / "set")]
    assert cli.main(regrade) == 2
    printed = capsys.readouterr().out
    assert "regrade refused" in printed and "--allow-mixed-scenarios" in printed
    assert [e["kind"] for e in stored(out, "manifest.json")["entries"]] == ["batch", "batch"]

    # Told to, it grades every stored call against the scenarios as they are now,
    # and from then on they all answer to that one version.
    assert cli.main([*regrade, "--allow-mixed-scenarios"]) == 0
    entries = stored(out, "manifest.json")["entries"]
    assert [e["kind"] for e in entries] == ["batch", "batch", "regrade"]
    assert entries[2]["scenarios"]["files"] == {"lookup.json": second}
    assert manifest.run_hashes(out) == {"lookup.json": {second: 2}}
    assert manifest.mixed([out]) == {}


def test_a_batch_placed_after_a_regrade_answers_to_its_own_scenarios(tmp_path: Path) -> None:
    out = rehearse(tmp_path)
    first = manifest.file_hash(tmp_path / "set" / "lookup.json")
    assert cli.main(["regrade", str(out), "--scenarios", str(tmp_path / "set")]) == 0
    rehearse(tmp_path, scenario={**LOOKUP, "max_seconds": 25})
    second = manifest.file_hash(tmp_path / "set" / "lookup.json")

    # The regrade covered the first call only: the later call is not pulled back to it.
    assert manifest.run_hashes(out) == {"lookup.json": {first: 1, second: 1}}
    with pytest.raises(manifest.MixedScenarios, match="lookup.json"):
        manifest.refuse_mixed([out])
    assert manifest.refuse_mixed([out], allow=True) == {"lookup.json": sorted([first, second])}


def test_two_directories_of_the_same_scenarios_are_one_test_and_of_different_ones_are_not(
    tmp_path: Path,
) -> None:
    monday = rehearse(_fresh(tmp_path, "mon"))
    tuesday = rehearse(_fresh(tmp_path, "tue"))
    assert manifest.mixed([monday, tuesday]) == {}

    changed = rehearse(_fresh(tmp_path, "wed"), scenario={**LOOKUP, "max_seconds": 25})
    assert list(manifest.mixed([monday, changed])) == ["lookup.json"]


def _fresh(root: Path, name: str) -> Path:
    (root / name).mkdir()
    return root / name


@pytest.mark.parametrize(
    ("leg", "rehearsal"), [("elevenlabs-socket", False), ("phone", False), ("fake", True)]
)
def test_the_manifest_records_how_the_calls_reached_the_agent(
    tmp_path: Path, leg: str, rehearsal: bool
) -> None:
    import argparse

    bank = ClipBank(write_clips(tmp_path / "bank"))
    (tmp_path / "set").mkdir()
    args = argparse.Namespace(leg=leg, repeats=1, allow_mutating="none", no_trace=False)
    manifest.open_for(args, tmp_path / "runs", tmp_path / "set", [], [], bank)

    (entry,) = stored(tmp_path / "runs", "manifest.json")["entries"]
    assert entry["target"]["leg"] == leg and entry["rehearsal"] is rehearsal
