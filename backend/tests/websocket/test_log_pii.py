"""Call-site tests: personal data must not reach a log record.

Each test runs a real code path with ``structlog.testing.capture_logs`` active
and asserts that neither the full number nor the argument value appears anywhere
in any captured record. ``capture_logs`` disables the configured processors, so
what it returns is exactly what the call site passed; a redaction applied only
by a processor would not be visible here, which is the point.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import structlog
from httpx import ASGITransport, AsyncClient
from structlog.testing import capture_logs

from app.main import app
from app.services.gpt_realtime import GPTRealtimeSession
from app.services.tools.registry import ToolRegistry
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
