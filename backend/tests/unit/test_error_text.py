"""Raw exception text and provider bodies must not reach tool results.

A tool result goes to the voice model, which may read it to a caller aloud.
A database error carries SQL and the bound parameters (other people's phone
numbers), an HTTP error a URL with the key in it, a provider body an echoed
request. Each module below gets one failing path; the result must carry
neither the placeholder phone, the SQL, nor the key, must keep
``success: False`` and must carry a stable ``error_code``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from sqlalchemy.exc import DBAPIError

from app.core.safe_errors import public_error, tool_failure
from app.services.tools.calendly_tools import CalendlyTools
from app.services.tools.crm_tools import CRMTools
from app.services.tools.gohighlevel_tools import GHL_BASE_URL, GoHighLevelTools
from app.services.tools.shopify_tools import ShopifyTools
from app.services.tools.sms_tools import TelnyxSMSTools, TwilioSMSTools

if TYPE_CHECKING:
    from collections.abc import Callable

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
        assert PHONE not in repr(result)
        assert KEY not in repr(result)


def test_public_error_is_message_plus_type_only() -> None:
    text = public_error(_db_error(), "Could not reach the provider")
    assert text == "Could not reach the provider (DBAPIError)"


def _status(
    code: int, body: dict[str, Any] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(code, json=body or {"message": f"echo {PHONE}"})


@pytest.mark.asyncio
async def test_shopify_only_404_is_not_found() -> None:
    base = f"https://demo-shop.myshopify.com/admin/api/{ShopifyTools.API_VERSION}"
    results = {}
    for code in (404, 500):
        tools = ShopifyTools("token", "demo-shop.myshopify.com")
        _wire(tools, base, _status(code))
        results[code] = await tools.get_order("1001")
    assert results[404]["error_code"] == "not_found"
    assert results[404]["error"] == "Order not found"
    assert results[500]["error_code"] == "unavailable"
    assert "not found" not in results[500]["error"].lower()
    assert results[500]["success"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("make", "base", "call"),
    [
        (lambda: CalendlyTools("t"), CalendlyTools.BASE_URL, lambda t: t.get_event("abc")),
        (
            lambda: ShopifyTools("t", "demo-shop.myshopify.com"),
            f"https://demo-shop.myshopify.com/admin/api/{ShopifyTools.API_VERSION}",
            lambda t: t.get_order_tracking("1001"),
        ),
        (
            lambda: TwilioSMSTools("ACtest", "s", "+15550100000"),
            f"{TwilioSMSTools.BASE_URL}/Accounts/ACtest",
            lambda t: t.get_message_status("SM123"),
        ),
    ],
    ids=["calendly", "shopify", "twilio"],
)
async def test_non_success_replies_log_service_and_status_without_the_body(
    make: Callable[[], Any], base: str, call: Callable[[Any], Any]
) -> None:
    from structlog.testing import capture_logs

    tools = make()
    _wire(tools, base, _status(403))
    with capture_logs() as logs:
        result = await call(tools)
    assert result["success"] is False
    entries = [e for e in logs if e.get("status_code") == 403 and e.get("service")]
    assert entries, logs
    assert PHONE not in repr(logs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "body", "expected"),
    [
        (
            400,
            {"code": 21211, "message": f"The 'To' number {PHONE} is not valid"},
            "invalid_number",
        ),
        (400, {"code": 20003, "message": f"auth {KEY}"}, "upstream_error"),
        (503, {"message": "down"}, "unavailable"),
    ],
)
async def test_twilio_send_reason_is_a_small_speakable_set(
    code: int, body: dict[str, Any], expected: str
) -> None:
    tools = TwilioSMSTools("ACtest", "secret", "+15550100000")
    _wire(tools, f"{TwilioSMSTools.BASE_URL}/Accounts/ACtest", _status(code, body))
    result = await tools.send_sms(PHONE, "hello")
    assert result["success"] is False
    assert result["error_code"] == expected
    assert PHONE not in repr(result)
    assert KEY not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "body", "expected"),
    [
        (422, {"errors": [{"code": "40310", "detail": f"bad {PHONE}"}]}, "invalid_number"),
        (422, {"errors": [{"code": "10001", "detail": f"x {PHONE}"}]}, "upstream_error"),
        (422, {"errors": [{"code": "40012", "detail": f"bad {PHONE}"}]}, "invalid_number"),
        (400, {"errors": [{"code": "40008", "detail": f"carrier {PHONE}"}]}, "unavailable"),
        (500, {"errors": []}, "unavailable"),
    ],
)
async def test_telnyx_send_reason_is_a_small_speakable_set(
    code: int, body: dict[str, Any], expected: str
) -> None:
    tools = TelnyxSMSTools("key", "+15550100000")
    _wire(tools, TelnyxSMSTools.BASE_URL, _status(code, body))
    result = await tools.send_sms(PHONE, "hello")
    assert result["success"] is False
    assert result["error_code"] == expected
    assert PHONE not in repr(result)


@pytest.mark.asyncio
async def test_crm_unknown_contact_does_not_echo_the_number() -> None:
    class _NoRows:
        def scalar_one_or_none(self) -> None:
            return None

    class _Db:
        async def execute(self, *_: Any, **__: Any) -> _NoRows:
            return _NoRows()

    result = await CRMTools(_Db(), user_id=1).book_appointment(  # type: ignore[arg-type]
        PHONE, "2030-01-02T10:00:00Z"
    )
    assert result["success"] is False
    assert PHONE not in repr(result)
