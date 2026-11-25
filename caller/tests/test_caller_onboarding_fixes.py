"""Onboarding fixes: one copy of each manifest column, forget that blanks what was said,
one region mapping, and a doctor that says which leg is ready.

No test here reaches the network; names and words are neutral placeholders."""

from __future__ import annotations

import argparse
import contextlib
import copy
import csv
import json
import re
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_capture_ingest import CONVERSATION, KEY, Platform, captured, no_ffmpeg  # noqa: F401
from test_clip_consent import FULL, OP, forget_args, forget_world, write_bank
from test_forget_review import SENTINEL, realistic_result

from synthiq_caller import cli, onboarding, regions
from synthiq_caller.clipbank.from_call import read_manifest
from synthiq_caller.clips import ClipBank
from synthiq_caller.ingest import MANIFEST_COLUMNS, ingest
from synthiq_caller.manifestcsv import ManifestError, read_rows

DOCS = Path(__file__).resolve().parents[1] / "docs"


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a test reached for the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


# --- 1. one copy of each manifest column ----------------------------------------------


def header(path: Path) -> list[str]:
    with path.open(encoding="utf-8", newline="") as handle:
        return next(csv.reader(handle))


def test_ingest_writes_each_manifest_column_once(tmp_path: Path) -> None:
    case = captured(tmp_path)
    ingest(case)
    names = header(case / "clips" / "manifest.csv")
    assert len(names) == len(set(names)), [n for n in names if names.count(n) > 1]
    assert {"provenance", "lawful_basis", "consent_ref"} <= set(names)
    assert set(MANIFEST_COLUMNS) <= set(names)


def duplicated_bank(
    tmp_path: Path, first: str, second: str, basis: tuple[str, str] = ("", "")
) -> Path:
    """A manifest in the old shape: provenance, lawful_basis and consent_ref each twice."""
    bank = tmp_path / "bank"
    bank.mkdir()
    (bank / "c1.wav").write_bytes(b"RIFF")
    rows = [
        "clip,said,checked_by_human,provenance,lawful_basis,consent_ref,provenance,lawful_basis,consent_ref",
        f"c1,hello,x,{first},{basis[0]},{basis[1]},{second},,",
    ]
    (bank / "manifest.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return bank


def test_a_value_in_the_first_copy_is_not_ignored(tmp_path: Path) -> None:
    bank = duplicated_bank(tmp_path, "synthetic", "")
    assert ClipBank(bank).synthetic("c1")
    assert read_manifest(bank)[0]["provenance"] == "synthetic"


def test_a_value_in_the_last_copy_is_not_ignored_either(tmp_path: Path) -> None:
    bank = duplicated_bank(tmp_path, "", "synthetic")
    assert ClipBank(bank).synthetic("c1")


def test_copies_that_disagree_refuse_instead_of_choosing(tmp_path: Path) -> None:
    bank = duplicated_bank(tmp_path, "real", "synthetic")
    with pytest.raises(ManifestError, match="provenance"):
        ClipBank(bank)
    with pytest.raises(ManifestError, match="provenance"):
        read_manifest(bank)
    with pytest.raises(ManifestError, match="provenance"):
        read_rows(bank / "manifest.csv")


def test_a_command_refuses_a_conflicting_manifest_in_words(tmp_path: Path) -> None:
    bank = duplicated_bank(tmp_path, "real", "synthetic")
    args = argparse.Namespace(clips=str(bank), clips_extra=[], allow_missing_consent=False)
    with pytest.raises(SystemExit, match="appears 2 times with different values"):
        cli._bank(args)


def test_forget_with_a_conflicting_manifest_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bank = duplicated_bank(tmp_path, "real", "synthetic")
    before = (bank / "manifest.csv").read_bytes()
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    code = cli.main(
        ["forget", "c1", "--clips", str(bank), "--scenarios", str(scenarios), "--operator", OP]
    )
    assert code == 2
    assert "nothing was changed" in capsys.readouterr().out
    assert (bank / "manifest.csv").read_bytes() == before and (bank / "c1.wav").exists()


def test_no_clip_becomes_consented_by_a_merge(tmp_path: Path) -> None:
    # A filled consent copy plus an empty one is read as filled; two different ones refuse.
    bank = duplicated_bank(tmp_path, "real", "", basis=("consent", "consent-0042"))
    row = read_rows(bank / "manifest.csv")[1][0]
    assert (row["lawful_basis"], row["consent_ref"]) == ("consent", "consent-0042")
    # An empty pair stays empty: nothing is filled in on a person's behalf.
    (tmp_path / "e").mkdir()
    empty = duplicated_bank(tmp_path / "e", "real", "")
    row = read_rows(empty / "manifest.csv")[1][0]
    assert row["lawful_basis"] == "" and row["consent_ref"] == ""


def test_forget_rewrites_a_merged_manifest_with_one_copy_of_each_column(
    tmp_path: Path,
) -> None:
    bank = write_bank(
        tmp_path / "b", {"a1": dict(FULL), "b1": {**FULL, "speaker_ref": "spk_zzzzzzzz1111"}}
    )
    text = (bank / "manifest.csv").read_text(encoding="utf-8").splitlines()
    text[0] += ",provenance"  # the old shape: a second, empty copy
    text[1:] = [line + "," for line in text[1:]]
    (bank / "manifest.csv").write_text("\n".join(text) + "\n", encoding="utf-8")
    scenarios = tmp_path / "s"
    scenarios.mkdir()
    assert (
        cli.main(
            ["forget", "a1", "--clips", str(bank), "--scenarios", str(scenarios), "--operator", OP]
        )
        == 0
    )
    names = header(bank / "manifest.csv")
    assert len(names) == len(set(names))
    assert read_rows(bank / "manifest.csv")[1][0]["clip"] == "b1"


# --- 2. forget blanks what was said, and keeps the checks -----------------------------

WORDS = "Quillfeather Zanzibar"


def case_with_words(tmp_path: Path) -> tuple[Path, str]:
    """An ingested case whose second caller turn is ``WORDS``, with the words also placed in
    the persona, an expectation and a stored result."""
    conversation = copy.deepcopy(CONVERSATION)
    for turn in conversation["transcript"]:
        if turn["message"] == "Sam Example.":
            turn["message"] = WORDS
    case = captured(tmp_path, Platform(conversation=conversation))
    ingest(case)
    path = case / "scenarios" / "scenario.json"
    scenario = json.loads(path.read_text(encoding="utf-8"))
    step = next(s for s in scenario["steps"] if s["intended"] == WORDS)
    step["field"] = "name"
    scenario["persona"] = {"facts": {"name": WORDS, "unrelated": "kept"}}
    scenario["expect"] = {
        "tools": [{"name_contains": "lookup", "arguments": {"q": WORDS}}],
        "fields": [{"field": "name", "tool_arg": "q", "equals": WORDS}],
    }
    path.write_text(json.dumps(scenario, indent=2), encoding="utf-8")
    clip = step["clips"][0]
    return case, re.sub(r"\.wav$", "", clip)


def everything(root: Path) -> str:
    return "\n".join(
        p.read_bytes().decode("utf-8", errors="replace") for p in root.rglob("*") if p.is_file()
    )


def test_forgetting_a_clip_blanks_its_words_in_the_case_scenario_and_keeps_the_check(
    tmp_path: Path,
) -> None:
    case, clip = case_with_words(tmp_path)
    assert WORDS in everything(case)
    code = cli.main(["forget", clip, "--clips", str(case / "clips"), "--operator", OP])
    assert code == 0
    assert WORDS not in everything(case)
    scenario = json.loads((case / "scenarios" / "scenario.json").read_text(encoding="utf-8"))
    step = next(s for s in scenario["steps"] if s["clips"] == [f"missing/{clip}"])
    assert step["field"] == "name" and step["intended"] == f"[removed: {clip}]"
    # the names stay, the value is blanked, and what is not the clip's words is untouched
    assert scenario["persona"]["facts"] == {"name": f"[removed: {clip}]", "unrelated": "kept"}
    assert scenario["expect"]["fields"][0]["field"] == "name"
    assert scenario["expect"]["fields"][0]["equals"] == f"[removed: {clip}]"
    assert scenario["expect"]["tools"][0]["name_contains"] == "lookup"
    assert any("removed" in m for m in scenario["missing_clips"])


def test_a_forget_run_twice_changes_nothing_the_second_time(tmp_path: Path) -> None:
    case, clip = case_with_words(tmp_path)
    args = ["forget", clip, "--clips", str(case / "clips"), "--operator", OP]
    assert cli.main(args) == 0
    after_one = everything(case)
    assert cli.main(args) == 0
    assert everything(case) == after_one
    scenario = json.loads((case / "scenarios" / "scenario.json").read_text(encoding="utf-8"))
    assert len(scenario["missing_clips"]) == len(set(scenario["missing_clips"]))


def test_a_scenario_pointed_at_missing_by_an_older_forget_is_finished(tmp_path: Path) -> None:
    """An interrupted (or older) removal left ``missing/<clip>`` with the words still in it."""
    world = forget_world(tmp_path)
    one = world["scenarios"] / "one.json"
    data = json.loads(one.read_text(encoding="utf-8"))
    data["steps"][0]["clips"] = ["missing/a1"]
    data["steps"][0]["intended"] = WORDS
    one.write_text(json.dumps(data), encoding="utf-8")
    assert cli.main(forget_args(world, "a1", "--reason", "consent_withdrawn")) == 0
    assert WORDS not in one.read_text(encoding="utf-8")
    assert json.loads(one.read_text(encoding="utf-8"))["steps"][0]["intended"] == "[removed: a1]"


def test_the_words_survive_nowhere_after_a_forget_across_every_kind_of_file(
    tmp_path: Path,
) -> None:
    """Scenarios, runs, a batch summary, a chain result, manifests, overrides and the case."""
    case, clip = case_with_words(tmp_path)
    # the words, as the tool writes them, in the places a stored run keeps them
    results = tmp_path / "runs"
    results.mkdir()
    run = realistic_result(clip)
    text = json.dumps(run).replace(SENTINEL, WORDS)
    (results / "run-001.json").write_text(text, encoding="utf-8")
    (results / "batch.json").write_text(
        json.dumps({"runs": [{"file": "run-001.json", "outcome": "failed"}], "ran": 1}),
        encoding="utf-8",
    )
    manifest = case / "clips" / "manifest.csv"
    assert WORDS in manifest.read_text(encoding="utf-8")
    (case / "clips" / "overrides.json").write_text(
        json.dumps({"clips": {clip: {"label": WORDS, "reason": "heard", "date": "2026-11-01"}}}),
        encoding="utf-8",
    )
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    (scenarios / "copy.json").write_text(
        (case / "scenarios" / "scenario.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    code = cli.main(
        [
            "forget",
            clip,
            "--clips",
            str(case / "clips"),
            "--scenarios",
            str(scenarios),
            "--results",
            str(results),
            "--operator",
            OP,
        ]
    )
    assert code == 0
    assert WORDS not in everything(tmp_path), [
        str(p.relative_to(tmp_path))
        for p in tmp_path.rglob("*")
        if p.is_file() and WORDS in p.read_text("utf-8", "replace")
    ]
    # a second pass, with a stored run kept: still nowhere
    assert cli.main(
        [
            "forget",
            clip,
            "--clips",
            str(case / "clips"),
            "--results",
            str(results),
            "--operator",
            OP,
        ]
    ) in (0, 1)
    assert WORDS not in everything(tmp_path)
    # the checks' names and outcomes stay in the stored run
    kept = json.loads((results / "run-001.json").read_text(encoding="utf-8"))
    assert [c["label"] for c in kept["grade"]["checks"]][:1] == [
        "field name reaches a tool as said"
    ]
    assert kept["grade"]["checks"][0]["passed"] is False


def test_the_description_says_what_is_blanked_and_what_is_not() -> None:
    from synthiq_caller import forget

    plan = forget.Plan("c1", ["c1"], [])
    plan.scenarios = {Path("s.json"): ["c1"]}
    text = "\n".join(forget.describe(plan, dry=True, keep_text=False, finishing=[]))
    assert "blanked" in text


# --- 3. one region mapping -----------------------------------------------------------


def test_one_mapping_serves_every_module_and_names_each_region() -> None:
    assert set(regions.REGIONS) == {"global", "us", "eu", "in", "sg"}
    assert onboarding.REGIONS is regions.REGIONS
    from synthiq_caller.legs import elevenlabs_socket
    from synthiq_caller.traces import elevenlabs, lookup

    assert elevenlabs.API == lookup.ELEVENLABS_API == elevenlabs_socket.API == regions.GLOBAL_API
    assert regions.REGIONS["global"] == regions.GLOBAL_API


def test_an_unknown_region_is_an_error_naming_the_choices() -> None:
    with pytest.raises(regions.UnknownRegion, match="mars"):
        regions.api_for("mars")
    for flag in ("api_from_args",):
        with pytest.raises(SystemExit, match="unknown region 'mars'"):
            getattr(regions, flag)(argparse.Namespace(region="mars"), {})


def test_the_setting_is_the_flag_then_the_environment_then_global() -> None:
    assert regions.region_setting("eu", {"ELEVENLABS_REGION": "in"}) == "eu"
    assert regions.region_setting("", {"ELEVENLABS_REGION": "in"}) == "in"
    assert regions.region_setting("", {}) == "global"
    namespace = argparse.Namespace(region="")
    assert regions.api_from_args(namespace, {"ELEVENLABS_REGION": "sg"}) == regions.REGIONS["sg"]


class Recorder:
    """Stands in for a client class: keeps the address it was given, opens nothing."""

    made: list[Any] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args, self.kwargs = args, kwargs
        Recorder.made.append(self)


@pytest.mark.parametrize("region", ["eu", "sg", "us"])
def test_run_batch_and_regrade_readers_use_the_region(
    region: str, monkeypatch: pytest.MonkeyPatch, no_network: None
) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", KEY)
    Recorder.made.clear()
    monkeypatch.setattr("synthiq_caller.traces.elevenlabs.ElevenLabsTraceReader", Recorder)
    args = argparse.Namespace(trace="elevenlabs", env_file=None, region=region)
    cli._trace_reader(args)
    assert Recorder.made[-1].kwargs["api"] == regions.REGIONS[region]

    monkeypatch.setattr("synthiq_caller.traces.lookup.ElevenLabsNumberTimeFinder", Recorder)
    reread = argparse.Namespace(
        leg="phone",
        trace="elevenlabs",
        env_file=None,
        region=region,
        agent="agent_x",
        trace_window=60.0,
        directory=".",
    )
    cli._reread_reader(reread)
    assert [m.kwargs["api"] for m in Recorder.made[-2:]] == [regions.REGIONS[region]] * 2

    monkeypatch.setattr("synthiq_caller.legs.elevenlabs_socket.ElevenLabsSocketLeg", Recorder)
    cli._build(
        argparse.Namespace(leg="elevenlabs-socket", agent="agent_x", env_file=None, region=region)
    )
    assert Recorder.made[-1].kwargs["api"] == regions.REGIONS[region]


def test_the_environment_setting_reaches_the_reader(
    monkeypatch: pytest.MonkeyPatch, no_network: None
) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", KEY)
    monkeypatch.setenv("ELEVENLABS_REGION", "in")
    Recorder.made.clear()
    monkeypatch.setattr("synthiq_caller.traces.elevenlabs.ElevenLabsTraceReader", Recorder)
    cli._trace_reader(argparse.Namespace(trace="elevenlabs", env_file=None, region=""))
    assert Recorder.made[-1].kwargs["api"] == regions.REGIONS["in"]


def test_the_phone_batch_trace_lookup_uses_the_region(
    monkeypatch: pytest.MonkeyPatch, no_network: None
) -> None:
    from synthiq_caller import phone_batch

    Recorder.made.clear()
    monkeypatch.setattr("synthiq_caller.traces.elevenlabs.ElevenLabsTraceReader", Recorder)
    monkeypatch.setattr("synthiq_caller.traces.lookup.ElevenLabsNumberTimeFinder", Recorder)
    monkeypatch.setattr(phone_batch, "check_public_url", lambda url: url)
    monkeypatch.setattr(phone_batch, "build_transcriber", lambda args, env: None)
    monkeypatch.setattr(phone_batch, "checked_target", lambda *a: ("+10000000000",))
    monkeypatch.setattr(phone_batch.Credentials, "from_env", classmethod(lambda cls, env: object()))
    monkeypatch.setattr(phone_batch, "TwilioRest", Recorder)
    monkeypatch.setattr(phone_batch, "PhoneExchange", Recorder)
    args = argparse.Namespace(
        to="+10000000000",
        from_number="+10000000001",
        public_url="https://example.invalid",
        max_parallel=1,
        allowed_numbers="",
        no_trace=False,
        trace="elevenlabs",
        agent="agent_x",
        trace_window=60.0,
        trace_ignore_caller_number=False,
        host="127.0.0.1",
        port=0,
        no_record=True,
        region="eu",
    )
    with contextlib.suppress(Exception):  # only the readers built before any failure matter
        phone_batch.open_phone_batch(args, {"ELEVENLABS_API_KEY": KEY}, Path("."))
    apis = [m.kwargs.get("api") for m in Recorder.made if "api" in m.kwargs]
    assert apis == [regions.REGIONS["eu"]] * 2


def test_the_cli_takes_region_on_every_command_that_reaches_the_platform(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for command in ("run", "batch", "regrade", "capture"):
        with pytest.raises(SystemExit):
            cli.main([command, "--help"])
        assert "--region" in capsys.readouterr().out, command


def test_an_unknown_region_on_capture_fails_before_any_request(
    capsys: pytest.CaptureFixture[str], no_network: None
) -> None:
    code = cli.main(
        [
            "capture",
            "--trace",
            "elevenlabs",
            "--conversation",
            "c",
            "--out",
            "x",
            "--region",
            "mars",
        ]
    )
    assert code == 2 and "unknown region" in capsys.readouterr().out


def test_the_documented_us_address_is_marked_as_not_from_the_docs_page() -> None:
    source = (Path(regions.__file__)).read_text(encoding="utf-8")
    assert "not confirmed by the documentation" in " ".join(source.split())


# --- 4. doctor: what the fake leg needs, and what a live leg needs ----------------------


def test_doctor_in_a_folder_without_a_key_passes_the_fake_leg_and_says_the_live_one_is_not_ready(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    no_network: None,
) -> None:
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_REGION", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: "/bin/ffmpeg")  # CI has none
    assert cli.main(["init", str(tmp_path / "t")]) == 0
    capsys.readouterr()
    assert cli.main(["doctor", str(tmp_path / "t")]) == 0
    out = capsys.readouterr().out
    assert "fake leg: ready" in out
    assert "live leg: NOT READY (key)" in out
    assert "live leg: ready" not in out


def test_doctor_live_fails_when_the_live_leg_is_not_ready(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, no_network: None
) -> None:
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert cli.main(["doctor", "--live"]) == 1
    assert "--live was given" in capsys.readouterr().out


def test_doctor_fails_the_fake_leg_on_a_broken_folder(tmp_path: Path, no_network: None) -> None:
    assert cli.main(["doctor", str(tmp_path), "--fake-only"]) == 1


def test_doctor_never_calls_the_live_leg_ready_for_a_check_nobody_made(
    monkeypatch: pytest.MonkeyPatch, no_network: None
) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/bin/ffmpeg")
    done = onboarding.doctor(environ={"ELEVENLABS_API_KEY": "k"})
    fake_ready, state, live_ready = onboarding.verdicts(done, fake_only=False)
    assert fake_ready and live_ready
    assert "nothing was called" in state and "address was not tried" in state
    assert state.startswith("settings present") and "ready" not in state
    # fake-only: the live leg is "not checked", never ready
    quiet = onboarding.doctor(fake_only=True, environ={})
    _, state, live_ready = onboarding.verdicts(quiet, fake_only=True)
    assert not live_ready and state == "not checked (--fake-only)"
    # a missing check keeps it not ready
    _, state, live_ready = onboarding.verdicts([], fake_only=False)
    assert not live_ready and state.startswith("NOT READY")
    # a check that failed keeps it not ready
    failed = onboarding.doctor(environ={}, region="mars")
    assert not onboarding.verdicts(failed, fake_only=False)[2]


def test_doctor_with_an_unknown_region_exits_1_even_without_live(no_network: None) -> None:
    assert cli.main(["doctor", "--region", "mars"]) == 1


def test_doctor_marks_each_live_line(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, no_network: None
) -> None:
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert re.search(r"FAIL +key .*\[live leg\]", out)


def test_a_doctor_transport_answer_is_still_marked_live() -> None:
    seen = onboarding.doctor(
        region="eu",
        check_network=True,
        environ={"ELEVENLABS_API_KEY": "k"},
        transport=httpx.MockTransport(lambda request: httpx.Response(404)),
    )
    net = next(c for c in seen if c.name == "network")
    assert net.needed_by == "live" and net.status == "ok" and "no key was sent" in net.detail


# --- the guide ---------------------------------------------------------------------


def test_the_guide_does_not_rely_on_an_alias_and_says_batch_exits_0_on_failed_runs() -> None:
    guide = (DOCS / "ONBOARDING.md").read_text(encoding="utf-8")
    assert not re.search(r"^\s*alias\b", guide, re.M)
    assert ".venv/bin" in guide
    assert "exits 0 even when runs failed" in guide and "--fail-under" in guide
    assert "live leg" in guide and "--live" in guide
