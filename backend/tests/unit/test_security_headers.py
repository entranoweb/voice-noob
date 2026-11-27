"""Security response headers: development, production, https behind a proxy."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.middleware.security import SecurityHeadersMiddleware

if TYPE_CHECKING:
    from starlette.requests import Request


async def _ok(_request: Request) -> Response:
    return JSONResponse({"ok": True})


def _app(*, debug: bool, docs: bool = False, trusted: str = "127.0.0.1") -> ProxyHeadersMiddleware:
    inner = Starlette(routes=[Route("/", _ok)])
    inner.add_middleware(SecurityHeadersMiddleware, debug=debug, docs_enabled=docs)
    # What uvicorn does in front of the app when forwarded_allow_ips is set.
    return ProxyHeadersMiddleware(inner, trusted_hosts=trusted)


async def _get(app: object, base: str = "http://test", **headers: str) -> Response:
    transport = ASGITransport(app=app, client=("127.0.0.1", 1234))  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url=base) as client:
        return await client.get("/", headers=headers)  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_hsts_absent_over_plain_http_in_development() -> None:
    r = await _get(_app(debug=True))
    assert "strict-transport-security" not in r.headers


@pytest.mark.asyncio
async def test_hsts_present_over_https_in_development() -> None:
    r = await _get(_app(debug=True), base="https://test")
    assert "max-age=" in r.headers["strict-transport-security"]


@pytest.mark.asyncio
async def test_hsts_present_in_production_even_if_proxy_hides_scheme() -> None:
    r = await _get(_app(debug=False))
    assert "max-age=" in r.headers["strict-transport-security"]


@pytest.mark.asyncio
async def test_hsts_from_trusted_proxy_forwarded_proto() -> None:
    r = await _get(
        _app(debug=True), **{"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9"}
    )
    assert "max-age=" in r.headers["strict-transport-security"]


@pytest.mark.asyncio
async def test_hsts_ignores_forwarded_proto_from_untrusted_peer() -> None:
    r = await _get(_app(debug=True, trusted="10.9.9.9"), **{"X-Forwarded-Proto": "https"})
    assert "strict-transport-security" not in r.headers


@pytest.mark.asyncio
async def test_xss_protection_header_not_sent() -> None:
    r = await _get(_app(debug=False))
    assert "x-xss-protection" not in r.headers


@pytest.mark.asyncio
async def test_csp_has_no_localhost_in_production() -> None:
    csp = (await _get(_app(debug=False))).headers["content-security-policy"]
    assert "localhost" not in csp
    assert "unsafe-inline" not in csp
    assert "cdn.jsdelivr.net" not in csp
    assert "frame-ancestors 'none'" in csp


@pytest.mark.asyncio
async def test_csp_allows_localhost_in_development() -> None:
    csp = (await _get(_app(debug=True))).headers["content-security-policy"]
    assert "http://localhost:*" in csp


@pytest.mark.asyncio
async def test_csp_allows_docs_assets_only_when_docs_enabled() -> None:
    csp = (await _get(_app(debug=False, docs=True))).headers["content-security-policy"]
    assert "https://cdn.jsdelivr.net" in csp
    assert "localhost" not in csp


@pytest.mark.asyncio
async def test_other_headers_kept() -> None:
    h = (await _get(_app(debug=False))).headers
    assert h["x-frame-options"] == "DENY"
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "microphone=()" in h["permissions-policy"]


@pytest.mark.asyncio
async def test_real_app_sends_hsts_and_no_localhost_csp_outside_debug() -> None:
    """The production wiring in app.main, not just the class in isolation."""
    from app.core.config import settings
    from app.main import app

    assert not settings.DEBUG  # the suite runs as production does
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/health")
    assert "max-age=" in r.headers["strict-transport-security"]
    assert "localhost" not in r.headers["content-security-policy"]
    assert "x-xss-protection" not in r.headers
