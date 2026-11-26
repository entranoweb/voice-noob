"""Refuse a request body over a size cap, whether or not it declares its length.

Neither FastAPI/Starlette (as installed) nor uvicorn caps the body: uvicorn's
``--limit-*`` and gunicorn's ``limit_request_*`` settings cover the request line
and headers only. A declared ``Content-Length`` over the cap is refused before a
byte is read; a body that does not declare one (chunked) or understates it is
counted as it streams, and refused when it passes the cap.
"""

import json
from collections.abc import Iterable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.limits import MAX_JSON_BODY_BYTES


class BodyTooLargeError(Exception):
    pass


class BodySizeLimitMiddleware:
    """Pure ASGI, so the body is counted as it arrives and never buffered here."""

    def __init__(
        self,
        app: ASGIApp,
        max_bytes: int = MAX_JSON_BODY_BYTES,
        exempt_prefixes: Iterable[str] = (),
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.exempt_prefixes = tuple(exempt_prefixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(self.exempt_prefixes):
            await self.app(scope, receive, send)
            return

        for name, value in scope["headers"]:
            if name == b"content-length" and value.isdigit() and int(value) > self.max_bytes:
                await self._refuse(send)
                return

        seen = 0
        exceeded = False
        started = False

        async def counted_receive() -> Message:
            nonlocal seen, exceeded
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    exceeded = True
                    raise BodyTooLargeError
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if exceeded:
                # The app is unwinding from the refusal; whatever it says
                # (Starlette's error handler answers 500) is replaced by the 413.
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted_receive, tracking_send)
        except Exception:
            if not exceeded or started:
                raise
            await self._refuse(send)
        else:
            if exceeded and not started:
                await self._refuse(send)

    async def _refuse(self, send: Send) -> None:
        body = json.dumps(
            {"detail": f"The request body is larger than {self.max_bytes} bytes"}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
