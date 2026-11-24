"""Turning the agent's audio into text, for legs that only carry audio.

On a telephone line the caller hears the agent; nothing hands it the agent's
words. A scenario decides what to say by matching the agent's line, so a phone
leg needs something to produce that line. That something is a ``Transcriber``:
the leg feeds it the agent's audio and it calls back with text, which the leg
emits as ``AgentText``. The director does not change.

Three are provided:

* ``NullTranscriber`` produces nothing. The call still connects and records; a
  scenario that waits for agent text will simply never answer.
* ``ScriptedTranscriber`` emits the next line from a list each time the agent
  starts speaking. For tests, the dry run, and agents whose order of questions
  is fixed.
* ``DeepgramTranscriber`` streams the audio to Deepgram's live endpoint and
  emits each final transcript. It needs only ``websockets``, already a
  dependency; no Deepgram package is imported.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol
from urllib.parse import urlencode

from synthiq_caller.mulaw import ulaw2lin
from synthiq_caller.turns import rms

OnText = Callable[[str], None]

DEEPGRAM_URL = "wss://api.deepgram.com/v1/listen"


class Transcriber(Protocol):
    async def start(self, on_text: OnText) -> None: ...

    async def feed(self, mulaw: bytes) -> None:
        """One frame of the agent's audio: G.711 mu-law, 8000 Hz, mono."""
        ...

    async def close(self) -> None: ...


class NullTranscriber:
    async def start(self, on_text: OnText) -> None:
        return None

    async def feed(self, mulaw: bytes) -> None:
        return None

    async def close(self) -> None:
        return None


class ScriptedTranscriber:
    """Emits the next scripted line when the agent starts a new stretch of speech."""

    def __init__(
        self, lines: list[str], *, speech_floor: float = 250.0, quiet_s: float = 0.4
    ) -> None:
        self._lines = list(lines)
        self._floor = speech_floor
        self._quiet_s = quiet_s
        self._on_text: OnText | None = None
        self._quiet_for = quiet_s  # start "quiet" so the first speech counts
        self._speaking = False

    async def start(self, on_text: OnText) -> None:
        self._on_text = on_text

    async def feed(self, mulaw: bytes) -> None:
        duration = len(mulaw) / 8000
        if rms(ulaw2lin(mulaw)) > self._floor:
            if not self._speaking and self._quiet_for >= self._quiet_s and self._lines:
                line = self._lines.pop(0)
                if self._on_text is not None:
                    self._on_text(line)
            self._speaking = True
            self._quiet_for = 0.0
        else:
            self._quiet_for += duration
            if self._quiet_for >= self._quiet_s:
                self._speaking = False

    async def close(self) -> None:
        return None


class _Socket(Protocol):
    async def send(self, message: str | bytes) -> None: ...

    async def close(self) -> None: ...

    def __aiter__(self) -> AsyncIterator[str | bytes]: ...


Connect = Callable[[str, dict[str, str]], Awaitable[Any]]


async def _websockets_connect(url: str, headers: dict[str, str]) -> Any:
    import websockets

    return await websockets.connect(url, additional_headers=headers, max_size=None)


class DeepgramTranscriber:
    """Deepgram live transcription of the agent's side of the call.

    Audio goes up as raw binary frames exactly as the carrier delivered it
    (mu-law, 8000 Hz), so nothing is resampled on the way. Only messages of type
    ``Results`` with ``is_final`` true and a non-empty transcript are passed on.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "nova-3",
        language: str = "en-GB",
        endpointing_ms: int = 300,
        keyterms: tuple[str, ...] = (),
        url: str = DEEPGRAM_URL,
        connect: Connect = _websockets_connect,
        keepalive_s: float = 4.0,
    ) -> None:
        if not api_key:
            raise ValueError("a Deepgram API key is required")
        self._key = api_key
        self._connect = connect
        self._keepalive_s = keepalive_s
        query: list[tuple[str, str]] = [
            ("model", model),
            ("language", language),
            ("encoding", "mulaw"),
            ("sample_rate", "8000"),
            ("channels", "1"),
            ("interim_results", "false"),
            ("punctuate", "true"),
            ("smart_format", "true"),
            ("endpointing", str(endpointing_ms)),
        ]
        query.extend(("keyterm", term) for term in keyterms)
        self.url = f"{url}?{urlencode(query)}"
        self._socket: _Socket | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self.errors: list[str] = []

    async def start(self, on_text: OnText) -> None:
        self._socket = await self._connect(self.url, {"Authorization": f"Token {self._key}"})
        self._tasks = [
            asyncio.create_task(self._read(on_text)),
            asyncio.create_task(self._keepalive()),
        ]

    async def _read(self, on_text: OnText) -> None:
        assert self._socket is not None
        async for raw in self._socket:
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            kind = message.get("type")
            if kind == "Error":
                self.errors.append(str(message.get("description") or message))
            if kind != "Results" or not message.get("is_final"):
                continue
            alternatives = (message.get("channel") or {}).get("alternatives") or [{}]
            transcript = str(alternatives[0].get("transcript") or "").strip()
            if transcript:
                on_text(transcript)

    async def _keepalive(self) -> None:
        assert self._socket is not None
        while True:
            await asyncio.sleep(self._keepalive_s)
            await self._socket.send(json.dumps({"type": "KeepAlive"}))

    async def feed(self, mulaw: bytes) -> None:
        if self._socket is not None and mulaw:
            await self._socket.send(mulaw)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._socket is not None:
            with contextlib.suppress(Exception):
                await self._socket.send(json.dumps({"type": "CloseStream"}))
            with contextlib.suppress(Exception):
                await self._socket.close()
        self._socket = None
