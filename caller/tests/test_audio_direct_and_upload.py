"""Both sides of a direct-connection call kept as WAV files, and sent to the server.

Sound here is generated tones; nothing is a recording of anyone.
"""

from __future__ import annotations

import base64
import json
import math
import struct
import wave
from pathlib import Path
from typing import Any

import httpx
import pytest

from synthiq_caller.cli import main
from synthiq_caller.legs.elevenlabs_socket import ElevenLabsSocketLeg
from synthiq_caller.recording import resample
from synthiq_caller.upload import audio_files, post_audio

REAL_CLIENT = httpx.Client


def tone(seconds: float, rate: int, hz: int = 440) -> bytes:
    return b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * hz * i / rate)))
        for i in range(int(seconds * rate))
    )


class FakeSocket:
    """Just enough of a websocket: it sends nothing anywhere and replays what the agent said."""

    def __init__(self, incoming: list[dict[str, Any]]) -> None:
        self._incoming = incoming
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def close(self) -> None:
        return None

    def __aiter__(self) -> FakeSocket:
        return self

    async def __anext__(self) -> str:
        if not self._incoming:
            raise StopAsyncIteration
        return json.dumps(self._incoming.pop(0))


def meta(out: str = "pcm_24000", inp: str = "pcm_16000") -> dict[str, Any]:
    return {
        "type": "conversation_initiation_metadata",
        "conversation_initiation_metadata_event": {
            "conversation_id": "conv_abc123",
            "user_input_audio_format": inp,
            "agent_output_audio_format": out,
        },
    }


def agent_audio(pcm: bytes) -> dict[str, Any]:
    return {"type": "audio", "audio_event": {"audio_base_64": base64.b64encode(pcm).decode()}}


def wav_of(path: Path) -> tuple[int, int, int]:
    with wave.open(str(path), "rb") as wav:
        return wav.getframerate(), wav.getnchannels(), wav.getnframes()


async def _call(leg: ElevenLabsSocketLeg, incoming: list[dict[str, Any]]) -> None:
    leg._ws = FakeSocket(incoming)
    async for _event in leg.events():
        pass


@pytest.mark.asyncio
async def test_a_direct_call_writes_both_sides_and_says_where(tmp_path: Path) -> None:
    out = tmp_path / "run"
    leg = ElevenLabsSocketLeg("key", "agent", audio_dir=str(out / "audio"))
    await _call(leg, [meta(), agent_audio(tone(0.5, 24000))])
    await leg.send_pcm(tone(0.25, 16000))
    await leg.close()

    details = leg.details()
    audio = details["audio"]
    assert "leg" not in details, "the leg's name is the batch's to say"
    assert audio["caller"].startswith("audio/conv_abc123-")
    assert audio["caller"].endswith(".caller.wav")
    assert audio["agent"].endswith(".agent.wav")
    assert audio["starts_at"] == "media stream start"
    caller_rate, _, caller_frames = wav_of(out / audio["caller"])
    agent_rate, _, agent_frames = wav_of(out / audio["agent"])
    assert caller_rate == agent_rate == audio["rate"] == 16000
    assert caller_frames == agent_frames, "the two files are the same length"
    # The agent's 24 kHz half second came out as half a second at 16 kHz.
    assert agent_frames >= 8000
    assert audio["seconds"] == pytest.approx(agent_frames / 16000, abs=0.01)


@pytest.mark.asyncio
async def test_no_audio_folder_means_no_files_and_nothing_claimed(tmp_path: Path) -> None:
    leg = ElevenLabsSocketLeg("key", "agent")
    await _call(leg, [meta(), agent_audio(tone(0.2, 24000))])
    await leg.close()

    assert leg.details() == {}
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_two_calls_to_one_folder_never_share_a_file(tmp_path: Path) -> None:
    legs = [
        ElevenLabsSocketLeg("key", "agent", audio_dir=str(tmp_path / "audio")) for _ in range(2)
    ]
    for leg in legs:
        await _call(leg, [meta(), agent_audio(tone(0.1, 24000))])
        await leg.close()

    names = {leg.details()["audio"]["agent"] for leg in legs}
    assert len(names) == 2
    assert len(list((tmp_path / "audio").glob("*.wav"))) == 4


@pytest.mark.asyncio
async def test_an_unwritable_folder_is_said_not_hidden(tmp_path: Path) -> None:
    blocker = tmp_path / "audio"
    blocker.write_text("a file where the folder should be")
    leg = ElevenLabsSocketLeg("key", "agent", audio_dir=str(blocker))
    await _call(leg, [meta(), agent_audio(tone(0.1, 24000))])
    await leg.close()

    assert "could not be written" in leg.details()["audio"]["error"]


def test_resampling_keeps_the_length_in_seconds() -> None:
    pcm = tone(0.5, 24000)
    assert len(resample(pcm, 24000, 16000)) == pytest.approx(16000 * 0.5 * 2, abs=4)
    assert resample(pcm, 24000, 24000) == pcm
    assert resample(b"", 24000, 16000) == b""


# ----- upload ---------------------------------------------------------------


def _run_with_audio(root: Path, name: str = "run-001.json") -> dict[str, Any]:
    (root / "audio").mkdir(exist_ok=True)
    for track in ("caller", "agent"):
        with wave.open(str(root / "audio" / f"x.{track}.wav"), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(tone(0.1, 8000))
    return {
        "file": name,
        "payload": {"audio": {"caller": "audio/x.caller.wav", "agent": "audio/x.agent.wav"}},
    }


def test_audio_files_lists_what_the_run_names_and_flags_what_is_missing(tmp_path: Path) -> None:
    present = _run_with_audio(tmp_path)
    missing = {"file": "run-002.json", "payload": {"audio": {"agent": "audio/gone.wav"}}}
    outside = {"file": "run-003.json", "payload": {"audio": {"agent": "../elsewhere.wav"}}}
    failed = {"file": "run-004.json", "payload": {"audio": {"error": "disk full"}}}
    none = {"file": "run-005.json", "payload": {}}

    found = audio_files(tmp_path, [present, missing, outside, failed, none])

    assert [(n, t, p is not None) for n, t, p, _ in found] == [
        ("run-001.json", "caller", True),
        ("run-001.json", "agent", True),
        ("run-002.json", "agent", False),
        ("run-003.json", "agent", False),
        ("run-004.json", "-", False),
    ]
    reasons = {n: why for n, _, _, why in found if why}
    assert "not there" in reasons["run-002.json"]
    assert "leaves the run directory" in reasons["run-003.json"]
    assert reasons["run-004.json"] == "disk full"


def test_each_file_is_its_own_request_after_the_run_with_the_same_token(tmp_path: Path) -> None:
    runs = [_run_with_audio(tmp_path)]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "stored"})

    sent, failures = post_audio(
        tmp_path,
        runs,
        api="https://voice.test/",
        token="secret-token",  # noqa: S106
        run_pk="abc",
        client=REAL_CLIENT(transport=httpx.MockTransport(handler)),
    )

    assert (sent, failures) == (2, [])
    assert [r.method for r in seen] == ["PUT", "PUT"]
    assert {r.url.params["track"] for r in seen} == {"caller", "agent"}
    assert {r.url.params["file"] for r in seen} == {"run-001.json"}
    assert all(r.url.path == "/api/v1/caller/runs/abc/audio" for r in seen)
    assert all(r.headers["authorization"] == "Bearer secret-token" for r in seen)
    assert all(r.content[:4] == b"RIFF" for r in seen)


def _write_run(out: Path, audio: dict[str, str]) -> None:
    payload = {
        "scenario_file": "a.json",
        "mutates": "none",
        "repeat": 1,
        "record": {"scenario": "a", "call_ref": "c", "ended": "x", "duration_s": 1.0},
        "grade": {"scenario": "a", "outcome": "passed", "checks": [], "fields": []},
        "audio": audio,
    }
    (out / "run-001.json").write_text(json.dumps(payload), encoding="utf-8")


def _serve(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    monkeypatch.setenv("SYNTHIQ_API_TOKEN", "secret-token")
    monkeypatch.setattr(
        "synthiq_caller.upload.httpx.Client",
        lambda **_kw: REAL_CLIENT(transport=httpx.MockTransport(handler)),
    )


def _upload_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audio_status: int) -> int:
    out = tmp_path / "runs"
    out.mkdir()
    _write_run(out, _run_with_audio(out)["payload"]["audio"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            if request.url.params["track"] == "agent" and audio_status != 200:
                return httpx.Response(audio_status, json={"detail": "not a WAV file"})
            return httpx.Response(200, json={"status": "stored"})
        return httpx.Response(201, json={"status": "created", "run": {"id": "abc", "attempted": 1}})

    _serve(monkeypatch, handler)
    return main(["upload", str(out), "--api", "https://voice.test", "--workspace", "ws-1"])


def test_the_command_succeeds_only_when_the_audio_went_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _upload_command(tmp_path, monkeypatch, 200)

    assert code == 0
    assert "audio: 2 file(s) uploaded" in capsys.readouterr().out


def test_an_audio_failure_is_said_and_the_exit_is_not_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _upload_command(tmp_path, monkeypatch, 422)

    printed = capsys.readouterr().out
    assert code == 1
    assert "AUDIO UPLOAD FAILED for 1 file(s)" in printed
    assert "run-001.json agent: refused (422) not a WAV file" in printed
    assert "secret-token" not in printed


def test_a_file_the_run_names_but_that_is_missing_fails_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "runs"
    out.mkdir()
    _write_run(out, {"agent": "audio/deleted.wav"})
    _serve(
        monkeypatch,
        lambda _r: httpx.Response(201, json={"status": "created", "run": {"id": "abc"}}),
    )

    code = main(["upload", str(out), "--api", "https://voice.test", "--workspace", "ws-1"])

    assert code == 1
    assert "audio/deleted.wav is not there" in capsys.readouterr().out
