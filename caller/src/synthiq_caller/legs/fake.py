"""A scripted in-memory agent, for tests.

The fake agent speaks its lines in order. For each line it emits the text, then
audio frames in real time (tone for speech, zeros for silence, and it keeps
sending zeros while it waits, as real agents do). It moves to the next line once
the caller has spoken and then been quiet for a moment, or after ``patience_s``.

It also keeps a log of when the caller's audio carried speech, so a test can
check the caller did not speak while the agent was still talking.
"""

from __future__ import annotations

import asyncio
import math
import struct
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from synthiq_caller.leg import AgentAudio, AgentText, Closed, Heard, LegEvent
from synthiq_caller.turns import rms

RATE = 16000


def tone(seconds: float, rate: int = RATE, level: int = 8000) -> bytes:
    count = int(seconds * rate)
    return b"".join(
        struct.pack("<h", int(level * math.sin(2 * math.pi * 440 * i / rate))) for i in range(count)
    )


@dataclass
class AgentLine:
    text: str
    speech_s: float = 0.4
    # Send the text this long before the audio starts (engines often do).
    text_lead_s: float = 0.15
    # Echo this back as what the recogniser heard from the caller's answer.
    hears: str = ""
    patience_s: float = 2.0


@dataclass
class FakeLeg:
    lines: list[AgentLine]
    frame_s: float = 0.02
    caller_quiet_s: float = 0.15
    caller_speech: list[tuple[float, float]] = field(default_factory=list)
    agent_speech: list[tuple[float, float]] = field(default_factory=list)
    dtmf: list[str] = field(default_factory=list)
    _t0: float = 0.0
    _last_caller_speech: float | None = None
    _caller_spoke: bool = False
    _closed: bool = False

    @property
    def input_rate(self) -> int:
        return RATE

    @property
    def call_ref(self) -> str | None:
        return "fake-call"

    def now(self) -> float:
        return time.monotonic() - self._t0

    async def connect(self) -> None:
        self._t0 = time.monotonic()

    async def send_pcm(self, pcm: bytes) -> None:
        if rms(pcm) > 250:
            now = self.now()
            self.caller_speech.append((now, now + len(pcm) / 2 / RATE))
            self._last_caller_speech = now
            self._caller_spoke = True

    async def send_dtmf(self, digits: str) -> None:
        self.dtmf.append(digits)
        self._caller_spoke = True
        self._last_caller_speech = self.now()

    async def close(self) -> None:
        self._closed = True

    async def events(self) -> AsyncIterator[LegEvent]:
        silence = b"\0" * (int(self.frame_s * RATE) * 2)
        for line in self.lines:
            if self._closed:
                break
            self._caller_spoke = False
            yield AgentText(line.text)
            await asyncio.sleep(line.text_lead_s)
            start = self.now()
            frames = max(1, int(line.speech_s / self.frame_s))
            for _ in range(frames):
                yield AgentAudio(tone(self.frame_s), RATE)
                await asyncio.sleep(self.frame_s)
            self.agent_speech.append((start, self.now()))
            waited_from = self.now()
            while not self._closed:
                yield AgentAudio(silence, RATE)
                await asyncio.sleep(self.frame_s)
                now = self.now()
                quiet_for = now - (self._last_caller_speech or now)
                if self._caller_spoke and quiet_for >= self.caller_quiet_s:
                    break
                if now - waited_from >= line.patience_s:
                    break
            if line.hears and self._caller_spoke:
                yield Heard(line.hears)
        yield Closed("script finished")
