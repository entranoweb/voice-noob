"""A stand-in for Twilio, for tests and the dry run. It places no call.

``FakeTwilio`` plays both of Twilio's parts: the control API the leg calls to
"dial", and the media-stream client that then connects to the leg's websocket
and speaks the same messages Twilio does (``connected``, ``start``, ``media``,
``stop``), in mu-law at 8000 Hz, one 20 ms frame at a time.

Its "agent" is a list of utterance lengths. For each it sends a tone for that
long, then silence until the caller has spoken and gone quiet (or patience runs
out), then moves on. Everything the leg sends back is kept with arrival times,
so a test can check pacing, what was said and when, and whether ``clear`` came.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import math
import re
import secrets
import struct
import time
from dataclasses import dataclass, field
from html import unescape
from typing import Any

import websockets

from synthiq_caller.mulaw import lin2ulaw, ulaw2lin
from synthiq_caller.turns import rms

RATE = 8000
FRAME_S = 0.02
FRAME_SAMPLES = int(RATE * FRAME_S)


def tone_pcm(seconds: float, frequency: float = 440.0, level: int = 8000) -> bytes:
    count = int(seconds * RATE)
    return b"".join(
        struct.pack("<h", int(level * math.sin(2 * math.pi * frequency * i / RATE)))
        for i in range(count)
    )


@dataclass
class ReceivedFrame:
    t: float
    mulaw: bytes

    @property
    def is_speech(self) -> bool:
        return rms(ulaw2lin(self.mulaw)) > 250


@dataclass
class FakeTwilio:
    """Implements the leg's ``CallControl`` and drives its media websocket."""

    # Seconds of speech for each agent line, in order.
    agent_lines: list[float]
    patience_s: float = 2.0
    caller_quiet_s: float = 0.2
    # Never answer: connect() should then fail with a clear error.
    answer: bool = True
    # Send a token the leg did not issue, to prove it refuses the stream.
    corrupt_token: bool = False
    frames: list[ReceivedFrame] = field(default_factory=list)
    clears: list[float] = field(default_factory=list)
    marks: list[str] = field(default_factory=list)
    agent_speech: list[tuple[float, float]] = field(default_factory=list)
    created: list[dict[str, Any]] = field(default_factory=list)
    ended: list[str] = field(default_factory=list)
    close_code: int | None = None
    _t0: float = 0.0
    _task: asyncio.Task[None] | None = None
    _hangup: bool = False

    def now(self) -> float:
        return time.monotonic() - self._t0

    # ----- control API ---------------------------------------------------

    async def create_call(
        self, *, to: str, from_: str, twiml: str, status_callback: str, record: bool, timeout_s: int
    ) -> str:
        call_sid = "CA" + secrets.token_hex(16)
        self.created.append(
            {
                "to": to,
                "from": from_,
                "twiml": twiml,
                "status_callback": status_callback,
                "record": record,
                "timeout_s": timeout_s,
                "sid": call_sid,
            }
        )
        url_match = re.search(r'<Stream url="([^"]+)"', twiml)
        if url_match is None:
            raise ValueError("the TwiML has no <Stream url=...>")
        parameters = {
            unescape(name): unescape(value)
            for name, value in re.findall(r'<Parameter name="([^"]*)" value="([^"]*)"', twiml)
        }
        if self.corrupt_token and "token" in parameters:
            parameters["token"] = "not-the-token"
        self._t0 = time.monotonic()
        if self.answer:
            self._task = asyncio.create_task(
                self._stream(unescape(url_match.group(1)), call_sid, parameters)
            )
        return call_sid

    async def end_call(self, call_sid: str) -> None:
        self.ended.append(call_sid)
        self._hangup = True

    async def wait_finished(self) -> None:
        if self._task is not None:
            with contextlib.suppress(Exception):
                await self._task

    # ----- media stream --------------------------------------------------

    def caller_speech(self, merge_gap_s: float = 0.07) -> list[tuple[float, float]]:
        """Stretches of caller speech as (start, end), merged across gaps under ``merge_gap_s``."""
        spans: list[tuple[float, float]] = []
        for frame in self.frames:
            if not frame.is_speech:
                continue
            if spans and frame.t - spans[-1][1] < merge_gap_s:
                spans[-1] = (spans[-1][0], frame.t + FRAME_S)
            else:
                spans.append((frame.t, frame.t + FRAME_S))
        return spans

    async def _stream(self, url: str, call_sid: str, parameters: dict[str, str]) -> None:
        stream_sid = "MZ" + secrets.token_hex(16)
        receiver: asyncio.Task[None] | None = None
        try:
            async with websockets.connect(url, max_size=None) as socket:
                receiver = asyncio.create_task(self._receive(socket))
                await socket.send(
                    json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"})
                )
                await socket.send(
                    json.dumps(
                        {
                            "event": "start",
                            "sequenceNumber": "1",
                            "streamSid": stream_sid,
                            "start": {
                                "accountSid": "ACfake",
                                "streamSid": stream_sid,
                                "callSid": call_sid,
                                "tracks": ["inbound"],
                                "customParameters": parameters,
                                "mediaFormat": {
                                    "encoding": "audio/x-mulaw",
                                    "sampleRate": RATE,
                                    "channels": 1,
                                },
                            },
                        }
                    )
                )
                speech = lin2ulaw(tone_pcm(FRAME_S))
                quiet = lin2ulaw(b"\0" * (FRAME_SAMPLES * 2))
                for seconds in self.agent_lines:
                    if self._hangup:
                        break
                    started = self.now()
                    for _ in range(max(1, int(seconds / FRAME_S))):
                        await self._media(socket, stream_sid, speech)
                    self.agent_speech.append((started, self.now()))
                    waited_from = self.now()
                    heard_before = len(self.caller_speech())
                    while not self._hangup:
                        await self._media(socket, stream_sid, quiet)
                        spans = self.caller_speech()
                        answered = len(spans) > heard_before
                        if answered and self.now() - spans[-1][1] >= self.caller_quiet_s:
                            break
                        if self.now() - waited_from >= self.patience_s:
                            break
                with contextlib.suppress(Exception):
                    await socket.send(
                        json.dumps(
                            {
                                "event": "stop",
                                "streamSid": stream_sid,
                                "stop": {"accountSid": "ACfake", "callSid": call_sid},
                            }
                        )
                    )
        except websockets.ConnectionClosed as closed:
            self.close_code = closed.rcvd.code if closed.rcvd else None
        finally:
            if receiver is not None:
                receiver.cancel()
                with contextlib.suppress(BaseException):
                    await receiver

    async def _media(self, socket: Any, stream_sid: str, mulaw: bytes) -> None:
        await socket.send(
            json.dumps(
                {
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {
                        "track": "inbound",
                        "timestamp": str(int(self.now() * 1000)),
                        "payload": base64.b64encode(mulaw).decode(),
                    },
                }
            )
        )
        await asyncio.sleep(FRAME_S)

    async def _receive(self, socket: Any) -> None:
        try:
            async for raw in socket:
                message = json.loads(raw)
                event = message.get("event")
                if event == "media":
                    payload = base64.b64decode(message["media"]["payload"])
                    self.frames.append(ReceivedFrame(self.now(), payload))
                elif event == "clear":
                    self.clears.append(self.now())
                elif event == "mark":
                    self.marks.append(str(message.get("mark", {}).get("name", "")))
        except websockets.ConnectionClosed as closed:
            self.close_code = closed.rcvd.code if closed.rcvd else None
