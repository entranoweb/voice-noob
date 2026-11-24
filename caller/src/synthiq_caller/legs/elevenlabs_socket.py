"""Leg for an ElevenLabs agent over its conversation websocket.

This reaches the agent's recogniser, turn-taking, workflow and tools without a
telephone line. It is not a phone call: the audio skips the carrier and its
codec, so failures that only appear on the phone path do not appear here.

Wire facts this module depends on (checked against a live agent, Nov 2026):

* ``GET /v1/convai/conversation/get-signed-url?agent_id=...`` returns ``signed_url``.
* The client opens with ``{"type": "conversation_initiation_client_data"}``.
* Caller audio goes up as ``{"user_audio_chunk": "<base64 PCM>"}``.
* ``conversation_initiation_metadata`` states both audio formats.
* ``audio`` frames keep arriving while the agent is silent.
* The ``user_transcript`` event carries its text under ``user_transcription_event``.
* ``ping`` must be answered with ``pong`` and the same ``event_id``.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import websockets

from synthiq_caller.leg import AgentAudio, AgentText, Closed, Heard, Interrupted, LegEvent
from synthiq_caller.mulaw import lin2ulaw, ulaw2lin

API = "https://api.elevenlabs.io/v1"


def _rate(fmt: str) -> int:
    try:
        return int(fmt.rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return 16000


class ElevenLabsSocketLeg:
    def __init__(self, api_key: str, agent_id: str, *, api: str = API) -> None:
        self._key = api_key
        self._agent_id = agent_id
        self._api = api
        self._ws: Any = None
        self._call_ref: str | None = None
        self._in_fmt = "pcm_16000"
        self._out_fmt = "pcm_16000"

    @property
    def input_rate(self) -> int:
        return _rate(self._in_fmt)

    @property
    def call_ref(self) -> str | None:
        return self._call_ref

    async def connect(self) -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self._api}/convai/conversation/get-signed-url",
                params={"agent_id": self._agent_id},
                headers={"xi-api-key": self._key},
            )
            response.raise_for_status()
            url = response.json()["signed_url"]
        self._ws = await websockets.connect(url, max_size=None)
        await self._ws.send(json.dumps({"type": "conversation_initiation_client_data"}))

    async def send_pcm(self, pcm: bytes) -> None:
        if self._ws is None:
            return
        payload = pcm
        if self._in_fmt.startswith("ulaw"):
            payload = lin2ulaw(pcm)
        try:
            await self._ws.send(
                json.dumps({"user_audio_chunk": base64.b64encode(payload).decode()})
            )
        except websockets.ConnectionClosed:
            return

    async def send_dtmf(self, digits: str) -> None:
        raise NotImplementedError("the conversation websocket has no keypad channel")

    def _decode_audio(self, b64: str) -> AgentAudio:
        raw = base64.b64decode(b64)
        if self._out_fmt.startswith("ulaw"):
            return AgentAudio(ulaw2lin(raw), 8000)
        return AgentAudio(raw, _rate(self._out_fmt))

    async def events(self) -> AsyncIterator[LegEvent]:
        if self._ws is None:
            yield Closed("not connected")
            return
        try:
            async for raw in self._ws:
                message = json.loads(raw)
                kind = message.get("type")
                if kind == "conversation_initiation_metadata":
                    meta = message.get("conversation_initiation_metadata_event", {})
                    self._call_ref = meta.get("conversation_id")
                    self._in_fmt = meta.get("user_input_audio_format", self._in_fmt)
                    self._out_fmt = meta.get("agent_output_audio_format", self._out_fmt)
                elif kind == "ping":
                    event_id = message.get("ping_event", {}).get("event_id")
                    await self._ws.send(json.dumps({"type": "pong", "event_id": event_id}))
                elif kind == "audio":
                    b64 = message.get("audio_event", {}).get("audio_base_64")
                    if b64:
                        yield self._decode_audio(b64)
                elif kind == "agent_response":
                    text = message.get("agent_response_event", {}).get("agent_response", "")
                    if text:
                        yield AgentText(text)
                elif kind == "user_transcript":
                    payload = (
                        message.get("user_transcription_event")
                        or message.get("user_transcript_event")
                        or {}
                    )
                    yield Heard(str(payload.get("user_transcript", "")))
                elif kind == "interruption":
                    yield Interrupted()
        except websockets.ConnectionClosed as closed:
            yield Closed(str(closed.rcvd.reason if closed.rcvd else ""))
            return
        yield Closed("")

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
