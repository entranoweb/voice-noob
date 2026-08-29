"""A websocket client that speaks ASGI directly to the app under test.

``TestClient`` drives the application from a second thread with its own event
loop, which does not mix with an asyncpg engine created in the test's loop. This
harness calls the ASGI application in the running loop instead, so a websocket
test can share the database session fixtures with every other test.

It is a client, not a mock: the real routing, the real dependency graph, and the
real endpoint function all run.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from types import TracebackType


class ASGIWebSocketClient:
    """Drive one websocket connection against an ASGI application."""

    def __init__(self, app: Any, path: str, *, query_string: str = "") -> None:
        self._app = app
        self._path = path
        self._query_string = query_string.encode()
        self._to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self.accepted = False
        self.close_code: int | None = None

    async def __aenter__(self) -> Self:
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": self._path,
            "raw_path": self._path.encode(),
            "query_string": self._query_string,
            "root_path": "",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "subprotocols": [],
            "state": {},
        }
        self._task = asyncio.create_task(self._app(scope, self._receive, self._send))
        await self._to_app.put({"type": "websocket.connect"})

        message = await self._next()
        if message["type"] == "websocket.accept":
            self.accepted = True
        elif message["type"] == "websocket.close":
            self.close_code = message.get("code", 1000)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.disconnect()

    # -- test-facing API --------------------------------------------------
    #
    # The read deadlines below are named `deadline_s` rather than `timeout`
    # deliberately: they bound how long a test waits for the app to say
    # something, and are not a cancellation budget a caller should be passing
    # down. Naming them `timeout` invites exactly that reading.

    async def send_json(self, payload: dict[str, Any]) -> None:
        await self._to_app.put({"type": "websocket.receive", "text": json.dumps(payload)})

    async def receive_json(self, *, deadline_s: float = 2.0) -> dict[str, Any] | None:
        """The next frame the app sent, or None if it closed instead."""
        message = await self._next(deadline_s=deadline_s)
        if message["type"] == "websocket.close":
            self.close_code = message.get("code", 1000)
            return None
        text = message.get("text")
        return json.loads(text) if text else None

    async def drain(self, *, deadline_s: float = 0.5) -> list[dict[str, Any]]:
        """Everything the app sends until it goes quiet or closes."""
        frames: list[dict[str, Any]] = []
        while True:
            try:
                message = await self._next(deadline_s=deadline_s)
            except TimeoutError:
                return frames
            if message["type"] == "websocket.close":
                self.close_code = message.get("code", 1000)
                return frames
            text = message.get("text")
            if text:
                frames.append(json.loads(text))

    async def disconnect(self, code: int = 1000) -> None:
        await self._to_app.put({"type": "websocket.disconnect", "code": code})
        if self._task is not None:
            with_timeout = asyncio.wait_for(asyncio.shield(self._task), timeout=5.0)
            try:
                await with_timeout
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    # -- ASGI plumbing ----------------------------------------------------

    async def _receive(self) -> dict[str, Any]:
        return await self._to_app.get()

    async def _send(self, message: dict[str, Any]) -> None:
        await self._from_app.put(message)

    async def _next(self, *, deadline_s: float = 5.0) -> dict[str, Any]:
        return await asyncio.wait_for(self._from_app.get(), timeout=deadline_s)


__all__ = ["ASGIWebSocketClient"]
