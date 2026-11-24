"""The one place that decides which host the outside world is told to call back.

Every URL this application hands to a carrier, or prints for a customer to paste
into their site, has to name a host the outside world can reach. There used to be
several places building those URLs and they did not agree: the Telnyx stream URL
honoured ``PUBLIC_URL``, while the Twilio stream URL and both outbound answer
webhooks took ``request.base_url``.

``request.base_url`` is assembled from the request line and the Host header, and
uvicorn ignores forwarded headers unless it has been told to trust the proxy. So
behind a proxy it resolves to the address the app is bound to, the carrier is
handed ``https://internal:8000/...``, and the call connects to nothing while every
log line reads healthy (DECISIONS §37).

The rule, applied everywhere through the functions below: ``PUBLIC_URL`` wins
when it is set, and the request's own origin is the fallback so a local run with
nothing configured still works.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.config import settings

if TYPE_CHECKING:
    from fastapi import Request


def public_base_url(request: Request | None = None) -> str:
    """The http(s) origin the outside world reaches this API on. No trailing slash."""
    configured = (settings.PUBLIC_URL or "").strip()
    if configured:
        return configured.rstrip("/")
    if request is not None:
        return str(request.base_url).strip().rstrip("/")
    return f"http://{settings.HOST}:{settings.PORT}"


def public_ws_base_url(request: Request | None = None) -> str:
    """The same origin as a websocket origin.

    Both ``http`` and ``https`` map to ``wss``: a carrier will not open a
    cleartext media stream, so the scheme is never what goes wrong here. The host
    is, which is why the tests assert the host.
    """
    return public_base_url(request).replace("http://", "wss://").replace("https://", "wss://")


def public_request_url(request: Request) -> str:
    """The URL the sender addressed this request to, as the sender saw it.

    Twilio signs the full URL it posted to. Behind a proxy ``request.url`` is the
    internal one, so a signature computed over the public URL would never match
    it and every webhook would be refused. Rebuilt from ``PUBLIC_URL`` when that
    is set; otherwise the request's own URL, unchanged.
    """
    if not (settings.PUBLIC_URL or "").strip():
        return str(request.url)
    url = f"{public_base_url(request)}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    return url


def frontend_base_url(request: Request | None = None) -> str:
    """The origin the web app and the embeddable widget are served from.

    The widget script and the embed page are frontend assets, and in a split
    deployment the frontend is a different host from this API. ``FRONTEND_URL``
    names it; failing that, the first allowed CORS origin is the frontend by
    construction (the production compose file sets one from the other); failing
    that, this API's own public origin, which is right for a single-host install.
    """
    configured = (settings.FRONTEND_URL or "").strip()
    if configured:
        return configured.rstrip("/")
    for origin in settings.CORS_ORIGINS:
        candidate = origin.strip().rstrip("/")
        if candidate and candidate != "*":
            return candidate
    return public_base_url(request)


__all__ = [
    "frontend_base_url",
    "public_base_url",
    "public_request_url",
    "public_ws_base_url",
]
