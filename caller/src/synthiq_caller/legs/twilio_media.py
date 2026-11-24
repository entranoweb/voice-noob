"""Phone leg: a real telephone call, carried over a Twilio bidirectional media stream.

The leg runs one small server. Twilio connects a websocket to it for the call's
audio and sends call-status requests to it over plain HTTP GET; both arrive on
the same port. The leg then asks Twilio to dial the agent's number with TwiML
that points the call's audio at that websocket.

Audio on the wire is G.711 mu-law at 8000 Hz, base64 in JSON, with no file
header. The director hands the leg 16-bit PCM at 8000 Hz; it is encoded here
and released one 20 ms frame at a time, so what the far end hears is paced by
this process and not by how fast a clip was queued.

Three things a phone line does not give a caller, and what this leg does:

* No agent text. An optional ``Transcriber`` turns the agent's audio into text
  and the leg emits it as ``AgentText``. Without one, a scenario that waits for
  agent text never answers.
* No recogniser output, so ``Heard`` is never emitted. What the agent heard has
  to be read afterwards from the agent engine's own trace.
* No message that presses a key. ``send_dtmf`` generates the two keypad tones
  as audio and plays them in the call.

Nothing in this module has been run against a carrier. See ``caller/docs/PHONE-LEG.md``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import http
import json
import secrets
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from synthiq_caller import dtmf
from synthiq_caller.leg import AgentAudio, AgentText, Closed, LegEvent
from synthiq_caller.mulaw import lin2ulaw, ulaw2lin
from synthiq_caller.transcribe import NullTranscriber, Transcriber
from synthiq_caller.twilio_rest import ENDED_STATUSES, CallControl, stream_twiml, valid_signature

RATE = 8000
FRAME_MS = 20
FRAME_BYTES = RATE * FRAME_MS // 1000  # one mu-law byte per sample
MULAW_SILENCE = 0xFF


class CallNotConnected(RuntimeError):
    pass


@dataclass
class PhoneConfig:
    to_number: str
    from_number: str
    # The address Twilio can reach this server on, e.g. https://caller.example.com.
    # Always stated by the operator; never taken from an incoming request.
    public_url: str = ""
    listen_host: str = "0.0.0.0"  # noqa: S104 - a carrier must be able to reach it
    listen_port: int = 8765
    record: bool = True
    # How long to wait for the call to be answered and the stream to start.
    answer_timeout_s: int = 60
    # Extra time after the last ring for the answer and the stream handshake.
    stream_grace_s: float = 15.0
    # Send frames of pure silence too. Off by default: the carrier fills the gap.
    send_silence: bool = False


@dataclass
class PhoneLog:
    """What happened on the wire, for the run record and for debugging."""

    stream_sid: str = ""
    statuses: list[str] = field(default_factory=list)
    received_dtmf: list[str] = field(default_factory=list)
    marks: list[str] = field(default_factory=list)
    frames_sent: int = 0
    frames_skipped_silent: int = 0
    clears_sent: int = 0
    rejected: list[str] = field(default_factory=list)
    handshake_signature_seen: bool = False


class TwilioMediaLeg:
    def __init__(
        self,
        config: PhoneConfig,
        control: CallControl,
        *,
        auth_token: str,
        transcriber: Transcriber | None = None,
    ) -> None:
        self.config = config
        self.public_url = config.public_url.rstrip("/")
        self._control = control
        self._auth_token = auth_token
        self._transcriber: Transcriber = transcriber or NullTranscriber()
        self._token = secrets.token_urlsafe(24)
        self._server: Server | None = None
        self._socket: ServerConnection | None = None
        self._events: asyncio.Queue[LegEvent] = asyncio.Queue()
        self._started = asyncio.Event()
        self._closed_sent = False
        self._call_sid: str | None = None
        self._outgoing = bytearray()
        self._sender: asyncio.Task[None] | None = None
        self._call_ended = False
        self.log = PhoneLog()

    # ----- Leg protocol -------------------------------------------------

    @property
    def input_rate(self) -> int:
        return RATE

    @property
    def call_ref(self) -> str | None:
        return self._call_sid

    @property
    def local_port(self) -> int:
        if self._server is None:
            raise RuntimeError("the media server has not been started")
        port: int = next(iter(self._server.sockets)).getsockname()[1]
        return port

    async def start_server(self) -> int:
        """Start listening and return the port. ``connect`` calls this if needed."""
        if self._server is None:
            self._server = await serve(
                self._handle_stream,
                self.config.listen_host,
                self.config.listen_port,
                process_request=self._process_request,
                max_size=None,
            )
        return self.local_port

    def stream_url(self) -> str:
        parts = urlsplit(self.public_url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        return f"{scheme}://{parts.netloc}{parts.path}/media/{self._token}"

    def status_url(self) -> str:
        return f"{self.public_url}/status/{self._token}"

    async def connect(self) -> None:
        if not self.public_url:
            raise CallNotConnected("public_url is not set: Twilio needs an address it can reach")
        await self.start_server()
        await self._transcriber.start(self._on_agent_text)
        self._call_sid = await self._control.create_call(
            to=self.config.to_number,
            from_=self.config.from_number,
            twiml=stream_twiml(self.stream_url(), {"token": self._token}),
            status_callback=self.status_url(),
            record=self.config.record,
            timeout_s=self.config.answer_timeout_s,
        )
        waiter = asyncio.create_task(self._started.wait())
        try:
            await asyncio.wait_for(
                waiter, timeout=self.config.answer_timeout_s + self.config.stream_grace_s
            )
        except TimeoutError as error:
            await self.close()
            last = self.log.statuses[-1] if self.log.statuses else "no status received"
            raise CallNotConnected(f"the media stream never started ({last})") from error
        if self._closed_sent and self.log.stream_sid == "":
            await self.close()
            raise CallNotConnected(f"the call ended before it was answered: {self.log.statuses}")
        self._sender = asyncio.create_task(self._send_frames())

    async def send_pcm(self, pcm: bytes) -> None:
        """Queue caller audio: 16-bit mono PCM at 8000 Hz."""
        self._outgoing.extend(lin2ulaw(pcm))

    async def send_dtmf(self, digits: str) -> None:
        """Play keypad tones in the call's audio. There is no out-of-band way on a stream."""
        self._outgoing.extend(lin2ulaw(dtmf.tones(digits, RATE)))

    async def clear_playback(self) -> None:
        """Drop audio not yet sent and tell Twilio to drop what it has buffered."""
        self._outgoing.clear()
        if self._socket is not None and self.log.stream_sid:
            with contextlib.suppress(ConnectionClosed):
                await self._socket.send(
                    json.dumps({"event": "clear", "streamSid": self.log.stream_sid})
                )
                self.log.clears_sent += 1

    async def events(self) -> AsyncIterator[LegEvent]:
        while True:
            event = await self._events.get()
            yield event
            if isinstance(event, Closed):
                return

    async def close(self) -> None:
        if self._sender is not None:
            self._sender.cancel()
            with contextlib.suppress(BaseException):
                await self._sender
            self._sender = None
        if self._call_sid and not self._call_ended:
            self._call_ended = True
            with contextlib.suppress(Exception):
                await self._control.end_call(self._call_sid)
        if self._socket is not None:
            with contextlib.suppress(Exception):
                await self._socket.close()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        with contextlib.suppress(Exception):
            await self._transcriber.close()
        self._emit_closed("(caller hung up)")

    # ----- internals ----------------------------------------------------

    def _on_agent_text(self, text: str) -> None:
        self._events.put_nowait(AgentText(text))

    def _emit_closed(self, reason: str) -> None:
        if not self._closed_sent:
            self._closed_sent = True
            self._events.put_nowait(Closed(reason))
        # Whoever is waiting in connect() must not wait for a call that is over.
        self._started.set()

    def _token_ok(self, path: str, prefix: str) -> bool:
        base = urlsplit(self.public_url).path.rstrip("/")
        expected = f"{base}{prefix}{self._token}"
        return secrets.compare_digest(path.split("?", 1)[0], expected)

    def _process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        """Answer Twilio's status callback over HTTP; let the media websocket through."""
        if "/status/" not in request.path:
            if request.headers.get("X-Twilio-Signature"):
                self.log.handshake_signature_seen = True
            return None
        if not self._token_ok(request.path, "/status/"):
            self.log.rejected.append("status: unknown path")
            return _plain(connection, http.HTTPStatus.NOT_FOUND, "not found")
        parts = urlsplit(self.public_url)
        url = f"{parts.scheme}://{parts.netloc}{request.path}"
        signature = request.headers.get("X-Twilio-Signature", "")
        if not valid_signature(self._auth_token, url, signature):
            self.log.rejected.append("status: bad signature")
            return _plain(connection, http.HTTPStatus.FORBIDDEN, "bad signature")
        query = dict(parse_qsl(urlsplit(request.path).query))
        status = query.get("CallStatus", "")
        self.log.statuses.append(status)
        if status in ENDED_STATUSES:
            self._call_ended = True
            self._emit_closed(f"(call {status})")
        return _plain(connection, http.HTTPStatus.OK, "ok")

    async def _handle_stream(self, socket: ServerConnection) -> None:
        path = socket.request.path if socket.request is not None else ""
        if not self._token_ok(path, "/media/") or self._socket is not None:
            self.log.rejected.append("media: unknown path or second connection")
            await socket.close(4401, "not authorised")
            return
        self._socket = socket
        try:
            async for raw in socket:
                await self._on_message(json.loads(raw))
        except ConnectionClosed:
            pass
        finally:
            self._emit_closed("(stream closed)")

    async def _on_message(self, message: dict[str, Any]) -> None:
        event = message.get("event")
        if event == "start":
            start = message.get("start") or {}
            token = (start.get("customParameters") or {}).get("token", "")
            call_sid = str(start.get("callSid") or "")
            mismatch = self._call_sid is not None and call_sid != self._call_sid
            if not secrets.compare_digest(str(token), self._token) or mismatch:
                self.log.rejected.append("media: start frame failed the token or call check")
                assert self._socket is not None
                await self._socket.close(4401, "not authorised")
                return
            self.log.stream_sid = str(message.get("streamSid") or start.get("streamSid") or "")
            self._started.set()
        elif event == "media":
            media = message.get("media") or {}
            if media.get("track", "inbound") != "inbound":
                return
            mulaw = base64.b64decode(media.get("payload", ""))
            if not self.log.stream_sid or not mulaw:
                return
            self._events.put_nowait(AgentAudio(ulaw2lin(mulaw), RATE))
            await self._transcriber.feed(mulaw)
        elif event == "dtmf":
            self.log.received_dtmf.append(str((message.get("dtmf") or {}).get("digit", "")))
        elif event == "mark":
            self.log.marks.append(str((message.get("mark") or {}).get("name", "")))
        elif event == "stop":
            self._call_ended = True
            self._emit_closed("(the far end hung up)")

    async def _send_frames(self) -> None:
        """Release one 20 ms frame every 20 ms, against a fixed schedule."""
        period = FRAME_MS / 1000
        next_at = time.monotonic()
        silent_frame = bytes([MULAW_SILENCE]) * FRAME_BYTES
        zero_frame = lin2ulaw(b"\0" * (FRAME_BYTES * 2))
        while True:
            if self._outgoing and self._socket is not None:
                frame = bytes(self._outgoing[:FRAME_BYTES])
                del self._outgoing[:FRAME_BYTES]
                frame = frame.ljust(FRAME_BYTES, bytes([MULAW_SILENCE]))
                if not self.config.send_silence and frame in (silent_frame, zero_frame):
                    self.log.frames_skipped_silent += 1
                else:
                    payload = base64.b64encode(frame).decode()
                    try:
                        await self._socket.send(
                            json.dumps(
                                {
                                    "event": "media",
                                    "streamSid": self.log.stream_sid,
                                    "media": {"payload": payload},
                                }
                            )
                        )
                    except ConnectionClosed:
                        return
                    self.log.frames_sent += 1
            next_at += period
            delay = next_at - time.monotonic()
            if delay < -1.0:
                # Fell far behind (a stalled loop): do not burst to catch up.
                next_at = time.monotonic()
                delay = 0.0
            await asyncio.sleep(max(0.0, delay))


def _plain(connection: ServerConnection, status: http.HTTPStatus, text: str) -> Response:
    body = (text + "\n").encode()
    headers = Headers(
        [
            ("Content-Type", "text/plain"),
            ("Content-Length", str(len(body))),
            ("Connection", "close"),
        ]
    )
    del connection  # the response is built by hand so the headers are ours
    return Response(status.value, status.phrase, headers, body)
