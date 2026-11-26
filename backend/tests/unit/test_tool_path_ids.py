"""Model-supplied ids must stay inside the path segment they were placed in.

A traversal id such as ``../calendars/events/abc`` used to be interpolated into the
vendor URL, normalised by httpx, and sent to a different endpoint with the
workspace owner's credentials. These tests capture the request URL with
``httpx.MockTransport`` (no network) for every tool that puts an argument in a
path.
"""

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.services.tools.calendly_tools import CalendlyTools
from app.services.tools.gohighlevel_tools import GHL_BASE_URL, GoHighLevelTools
from app.services.tools.path_ids import encode_path_id
from app.services.tools.shopify_tools import ShopifyTools
from app.services.tools.sms_tools import TelnyxSMSTools, TwilioSMSTools

SHOP = "demo-shop.myshopify.com"
SHOPIFY_BASE = f"https://{SHOP}/admin/api/{ShopifyTools.API_VERSION}"
TWILIO_BASE = f"{TwilioSMSTools.BASE_URL}/Accounts/ACtest"

BAD_IDS = [
    "../calendars/events/abc",
    "..",
    ".",
    "a/b",
    "a\\b",
    "a?x=1",
    "a#frag",
    "%2e%2e",
    "..%2fcalendars",
    "",
    "   ",
    "a\nb",
    "a\x00b",
    "a\tb",
]
GOOD_ID = "abc123-_XY"


def _wire(tools: Any, base_url: str, requests: list[httpx.Request]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404, json={})

    tools._client = httpx.AsyncClient(
        base_url=base_url, transport=httpx.MockTransport(handler), timeout=5.0
    )


def _ghl() -> GoHighLevelTools:
    return GoHighLevelTools("token", "loc1")


# (label, tools factory, base url, call(tools, id), expected path under base for GOOD_ID)
CASES: list[tuple[str, Callable[[], Any], str, Callable[[Any, str], Any], str]] = [
    (
        "ghl_get_contact",
        _ghl,
        GHL_BASE_URL,
        lambda t, i: t.ghl_get_contact(i),
        f"/contacts/{GOOD_ID}",
    ),
    (
        "ghl_update_contact",
        _ghl,
        GHL_BASE_URL,
        lambda t, i: t.ghl_update_contact(i, first_name="Sam"),
        f"/contacts/{GOOD_ID}",
    ),
    (
        "ghl_add_contact_tags",
        _ghl,
        GHL_BASE_URL,
        lambda t, i: t.ghl_add_contact_tags(i, ["vip"]),
        f"/contacts/{GOOD_ID}/tags",
    ),
    (
        "ghl_get_calendar_slots",
        _ghl,
        GHL_BASE_URL,
        lambda t, i: t.ghl_get_calendar_slots(i, "2030-01-02"),
        f"/calendars/{GOOD_ID}/free-slots",
    ),
    (
        "ghl_get_appointments",
        _ghl,
        GHL_BASE_URL,
        lambda t, i: t.ghl_get_appointments(i),
        f"/contacts/{GOOD_ID}/appointments",
    ),
    (
        "ghl_cancel_appointment",
        _ghl,
        GHL_BASE_URL,
        lambda t, i: t.ghl_cancel_appointment(i),
        f"/calendars/events/{GOOD_ID}",
    ),
    (
        "calendly_get_event",
        lambda: CalendlyTools("token"),
        CalendlyTools.BASE_URL,
        lambda t, i: t.get_event(i),
        f"/scheduled_events/{GOOD_ID}",
    ),
    (
        "calendly_cancel_event",
        lambda: CalendlyTools("token"),
        CalendlyTools.BASE_URL,
        lambda t, i: t.cancel_event(i, "reason"),
        f"/scheduled_events/{GOOD_ID}/cancellation",
    ),
    (
        "shopify_get_order",
        lambda: ShopifyTools("token", SHOP),
        SHOPIFY_BASE,
        lambda t, i: t.get_order(i),
        f"/orders/{GOOD_ID}.json",
    ),
    (
        "shopify_get_order_tracking",
        lambda: ShopifyTools("token", SHOP),
        SHOPIFY_BASE,
        lambda t, i: t.get_order_tracking(i),
        f"/orders/{GOOD_ID}/fulfillments.json",
    ),
    (
        "shopify_check_inventory",
        lambda: ShopifyTools("token", SHOP),
        SHOPIFY_BASE,
        lambda t, i: t.check_inventory(i),
        f"/products/{GOOD_ID}.json",
    ),
    (
        "twilio_get_message_status",
        lambda: TwilioSMSTools("ACtest", "secret", "+15550100"),
        TWILIO_BASE,
        lambda t, i: t.get_message_status(i),
        f"/Messages/{GOOD_ID}.json",
    ),
    (
        "telnyx_get_message_status",
        lambda: TelnyxSMSTools("key", "+15550100"),
        TelnyxSMSTools.BASE_URL,
        lambda t, i: t.get_message_status(i),
        f"/messages/{GOOD_ID}",
    ),
]
_IDS = [case[0] for case in CASES]


@pytest.mark.parametrize("bad_id", BAD_IDS)
@pytest.mark.parametrize(("label", "factory", "base", "call", "expected"), CASES, ids=_IDS)
async def test_unsafe_id_sends_nothing_and_fails(
    label: str,
    factory: Callable[[], Any],
    base: str,
    call: Callable[[Any, str], Any],
    expected: str,
    bad_id: str,
) -> None:
    requests: list[httpx.Request] = []
    tools = factory()
    _wire(tools, base, requests)

    result = await call(tools, bad_id)

    assert requests == [], f"{label} sent a request for {bad_id!r}: {requests[0].url}"
    assert result["success"] is False
    assert "invalid" in result["error"].lower()


@pytest.mark.parametrize(("label", "factory", "base", "call", "expected"), CASES, ids=_IDS)
async def test_normal_id_hits_the_same_url_as_before(
    label: str,
    factory: Callable[[], Any],
    base: str,
    call: Callable[[Any, str], Any],
    expected: str,
) -> None:
    requests: list[httpx.Request] = []
    tools = factory()
    _wire(tools, base, requests)

    await call(tools, GOOD_ID)

    assert requests, f"{label} sent no request"
    base_url = httpx.URL(base)
    assert requests[0].url.host == base_url.host
    assert requests[0].url.path == base_url.path.rstrip("/") + expected


async def test_shopify_order_hash_prefix_is_still_stripped() -> None:
    requests: list[httpx.Request] = []
    tools = ShopifyTools("token", SHOP)
    _wire(tools, SHOPIFY_BASE, requests)

    await tools.get_order("#1001")

    assert requests[0].url.path == f"/admin/api/{ShopifyTools.API_VERSION}/orders/1001.json"


@pytest.mark.parametrize("bad_id", BAD_IDS)
def test_encode_path_id_rejects_unsafe_values(bad_id: str) -> None:
    assert encode_path_id(bad_id) is None


@pytest.mark.parametrize("bad_value", [None, True, 1.5, ["a"], {"a": 1}, b"abc"])
def test_encode_path_id_rejects_non_ids(bad_value: Any) -> None:
    assert encode_path_id(bad_value) is None


def test_encode_path_id_passes_ordinary_ids_through() -> None:
    assert encode_path_id("abc123-_XY") == "abc123-_XY"
    assert encode_path_id(4521) == "4521"
    assert encode_path_id("  spaced ") == "spaced"
    assert encode_path_id("a b") == "a%20b"
    assert encode_path_id("a" * 300) is None
