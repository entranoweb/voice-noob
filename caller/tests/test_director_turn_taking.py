"""Director turn-taking: mid-sentence holds, yielding, hang-up timing, pending lines,
and keeping the event loop free. A fake leg, no real audio services."""

import asyncio
import threading
import wave
from collections.abc import AsyncIterator, Coroutine
from pathlib import Path
from typing import Any

import pytest

from synthiq_caller.clips import ClipBank
from synthiq_caller.director import Director
from synthiq_caller.leg import AgentAudio, AgentText, Closed, LegEvent
from synthiq_caller.legs.fake import RATE, AgentLine, FakeLeg, tone
from synthiq_caller.scenario import Scenario, parse_scenario
from synthiq_caller.turns import TurnSettings, rms

FAST = TurnSettings(hangover_s=0.2, audio_start_wait_s=0.6, min_speech_s=0.06)


def write_clip(directory: Path, name: str, seconds: float) -> None:
    with wave.open(str(directory / name), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(tone(seconds))


@pytest.fixture
def bank(tmp_path: Path) -> ClipBank:
    for name, seconds in (("short.wav", 0.3), ("long.wav", 2.0), ("name.wav", 0.2)):
        write_clip(tmp_path, name, seconds)
    return ClipBank(tmp_path, allow_unknown_provenance=True, allow_missing_consent=True)


class ScriptLeg:
    """A leg whose events the test pushes by hand, recording what the caller sends."""

    input_rate = RATE
    call_ref = "script-call"

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[LegEvent] = asyncio.Queue()
        self.sent: list[bytes] = []
        self.clears = 0

    async def connect(self) -> None: ...

    async def send_pcm(self, pcm: bytes) -> None:
        self.sent.append(pcm)

    async def send_dtmf(self, digits: str) -> None: ...

    async def close(self) -> None: ...

    async def clear_playback(self) -> None:
        self.clears += 1

    async def events(self) -> AsyncIterator[LegEvent]:
        while True:
            event = await self.inbox.get()
            yield event
            if isinstance(event, Closed):
                return

    def speech_sent(self) -> float:
        return sum(len(c) / 2 / RATE for c in self.sent if rms(c) > 250)

    async def agent_says(self, text: str, speech_s: float) -> None:
        self.inbox.put_nowait(AgentText(text))
        await self.agent_audio(speech_s)

    async def agent_audio(self, seconds: float, *, speech: bool = True) -> None:
        frame = 0.02
        silence = b"\0" * (int(frame * RATE) * 2)
        for _ in range(int(seconds / frame)):
            self.inbox.put_nowait(AgentAudio(tone(frame) if speech else silence, RATE))
            await asyncio.sleep(frame)


async def run_with(director: Director, script: Coroutine[Any, Any, None]) -> None:
    """Run the director while ``script`` plays the agent; fail if the script fails."""
    run = asyncio.create_task(director.run())
    await script
    await run


def scenario(steps: list[dict[str, object]], **extra: object) -> Scenario:
    return parse_scenario({"name": "t", "steps": steps, "max_seconds": 8, **extra})


# F11: do not answer while the agent's pending text ends mid-sentence.
async def test_waits_a_beat_longer_when_the_line_ends_mid_sentence(bank: ClipBank) -> None:
    sc = scenario([{"when": "postcode", "clips": ["short.wav"]}])
    leg = ScriptLeg()
    director = Director(sc, leg, bank, turn_settings=FAST, chunk_ms=20)

    async def script() -> None:
        # A read-back with a breath in the middle: quiet longer than the hangover,
        # shorter than hangover plus the extra beat.
        await leg.agent_says("Your postcode is SW1A,", 0.4)
        await leg.agent_audio(0.3, speech=False)
        mid_pause_sent = leg.speech_sent()
        await leg.agent_says("one A A. Is that right?", 0.4)
        await leg.agent_audio(0.6, speech=False)
        assert mid_pause_sent == 0.0, "answered in the middle of a read-back"
        leg.inbox.put_nowait(Closed("done"))

    await run_with(director, script())
    assert [s.clips for s in director.record.said] == [["short.wav"]]


async def test_a_line_that_never_finishes_is_still_answered_eventually(bank: ClipBank) -> None:
    # Hard upper wait: text with no closing punctuation must not hang the rig.
    sc = scenario([{"when": "postcode", "clips": ["short.wav"]}])
    leg = ScriptLeg()
    director = Director(sc, leg, bank, turn_settings=FAST, chunk_ms=20)

    async def script() -> None:
        await leg.agent_says("Could you give me your postcode", 0.3)
        await leg.agent_audio(1.5, speech=False)
        leg.inbox.put_nowait(Closed("done"))

    await run_with(director, script())
    assert [s.clips for s in director.record.said] == [["short.wav"]]


# F12: the caller yields when the agent starts talking over it.
async def test_caller_yields_when_the_agent_speaks_over_it(bank: ClipBank) -> None:
    sc = scenario([{"when": "name", "clips": ["long.wav"]}])
    leg = ScriptLeg()
    director = Director(sc, leg, bank, turn_settings=FAST, chunk_ms=20)

    async def script() -> None:
        await leg.agent_says("What is your name?", 0.3)
        await leg.agent_audio(0.5, speech=False)  # director answers with 2 s of speech
        await leg.agent_audio(0.3)  # the agent talks over the answer
        await leg.agent_audio(0.5, speech=False)
        before = leg.speech_sent()
        await leg.agent_audio(0.5, speech=False)
        assert leg.speech_sent() == before, "caller kept talking after yielding"
        leg.inbox.put_nowait(Closed("done"))

    await run_with(director, script())
    assert leg.clears == 1
    assert leg.speech_sent() < 1.0  # nowhere near the 2 s clip
    assert any(e["kind"] == "yielded" for e in director.record.timeline)


async def test_talk_over_steps_do_not_yield(bank: ClipBank) -> None:
    sc = scenario([{"when": "name", "clips": ["long.wav"], "talk_over": True}])
    leg = ScriptLeg()
    director = Director(sc, leg, bank, turn_settings=FAST, chunk_ms=20)

    async def script() -> None:
        await leg.agent_says("What is your name?", 0.3)
        await leg.agent_audio(0.3)
        await leg.agent_audio(0.4)
        leg.inbox.put_nowait(Closed("done"))

    await run_with(director, script())
    assert leg.clears == 0
    assert not any(e["kind"] == "yielded" for e in director.record.timeline)


# F17: the end phrase as text is not the agent having said it.
async def test_does_not_hang_up_before_the_agent_has_spoken_the_goodbye(bank: ClipBank) -> None:
    sc = scenario([{"when": "never", "clips": ["short.wav"]}], hangup_after="goodbye")
    leg = FakeLeg([AgentLine("Thank you, goodbye.", speech_s=1.0, patience_s=3.0)])
    record = await Director(sc, leg, bank, turn_settings=FAST, chunk_ms=20).run()
    hangup = next(e for e in record.timeline if e["kind"] == "hangup")
    spoken_until = leg.agent_speech[0][1]
    assert record.ended == "caller hung up: scenario end"
    assert hangup["t"] >= spoken_until - 0.05


# F18: lines that arrive during a step's delay are kept for the next turn.
async def test_lines_arriving_during_a_delay_are_not_discarded(bank: ClipBank) -> None:
    sc = scenario(
        [
            {"when": "how can i help", "clips": ["short.wav"], "delay_s": 1.0},
            {"when": "full name", "clips": ["name.wav"]},
        ]
    )
    leg = FakeLeg(
        [
            AgentLine("How can I help?", speech_s=0.4, patience_s=0.7),
            AgentLine("Your full name?", speech_s=0.3, patience_s=0.5),
            AgentLine("Please hold.", speech_s=0.3, patience_s=3.0),
        ]
    )
    record = await Director(sc, leg, bank, turn_settings=FAST, chunk_ms=20).run()
    assert [s.clips for s in record.said] == [["short.wav"], ["name.wav"]]


# F19: resampling can shell out to ffmpeg; it must not block the event loop.
async def test_clips_are_joined_off_the_event_loop(bank: ClipBank, monkeypatch) -> None:  # noqa: ANN001
    sc = scenario([{"when": "name", "clips": ["short.wav"]}])
    main = threading.get_ident()
    seen: list[int] = []
    real = bank.joined

    def joined(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        seen.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(bank, "joined", joined)
    leg = FakeLeg([AgentLine("What is your name?", speech_s=0.3, patience_s=1.0)])
    await Director(sc, leg, bank, turn_settings=FAST, chunk_ms=20).run()
    assert seen and all(ident != main for ident in seen)
