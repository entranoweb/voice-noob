"""Raw exception text and provider bodies must not reach tool results.

A tool result goes to the voice model, which may read it to a caller aloud.
A database error carries SQL and the bound parameters (other people's phone
numbers), an HTTP error a URL with the key in it, a provider body an echoed
request. Each module below gets one failing path; the result must carry
neither the placeholder phone, the SQL, nor the key, must keep
``success: False`` and must carry a stable ``error_code``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import DBAPIError

from app.core.safe_errors import public_error, tool_failure
from app.services.tools.calendly_tools import CalendlyTools
from app.services.tools.crm_tools import CRMTools
from app.services.tools.gohighlevel_tools import GHL_BASE_URL, GoHighLevelTools
from app.services.tools.shopify_tools import ShopifyTools
from app.services.tools.sms_tools import TelnyxSMSTools, TwilioSMSTools

PHONE = "+15550100042"
KEY = "sk-placeholder-key-0000"
SQL = "SELECT * FROM contacts WHERE phone_number = :phone"
LEAKS = (PHONE, KEY, "SELECT", "contacts", "phone_number", "https://", "Traceback")


def _db_error() -> DBAPIError:
    return DBAPIError(SQL, {"phone": PHONE}, Exception(f"driver says {PHONE}"))


def _assert_clean(result: dict[str, Any]) -> None:
    assert result["success"] is False
    assert result.get("error_code"), result
    text = repr(result)
    for leak in LEAKS:
        assert leak not in text, f"{leak!r} reached the tool result: {result}"


class _BrokenDb:
    async def execute(self, *_: Any, **__: Any) -> Any:
        raise _db_error()

    async def rollback(self) -> None:
        return None


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _wire(tools: Any, base_url: str, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    tools._client = httpx.AsyncClient(base_url=base_url, transport=_transport(handler), timeout=5.0)


def _raises(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError(f"cannot reach https://api.example.test/v1?key={KEY}&to={PHONE}")


def _echoes(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        400,
        json={"message": f"Bad number {PHONE}", "detail": f"key {KEY}"},
    )


@pytest.mark.asyncio
async def test_crm_database_error_is_not_returned() -> None:
    result = await CRMTools(_BrokenDb(), user_id=1).search_customer(PHONE)  # type: ignore[arg-type]
    _assert_clean(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [_raises, _echoes], ids=["transport_error", "provider_body"])
async def test_ghl_error_is_not_returned(handler: Callable[..., httpx.Response]) -> None:
    tools = GoHighLevelTools("token", "loc1")
    _wire(tools, GHL_BASE_URL, handler)
    result = await tools.ghl_create_contact(first_name="Sam", phone=PHONE)
    _assert_clean(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [_raises, _echoes], ids=["transport_error", "provider_body"])
async def test_twilio_sms_error_is_not_returned(handler: Callable[..., httpx.Response]) -> None:
    tools = TwilioSMSTools("ACtest", "secret", "+15550100000")
    _wire(tools, f"{TwilioSMSTools.BASE_URL}/Accounts/ACtest", handler)
    _assert_clean(await tools.send_sms(PHONE, "hello"))


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [_raises, _echoes], ids=["transport_error", "provider_body"])
async def test_telnyx_sms_error_is_not_returned(handler: Callable[..., httpx.Response]) -> None:
    tools = TelnyxSMSTools("key", "+15550100000")
    _wire(tools, TelnyxSMSTools.BASE_URL, handler)
    _assert_clean(await tools.send_sms(PHONE, "hello"))


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [_raises, _echoes], ids=["transport_error", "provider_body"])
async def test_calendly_error_is_not_returned(handler: Callable[..., httpx.Response]) -> None:
    tools = CalendlyTools("token")
    tools._user_uri = "https://api.calendly.com/users/placeholder"  # type: ignore[attr-defined]
    _wire(tools, CalendlyTools.BASE_URL, handler)
    _assert_clean(await tools.get_event("abc123"))


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [_raises, _echoes], ids=["transport_error", "provider_body"])
async def test_shopify_error_is_not_returned(handler: Callable[..., httpx.Response]) -> None:
    tools = ShopifyTools("token", "demo-shop.myshopify.com")
    _wire(tools, f"https://demo-shop.myshopify.com/admin/api/{ShopifyTools.API_VERSION}", handler)
    _assert_clean(await tools.get_order("1001"))


def test_tool_failure_is_fixed_per_class_and_keeps_success_false() -> None:
    timeout = tool_failure(httpx.ReadTimeout(f"slow {PHONE}"), "booking system")
    down = tool_failure(httpx.ConnectError(f"refused {KEY}"), "booking system")
    other = tool_failure(_db_error(), "booking system")
    assert [r["error_code"] for r in (timeout, down, other)] == [
        "timeout",
        "unavailable",
        "internal_error",
    ]
    for result in (timeout, down, other):
        assert result["success"] is False
        assert "booking system" in result["error"]
        assert PHONE not in repr(result) and KEY not in repr(result)


def test_public_error_is_message_plus_type_only() -> None:
    text = public_error(_db_error(), "Could not reach the provider")
    assert text == "Could not reach the provider (DBAPIError)"
