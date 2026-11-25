"""A scripted in-memory agent, for tests and for rehearsing a batch.

The fake agent speaks its lines in order. For each line it emits the text, then
audio frames in real time (tone for speech, zeros for silence, and it keeps
sending zeros while it waits, as real agents do). It moves to the next line once
the caller has spoken and then been quiet for a moment, or after ``patience_s``.

It also keeps a log of when the caller's audio carried speech, so a test can
check the caller did not speak while the agent was still talking.

A rehearsal (``batch --leg fake``) reads the agent's lines, and the trace it
would have left, from a script file:

    {
      "lines": [{"text": "How can I help?"}, {"text": "Your date of birth?", "hears": "20 Feb"}],
      "trace": {"tool_calls": [{"name": "lookup", "arguments": {}, "result": {}, "ok": true}]}
    }

Nothing in a rehearsal says anything about any agent. Every run it writes is
marked ``rehearsal`` and no gate can be met by one.
"""

from __future__ import annotations

import asyncio
import json
import math
import struct
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from synthiq_caller.leg import AgentAudio, AgentText, Closed, Heard, LegEvent
from synthiq_caller.traces.base import Trace
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


class ScriptError(ValueError):
    """An agent script that cannot be used for a rehearsal."""


@dataclass
class AgentScript:
    """What a scripted agent says in one call, and the trace that call leaves behind."""

    lines: list[AgentLine]
    trace: dict[str, Any] = field(default_factory=dict)


def parse_script(data: Any) -> AgentScript:
    if not isinstance(data, dict) or not isinstance(data.get("lines"), list) or not data["lines"]:
        raise ScriptError("an agent script needs a non-empty list of 'lines'")
    lines: list[AgentLine] = []
    for index, raw in enumerate(data["lines"]):
        if isinstance(raw, str):
            raw = {"text": raw}
        if not isinstance(raw, dict) or not raw.get("text"):
            raise ScriptError(f"line {index} needs 'text'")
        lines.append(
            AgentLine(
                text=str(raw["text"]),
                speech_s=float(raw.get("speech_s", 0.4)),
                text_lead_s=float(raw.get("text_lead_s", 0.15)),
                hears=str(raw.get("hears", "")),
                patience_s=float(raw.get("patience_s", 2.0)),
            )
        )
    trace = data.get("trace") or {}
    if not isinstance(trace, dict):
        raise ScriptError("'trace' must be an object")
    return AgentScript(lines=lines, trace=trace)


def load_script(path: str | Path) -> AgentScript:
    return parse_script(json.loads(Path(path).read_text(encoding="utf-8")))


def script_for(source: str | Path, scenario_file: str) -> AgentScript:
    """The script for one scenario file.

    ``source`` is one script used for every scenario, or a directory holding
    one per scenario under the scenario file's own name, with ``default.json``
    for the rest.
    """
    root = Path(source)
    if root.is_file():
        return load_script(root)
    for name in (Path(scenario_file).name, Path(scenario_file).stem + ".json", "default.json"):
        if (root / name).is_file():
            return load_script(root / name)
    raise ScriptError(f"no agent script for {scenario_file} in {root} (and no default.json)")


@dataclass
class ScriptedTraceReader:
    """Hands back the trace the script says the call left. Reads nothing."""

    trace: dict[str, Any]

    async def read(self, call_ref: str) -> Trace:
        # Final by construction: nothing will be added to a script.
        return Trace.from_json({**self.trace, "call_ref": call_ref, "final": True})
