"""Capture a live call into a case directory, ingest it, and validate the result.

Everything runs on a synthetic conversation and a generated tone: no network (the
HTTP layer is a mock transport), no real audio, no names.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import math
import struct
import wave
from pathlib import Path
from typing import Any

import httpx
import pytest

from synthiq_caller import capture as capture_module
from synthiq_caller.batch import NameBudget, load_scenarios, run_batch
from synthiq_caller.capture import CaptureError, capture, sniff_audio
from synthiq_caller.cli import main
from synthiq_caller.clips import ClipBank
from synthiq_caller.ingest import IngestError, ingest, plan_turn_clips
from synthiq_caller.legs.fake import FakeLeg
from synthiq_caller.plan import dry_run
from synthiq_caller.scenario import parse_scenario
from synthiq_caller.traces.elevenlabs import ElevenLabsTraceReader
from synthiq_caller.validation import validate_directory

FIXTURES = Path(__file__).parent / "fixtures"
KEY = "xi-test-SECRET-key-0123456789"
CONVERSATION = json.loads((FIXTURES / "capture_conversation.json").read_text(encoding="utf-8"))


def tone_wav(seconds: int = 30, rate: int = 8000) -> bytes:
    """A generated 440 Hz tone: loud enough not to be flagged as silence."""
    frames = b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * n / rate)))
        for n in range(seconds * rate)
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(frames)
    return buffer.getvalue()


CUTS: list[tuple[float, float]] = []


def cut_without_ffmpeg(source: Path, start: float, end: float, target: Path, rate: int = 0) -> None:
    """Stand-in for the ffmpeg cut (CI has no ffmpeg): slice a WAV and record the span asked for."""
    CUTS.append((start, end))
    with wave.open(str(source), "rb") as wav:
        frame_rate = wav.getframerate()
        wav.setpos(min(int(start * frame_rate), wav.getnframes()))
        frames = wav.readframes(max(0, int((end - start) * frame_rate)))
    with wave.open(str(target), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(frame_rate)
        out.writeframes(frames)


@pytest.fixture(autouse=True)
def no_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    CUTS.clear()
    monkeypatch.setattr("synthiq_caller.ingest.cut", cut_without_ffmpeg)
    monkeypatch.setattr("synthiq_caller.clipbank.build.cut", cut_without_ffmpeg)


class Platform:
    """A mock of the two endpoints, recording what was asked of it."""

    def __init__(
        self,
        conversation: dict[str, Any] | None = None,
        audio_status: int = 200,
    ) -> None:
        self.conversation = conversation or CONVERSATION
        self.audio = tone_wav()
        self.audio_status = audio_status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.method == "GET"  # a capture only ever reads
        assert request.headers["xi-api-key"] == KEY
        if request.url.path.endswith("/audio"):
            # The header says mp3 and the bytes are a WAV, on purpose: bytes decide.
            return httpx.Response(
                self.audio_status, content=self.audio, headers={"content-type": "audio/mpeg"}
            )
        return httpx.Response(200, json=self.conversation)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def captured(tmp_path: Path, platform: Platform | None = None, name: str = "case") -> Path:
    out = tmp_path / name
    capture("conv_fixture_0001", out, KEY, transport=(platform or Platform()).transport)
    return out


def tree_text(root: Path) -> str:
    return "\n".join(
        p.read_bytes().decode("utf-8", errors="replace") for p in root.rglob("*") if p.is_file()
    )


@pytest.mark.parametrize(
    ("data", "extension"),
    [
        (b"RIFF\x00\x00\x00\x00WAVEfmt ", "wav"),
        (b"ID3\x04\x00\x00", "mp3"),
        (b"\xff\xfb\x90\x00", "mp3"),
        (b"OggS\x00\x02", "ogg"),
        (b"fLaC\x00\x00", "flac"),
        (b"\x00\x00\x00\x20ftypM4A ", "m4a"),
        (b"not audio at all", "bin"),
        (b"", "bin"),
    ],
)
def test_the_audio_type_comes_from_its_bytes(data: bytes, extension: str) -> None:
    assert sniff_audio(data) == extension


def test_capture_writes_the_whole_case_and_trusts_bytes_not_headers(tmp_path: Path) -> None:
    out = captured(tmp_path)
    assert sorted(p.name for p in out.iterdir()) == [
        "audio.wav",
        "capture.json",
        "conversation.json",
        "trace.json",
        "transcript.txt",
    ]
    assert json.loads((out / "conversation.json").read_text(encoding="utf-8")) == CONVERSATION
    trace = json.loads((out / "trace.json").read_text(encoding="utf-8"))
    assert [c["name"] for c in trace["tool_calls"]] == ["lookup_record"]
    transcript = (out / "transcript.txt").read_text(encoding="utf-8")
    assert "[00:12] user: Sam Example." in transcript
    assert "not the truth" in transcript
    meta = json.loads((out / "capture.json").read_text(encoding="utf-8"))
    assert meta["audio"]["captured"] is True
    assert meta["audio"]["file"] == "audio.wav"
    assert len(meta["audio"]["sha256"]) == 64
    assert meta["contains_personal_data"] is True


def test_the_key_never_reaches_a_file_or_the_screen(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = Platform()
    monkeypatch.setenv("ELEVENLABS_API_KEY", KEY)
    original = capture_module.ElevenLabsTraceReader

    def with_mock(*args: Any, **kwargs: Any) -> ElevenLabsTraceReader:
        return original(*args, **{**kwargs, "transport": platform.transport})

    monkeypatch.setattr(capture_module, "ElevenLabsTraceReader", with_mock)
    out = tmp_path / "case"
    argv = ["capture", "--trace", "elevenlabs", "--conversation", "conv_fixture_0001"]
    assert main([*argv, "--out", str(out)]) == 0
    shown = capsys.readouterr()
    assert KEY not in shown.out + shown.err
    assert KEY not in tree_text(out)
    assert main(["ingest", str(out)]) == 0
    shown = capsys.readouterr()
    assert KEY not in shown.out + shown.err + tree_text(out)


def test_an_error_that_mentions_the_key_is_scrubbed(tmp_path: Path) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach host with {KEY}")

    with pytest.raises(CaptureError) as caught:
        capture("conv_x", tmp_path / "c", KEY, transport=httpx.MockTransport(boom))
    assert KEY not in str(caught.value)
    assert not (tmp_path / "c").exists()


def test_a_missing_key_is_refused_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    argv = ["capture", "--trace", "elevenlabs", "--conversation", "c", "--out", str(tmp_path / "x")]
    assert main(argv) == 2
    assert not (tmp_path / "x").exists()


def test_an_existing_case_is_not_overwritten_without_force(tmp_path: Path) -> None:
    out = captured(tmp_path)
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    again = Platform()
    with pytest.raises(CaptureError, match="--force"):
        capture("conv_fixture_0001", out, KEY, transport=again.transport)
    assert again.requests == []  # refused before asking the platform for anything
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before


def test_force_replaces_a_capture_and_leaves_other_files(tmp_path: Path) -> None:
    out = captured(tmp_path)
    (out / "notes.txt").write_text("mine", encoding="utf-8")
    other = {**CONVERSATION, "agent_id": "agent_other"}
    capture("conv_fixture_0001", out, KEY, force=True, transport=Platform(other).transport)
    replaced = json.loads((out / "conversation.json").read_text(encoding="utf-8"))
    assert replaced["agent_id"] == "agent_other"
    assert (out / "notes.txt").read_text(encoding="utf-8") == "mine"


def test_a_call_without_audio_is_said_to_be_without_audio(tmp_path: Path) -> None:
    platform = Platform({**CONVERSATION, "has_audio": False})
    out = tmp_path / "case"
    document = capture("conv_fixture_0001", out, KEY, transport=platform.transport)
    assert document["audio"]["captured"] is False
    assert "no audio" in document["audio"]["reason_missing"]
    assert not list(out.glob("audio.*"))
    assert not any(r.url.path.endswith("/audio") for r in platform.requests)
    assert (out / "transcript.txt").exists()
    with pytest.raises(IngestError, match="no audio"):
        ingest(out)


def test_a_refused_audio_request_is_recorded_not_hidden(tmp_path: Path) -> None:
    out = tmp_path / "case"
    document = capture("c", out, KEY, transport=Platform(audio_status=404).transport)
    assert document["audio"]["captured"] is False
    assert "404" in document["audio"]["reason_missing"]


def test_a_conversation_that_never_finishes_is_an_error(tmp_path: Path) -> None:
    platform = Platform({**CONVERSATION, "status": "processing"})
    with pytest.raises(CaptureError):
        capture("c", tmp_path / "case", KEY, wait_s=0.0, transport=platform.transport)
    assert not (tmp_path / "case").exists()


def test_the_trace_reader_still_parses_what_it_fetches() -> None:
    reader = ElevenLabsTraceReader(KEY, transport=Platform().transport)
    trace = asyncio.run(reader.read("conv_fixture_0001"))
    assert [c.name for c in trace.tool_calls] == ["lookup_record"]


def read_manifest(case: Path) -> list[dict[str, str]]:
    with (case / "clips" / "manifest.csv").open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_ingest_cuts_the_callers_turns_and_marks_every_clip_unverified(tmp_path: Path) -> None:
    case = captured(tmp_path)
    result = ingest(case)
    assert result["clips"] == 3
    rows = read_manifest(case)
    assert [r["category"] for r in rows] == ["opener", "name", "body_part"]
    for row in rows:
        assert row["label_status"] == "unverified"
        assert row["provenance"] == "real-call"
        assert row["checked_by_human"] == ""
        assert "coarse_cut" in row["review"].split()
        assert (case / "clips" / f"{row['clip']}.wav").stat().st_size > 44
    # P8 owns consent, retention and the speaker reference: nothing here invents them.
    assert not {"consent_ref", "lawful_basis", "speaker_ref", "retain_until"} & set(rows[0])
    bank = ClipBank(case / "clips")
    names = [r["clip"] for r in rows]
    assert not any(bank.verified(n) for n in names)
    assert not any(bank.synthetic(n) for n in names)
    assert len(bank.label_doubts(names)) == 3
    # The spans handed to the cutter are the turn times, padded 0.5 s each side:
    # 4 s to the next turn at 8 s, then 12 to 15 and 20 to 24.
    assert CUTS == [(3.5, 8.5), (11.5, 15.5), (19.5, 24.5)]


def test_the_cut_is_padded_and_the_turn_ends_where_the_next_begins() -> None:
    turns = [
        {"role": "agent", "t": 0.0, "text": "Your name?", "interrupted": False},
        {"role": "user", "t": 4.0, "text": "Sam.", "interrupted": False},
        {"role": "agent", "t": 8.0, "text": "Thanks.", "interrupted": False},
    ]
    ((_, plan),) = plan_turn_clips(turns, pad_s=0.5, duration_s=30.0)
    assert (plan.start, plan.end) == (3.5, 8.5)
    assert plan.review == ["coarse_cut", "mixed_audio"]


def test_a_caller_who_cut_the_agent_off_is_flagged_as_overlapping() -> None:
    turns = [
        {"role": "agent", "t": 0.0, "text": "Your name is", "interrupted": True},
        {"role": "user", "t": 2.0, "text": "Sam.", "interrupted": False},
        {"role": "agent", "t": 2.0, "text": "Thanks.", "interrupted": False},
    ]
    ((_, plan),) = plan_turn_clips(turns, pad_s=0.5, duration_s=30.0)
    assert "overlaps_agent" in plan.review
    assert plan.end > plan.start


def test_the_skeleton_is_the_agents_questions_in_order_with_nothing_expected(
    tmp_path: Path,
) -> None:
    case = captured(tmp_path)
    ingest(case)
    data = json.loads((case / "scenarios" / "scenario.json").read_text(encoding="utf-8"))
    assert data["expect"] == {"tools": [], "fields": []}
    assert data["persona"] == {"facts": {}}  # nothing taken from a machine transcript
    assert data["mutates"] == "unknown"
    assert len(data["steps"]) == 3
    patterns = [s["when"] for s in data["steps"]]
    assert "help" in patterns[0]
    assert "full" in patterns[1] and "name" in patterns[1]
    assert "body" in patterns[2]
    assert all(s["max_uses"] == 1 for s in data["steps"])
    scenario = load_scenarios(case / "scenarios")[0][
        1
    ]  # it parses, and each pattern finds its own question
    assert scenario.steps[1].when.search("Of course. What is your full name?")
    assert not scenario.steps[1].when.search("Which part of the body is it?")
    assert not scenario.states_expectations()


def test_ingest_refuses_to_overwrite_and_force_cuts_again(tmp_path: Path) -> None:
    case = captured(tmp_path)
    ingest(case)
    scenario = case / "scenarios" / "scenario.json"
    scenario.write_text(scenario.read_text(encoding="utf-8") + " ", encoding="utf-8")
    edited = scenario.read_bytes()
    with pytest.raises(IngestError, match="--force"):
        ingest(case)
    assert scenario.read_bytes() == edited
    assert ingest(case, force=True)["clips"] == 3


def test_ingest_of_something_that_is_not_a_capture_is_refused(tmp_path: Path) -> None:
    with pytest.raises(IngestError, match="not a capture"):
        ingest(tmp_path)


def test_ingest_with_a_word_level_transcript_uses_the_ordinary_build(tmp_path: Path) -> None:
    case = captured(tmp_path)
    words: list[dict[str, Any]] = []
    for speaker, start, text in [
        ("a", 0.0, "Hello how can I help"),
        ("b", 4.0, "I would like information"),
        ("a", 8.0, "What is your full name"),
        ("b", 12.0, "Sam Example"),
        ("a", 15.0, "Which part of the body is it"),
        ("b", 20.0, "The left knee"),
    ]:
        for i, word in enumerate(text.split()):
            at = start + i * 0.3
            words.append(
                {"text": word, "start": at, "end": at + 0.25, "type": "word", "speaker_id": speaker}
            )
    stt = tmp_path / "words.stt.json"
    stt.write_text(json.dumps({"words": words}), encoding="utf-8")
    result = ingest(case, stt=stt)
    assert result["cut_by"] == "stt"
    rows = read_manifest(case)
    assert len(rows) == 3
    assert {r["label_status"] for r in rows} == {"unverified"}
    assert {r["provenance"] for r in rows} == {"real-call"}
    steps = json.loads((case / "scenarios" / "scenario.json").read_text(encoding="utf-8"))["steps"]
    assert len(steps) == 3


def test_validate_reports_a_skeleton_as_ungradeable_not_ok_and_not_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case = captured(tmp_path)
    ingest(case)
    rows = validate_directory(case / "scenarios", ClipBank(case / "clips"), None)
    assert len(rows) == 1
    assert not rows[0].states_expectations
    assert "UNGRADEABLE until expectations are stated" in rows[0].line()
    assert not rows[0].problem  # not an error: an unfinished file, said loudly
    code = main(["validate", str(case / "scenarios"), "--clips", str(case / "clips")])
    out = capsys.readouterr().out
    assert code == 0
    assert "UNGRADEABLE until expectations are stated" in out
    assert "1 scenario(s) UNGRADEABLE" in out


def test_validate_strict_turns_the_ungradeable_warning_into_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case = captured(tmp_path)
    ingest(case)
    args = ["validate", str(case / "scenarios"), "--clips", str(case / "clips")]
    assert main(args) == 0
    capsys.readouterr()
    assert main([*args, "--strict"]) == 1
    out = capsys.readouterr().out
    assert "ERROR" in out and "scenario.json" in out and "states no expectations" in out


def test_validate_strict_passes_a_scenario_that_states_its_expectations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case = captured(tmp_path)
    ingest(case)
    path = case / "scenarios" / "scenario.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["expect"] = {"tools": [{"name": "lookup_record"}], "fields": []}
    path.write_text(json.dumps(data), encoding="utf-8")
    args = ["validate", str(case / "scenarios"), "--clips", str(case / "clips")]
    assert main(args) == 0
    assert main([*args, "--strict"]) == 0
    assert "ERROR" not in capsys.readouterr().out


def test_strict_is_only_about_gradeability_an_unverified_label_still_passes(
    tmp_path: Path,
) -> None:
    """Captured clips are ``label_status=unverified``; --strict does not fail on that."""
    case = captured(tmp_path)
    ingest(case)
    path = case / "scenarios" / "scenario.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["expect"] = {"tools": [{"name": "lookup_record"}], "fields": []}
    path.write_text(json.dumps(data), encoding="utf-8")
    manifest_text = (case / "clips" / "manifest.csv").read_text(encoding="utf-8")
    assert "unverified" in manifest_text
    assert main(["validate", str(path.parent), "--clips", str(case / "clips"), "--strict"]) == 0


def test_stating_an_expectation_makes_it_gradeable(tmp_path: Path) -> None:
    case = captured(tmp_path)
    ingest(case)
    path = case / "scenarios" / "scenario.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["expect"] = {"tools": [{"name": "lookup_record"}], "fields": []}
    path.write_text(json.dumps(data), encoding="utf-8")
    (row,) = validate_directory(case / "scenarios", None, None)
    assert row.states_expectations
    assert "UNGRADEABLE" not in row.line()


@pytest.mark.parametrize(
    "expect",
    [
        {"tools": [{"name": "t"}]},
        {"fields": ["name"]},
        {"agent_says": ["hello"]},
        {"agent_never_says": ["bad word"]},
        {"read_back": {"k": "v"}},
        {"booking": {"weekdays": ["monday"]}},
    ],
)
def test_every_kind_of_expectation_counts(expect: dict[str, Any]) -> None:
    data = {
        "name": "n",
        "mutates": "none",
        "persona": {"facts": {"name": "Sam Example"}},
        "steps": [{"when": "x", "clips": ["c"]}],
        "expect": expect,
    }
    assert parse_scenario(data).states_expectations()


def test_a_batch_holds_back_an_ungradeable_scenario_unless_allowed(tmp_path: Path) -> None:
    case = captured(tmp_path)
    ingest(case)
    scenarios = load_scenarios(case / "scenarios")
    bank = ClipBank(case / "clips")
    built: list[int] = []

    def make() -> FakeLeg:
        built.append(1)
        raise AssertionError("no call may be placed")

    held = asyncio.run(
        run_batch(
            scenarios,
            make,
            bank,
            tmp_path / "runs",
            allow_mutating="all",
            allow_unverified_clips=True,
            budget=NameBudget(limit=2),
        )
    )
    assert built == []
    assert held.ran == 0
    assert "UNGRADEABLE" in held.blocked[0].reason
    assert "no expectations" in held.blocked[0].reason
    plan = "\n".join(dry_run(scenarios, [], leg="fake", allow_mutating="all"))
    assert "NOT RUN: UNGRADEABLE" in plan
    permitted = dry_run(scenarios, [], leg="fake", allow_mutating="all", allow_ungradeable=True)
    assert "UNGRADEABLE" not in "\n".join(permitted)


def _git_repo(root: Path, ignore: str = "") -> Path:
    import subprocess

    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text(ignore, encoding="utf-8")
    return root


def test_a_capture_inside_a_repository_is_refused_unless_git_ignores_it(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo", "cases/\n")
    platform = Platform()
    with pytest.raises(CaptureError, match="inside a git work tree"):
        capture("c", repo / "elsewhere" / "case", KEY, transport=platform.transport)
    assert platform.requests == []  # refused before any request
    assert not (repo / "elsewhere").exists()
    capture("c", repo / "cases" / "case", KEY, transport=platform.transport)
    assert (repo / "cases" / "case" / "audio.wav").exists()


def test_a_capture_outside_any_work_tree_is_allowed(tmp_path: Path) -> None:
    capture("c", tmp_path / "plain" / "case", KEY, transport=Platform().transport)
    assert (tmp_path / "plain" / "case" / "capture.json").exists()


def test_the_default_cases_directory_is_ignored_by_the_repository() -> None:
    import subprocess

    here = Path(__file__).parent.parent
    ignored = subprocess.run(
        ["git", "-C", str(here), "check-ignore", "-q", str(here / "cases" / "x" / "audio.wav")]
    )
    assert ignored.returncode == 0
    ignored = subprocess.run(
        ["git", "-C", str(here), "check-ignore", "-q", str(here / "cases" / "x" / "capture.json")]
    )
    assert ignored.returncode == 0
