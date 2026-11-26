"""Call-site tests: personal data must not reach a log record.

Each test runs a real code path with ``structlog.testing.capture_logs`` active
and asserts that neither the full number nor the argument value appears anywhere
in any captured record. ``capture_logs`` disables the configured processors, so
what it returns is exactly what the call site passed; a redaction applied only
by a processor would not be visible here, which is the point.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Self

import httpx
import pytest
import structlog
from httpx import ASGITransport, AsyncClient
from structlog.testing import capture_logs

from app.main import app
from app.services.gpt_realtime import GPTRealtimeSession
from app.services.telephony.telnyx_service import TelnyxService
from app.services.tools.gohighlevel_tools import GoHighLevelTools
from app.services.tools.registry import ToolRegistry
from app.services.tools.shopify_tools import ShopifyTools
from app.services.tools.sms_tools import TelnyxSMSTools
from tests.websocket.test_telnyx_inbound_call import (  # noqa: F401
    AGENT_NUMBER,
    CALL_SID,
    CALLER_NUMBER,
    inbound_call_env,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_LOGGING_MODULES = (
    "app.api.telephony",
    "app.api.tools",
    "app.services.gpt_realtime",
    "app.services.tools.sms_tools",
    "app.services.tools.shopify_tools",
    "app.services.tools.gohighlevel_tools",
    "app.services.telephony.telnyx_service",
)


@pytest.fixture(autouse=True)
def _capturable_loggers(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Let ``capture_logs`` see the modules' loggers.

    ``app.main`` configures structlog with ``cache_logger_on_first_use=True``,
    and the structlog docs say capture_logs does not affect cached loggers. It
    also filters below WARNING unless DEBUG is on, so the INFO lines a debugging
    operator would turn on never reach a processor. So lower the filter and swap in fresh, uncached loggers for the duration of the test and restore the
    original configuration afterwards.
    """
    import importlib

    saved = structlog.get_config()
    structlog.configure(
        cache_logger_on_first_use=False,
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
    )
    for name in _LOGGING_MODULES:
        monkeypatch.setattr(importlib.import_module(name), "logger", structlog.get_logger())
    yield
    structlog.configure(**saved)


SECRET_ARGUMENT = "pearl-grey-unlikely-value"
SECRET_NUMBER = "+15550100042"
SECRET_EMAIL = "pat.example@example.invalid"


def _everything(records: list[dict[str, Any]]) -> str:
    return repr(records)


@pytest.mark.asyncio
async def test_tool_execution_logs_argument_keys_not_values(
    test_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = GPTRealtimeSession(
        test_session,
        1,
        {"enabled_tools": ["crm"], "enabled_tool_ids": {"crm": ["search_customer"]}},
    )
    session.tool_registry = ToolRegistry(test_session, 1)

    async def execute(_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {"success": True}

    monkeypatch.setattr(session.tool_registry, "execute_tool", execute)

    with capture_logs() as records:
        await session.handle_tool_call(
            {
                "name": "search_customer",
                "arguments": {"query": SECRET_ARGUMENT, "to": SECRET_NUMBER},
            }
        )

    text = _everything(records)
    assert records, "the tool call logged nothing, so this test proves nothing"
    assert SECRET_ARGUMENT not in text
    assert SECRET_NUMBER not in text
    assert "query" in text  # the key names are still there


@pytest.mark.asyncio
async def test_inbound_telnyx_webhook_logs_no_full_number(
    inbound_call_env: dict[str, Any],  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = (
        f"CallSid={CALL_SID}&AccountSid=acct&From={CALLER_NUMBER.replace('+', '%2B')}"
        f"&To={AGENT_NUMBER.replace('+', '%2B')}&CallStatus=ringing&Direction=inbound"
    ).encode()

    caplog.set_level(logging.DEBUG, logger="app")
    with capture_logs() as records:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://voice.example.com"
        ) as client:
            response = await client.post(
                "/webhooks/telnyx/voice",
                content=body,
                headers={
                    "content-type": "application/x-www-form-urlencoded",
                    **inbound_call_env["sign"](body),
                },
            )

    assert response.status_code == 200
    # Only the application's own stdlib loggers: the DB driver's debug output
    # echoes bound parameters and is not something this code logs.
    app_lines = " ".join(r.getMessage() for r in caplog.records if r.name.startswith("app"))
    text = _everything(records) + app_lines
    assert records, "the webhook logged nothing, so this test proves nothing"
    for number in (CALLER_NUMBER, AGENT_NUMBER):
        assert number not in text
        assert number.lstrip("+") not in text
    assert CALLER_NUMBER[-2:] in text  # last two digits are kept for correlation


@pytest.mark.asyncio
async def test_outbound_sms_failure_logs_no_number_or_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach carrier for {SECRET_NUMBER}")

    tools = TelnyxSMSTools("key", "+15550100")
    tools._client = httpx.AsyncClient(
        base_url=TelnyxSMSTools.BASE_URL, transport=httpx.MockTransport(handler)
    )

    with capture_logs() as records:
        result = await tools.send_sms(SECRET_NUMBER, SECRET_ARGUMENT)

    assert result["success"] is False
    text = _everything(records)
    assert records, "the failure logged nothing, so this test proves nothing"
    assert SECRET_NUMBER not in text
    assert SECRET_ARGUMENT not in text


def _raising_client(base_url: str) -> httpx.AsyncClient:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"lookup failed for {SECRET_EMAIL} / {SECRET_NUMBER} at https://x.invalid/?q={SECRET_EMAIL}"
        )

    return httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(handler))


def _assert_clean(records: list[dict[str, Any]]) -> None:
    text = _everything(records)
    assert records, "nothing was logged, so this test proves nothing"
    assert SECRET_EMAIL not in text
    assert "pat.example" not in text
    assert SECRET_NUMBER not in text
    assert SECRET_NUMBER.lstrip("+") not in text
    # No traceback: its last line is "ExcType: message" and cannot be redacted
    # by the call site.
    assert not any(r.get("exc_info") for r in records)
    assert any(r.get("error_type") == "ConnectError" for r in records)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["shopify", "gohighlevel"])
async def test_tool_error_with_email_and_number_in_message_is_masked(tool: str) -> None:
    with capture_logs() as records:
        if tool == "shopify":
            shopify = ShopifyTools("token", "shop.myshopify.com")
            shopify._client = _raising_client("https://shop.myshopify.com")
            result = await shopify.search_customers(SECRET_EMAIL)
        else:
            ghl = GoHighLevelTools("token", "loc1")
            ghl._client = _raising_client("https://services.example.invalid")
            result = await ghl.ghl_search_contact(SECRET_EMAIL)

    assert result["success"] is False
    _assert_clean(records)


@pytest.mark.asyncio
async def test_telephony_exception_logs_no_traceback_message() -> None:
    service = TelnyxService("key")
    service._http_client = _raising_client("https://api.example.invalid")

    with capture_logs() as records:
        released = await service.release_phone_number("pn-1")

    assert released is False
    _assert_clean(records)


# --- review round 3: database errors, middleware, cache, realtime errors -----

_DB_SECRET = "Pat Example private note"


def _database_error() -> Exception:
    from sqlalchemy.exc import DBAPIError

    return DBAPIError(
        "INSERT INTO call_records (transcript) VALUES ($1)",
        (_DB_SECRET,),
        Exception("connection was closed"),
    )


@pytest.mark.asyncio
async def test_persist_step_database_error_logs_no_bound_parameters() -> None:
    from unittest.mock import AsyncMock

    from app.api import telephony_ws

    async def write() -> None:
        raise _database_error()

    db = AsyncMock()
    with capture_logs() as records:
        await telephony_ws._persist_step("transcript", write, db, structlog.get_logger())

    text = _everything(records)
    assert records, "the failure logged nothing, so this test proves nothing"
    assert _DB_SECRET not in text
    assert "INSERT INTO" not in text
    assert any(r.get("error_type") == "DBAPIError" for r in records)


@pytest.mark.asyncio
async def test_request_failed_log_has_no_raw_message_or_traceback() -> None:
    from fastapi import FastAPI

    from app.middleware import request_tracing
    from app.middleware.request_tracing import RequestTracingMiddleware

    request_tracing.logger = structlog.get_logger()
    failing = FastAPI()
    failing.add_middleware(RequestTracingMiddleware)

    @failing.get("/boom")
    async def boom() -> None:
        raise RuntimeError(f"lookup failed for {SECRET_EMAIL} {SECRET_NUMBER}")

    with capture_logs() as records:
        async with AsyncClient(
            transport=ASGITransport(app=failing, raise_app_exceptions=False),
            base_url="https://voice.example.com",
        ) as client:
            await client.get("/boom")

    failed = [r for r in records if r["event"] == "request_failed"]
    assert failed, "request_failed was not logged"
    text = _everything(failed)
    assert SECRET_EMAIL not in text
    assert SECRET_NUMBER not in text
    assert not any(r.get("exc_info") for r in failed)


@pytest.mark.asyncio
async def test_cache_error_logs_no_key_number_or_traceback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app.core import cache

    class Broken:
        async def get(self, _key: str) -> None:
            raise ConnectionError(f"redis down for {SECRET_NUMBER}")

    async def get_redis() -> Broken:
        return Broken()

    monkeypatch.setattr(cache.redis_module, "get_redis", get_redis)
    caplog.set_level(logging.DEBUG, logger="app")

    assert await cache.cache_get(f"contact:{SECRET_NUMBER}") is None

    records = [r for r in caplog.records if r.name.startswith("app")]
    text = " ".join(r.getMessage() for r in records)
    assert "redis down" in text, "the failure logged nothing, so this test proves nothing"
    assert SECRET_NUMBER not in text
    assert SECRET_NUMBER.lstrip("+") not in text
    assert all(r.exc_info is None for r in records)


@pytest.mark.asyncio
async def test_realtime_error_event_logs_scrubbed_message(
    test_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    session = GPTRealtimeSession(test_session, 1, {})
    event = SimpleNamespace(
        type="error",
        error=SimpleNamespace(
            type="invalid_request_error",
            code="bad_value",
            message=f"cannot read {SECRET_EMAIL} or {SECRET_NUMBER}",
        ),
    )

    class Connection:
        def __aiter__(self) -> Any:
            return self._iter()

        async def _iter(self) -> Any:
            yield event

    session.connection = Connection()
    with capture_logs() as records:
        await session.process_realtime_events()

    errors = [r for r in records if r["event"] == "realtime_api_error"]
    assert errors, "the error event was not logged"
    text = _everything(errors)
    assert SECRET_EMAIL not in text
    assert SECRET_NUMBER not in text
    assert errors[0]["error_code"] == "bad_value"


def test_redaction_processor_runs_before_the_renderer_in_both_pipelines() -> None:
    """Neither the application's pipeline nor the audit logger may bypass it."""
    from app.core import audit
    from app.core.log_redaction import redact_event_dict

    for processors in (structlog.get_config()["processors"], audit._audit_logger._processors):
        names = [getattr(p, "__name__", type(p).__name__) for p in processors]
        assert redact_event_dict in processors
        assert names.index("redact_event_dict") < names.index("JSONRenderer")
    app_names = [
        getattr(p, "__name__", type(p).__name__) for p in structlog.get_config()["processors"]
    ]
    assert app_names.index("ExceptionRenderer") < app_names.index("redact_event_dict")


@pytest.mark.asyncio
async def test_get_db_integrity_error_prints_no_parameters(
    capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """A stdlib logger runs for every request with a database error."""
    from sqlalchemy.exc import IntegrityError

    from app.db import session as db_session

    params = {"first_name": "Pat Example", "notes": "she said hello", "phone": SECRET_NUMBER}
    error = IntegrityError("INSERT INTO contacts (...)", params, Exception("duplicate key"))

    class FakeSession:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

        async def rollback(self) -> None:
            return None

        async def commit(self) -> None:
            raise error

        async def close(self) -> None:
            return None

    caplog.set_level(logging.DEBUG)
    original = db_session.AsyncSessionLocal
    db_session.AsyncSessionLocal = FakeSession  # type: ignore[assignment]
    try:
        gen = db_session.get_db()
        await gen.__anext__()
        with pytest.raises(IntegrityError):
            await gen.__anext__()
    finally:
        db_session.AsyncSessionLocal = original

    streams = capfd.readouterr()
    seen = streams.out + streams.err + caplog.text
    assert "Database session error" in seen, "the failure logged nothing, so this proves nothing"
    for secret in ("Pat Example", "she said hello", SECRET_NUMBER, "[parameters:"):
        assert secret not in seen


def _raise(message: str) -> None:
    raise RuntimeError(message)


def test_stdlib_records_are_scrubbed_whoever_logs_them(caplog: pytest.LogCaptureFixture) -> None:
    """The bridge: a library logger that never heard of log_redaction."""
    caplog.set_level(logging.DEBUG)
    lib = logging.getLogger("some.third.party.library")
    try:
        _raise(f"failed for {SECRET_EMAIL} {SECRET_NUMBER}")
    except RuntimeError:
        lib.exception("request to %s failed", SECRET_EMAIL)
    lib.warning("callback for %s", SECRET_NUMBER)

    text = caplog.text + " ".join(r.getMessage() for r in caplog.records)
    assert "failed" in text
    assert SECRET_EMAIL not in text
    assert "pat.example" not in text
    assert SECRET_NUMBER not in text
    assert SECRET_NUMBER.lstrip("+") not in text
