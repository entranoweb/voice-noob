"""The outbound TeXML request against Telnyx's documented shape.

Documented (Telnyx OpenAPI, operation CreateTexmlCall): ``POST
/texml/calls/{connection_id}`` where the id is the TeXML application's, a body
with PascalCase ``From``/``To``/``Url``, and a flat response carrying
``call_sid``. No network: an ``httpx.MockTransport`` answers.
"""

import json
from typing import Any

import httpx
import pytest

from app.services.telephony.telnyx_service import TelnyxConfigurationError, TelnyxService

FROM = "+15550100001"
TO = "+15550100002"
ANSWER = "https://voice.example.test/webhooks/telnyx/answer?agent_id=a1&record_id=r1"
STATUS = "https://voice.example.test/webhooks/telnyx/status?agent_id=a1&record_id=r1"
APP_ID = "1293384261075731499"

APPS = {
    "data": [
        {"id": "other", "friendly_name": "x"},
        {"id": APP_ID, "friendly_name": "voice-noob-inbound"},
    ]
}


def _service(handler: Any) -> TelnyxService:
    service = TelnyxService("key")
    service._http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.telnyx.com/v2"
    )
    return service


def _handler(calls: list[httpx.Request], response: dict[str, Any]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/v2/texml_applications":
            return httpx.Response(200, json=APPS)
        return httpx.Response(200, json=response)

    return handler


async def _dial(response: dict[str, Any]) -> tuple[Any, httpx.Request]:
    calls: list[httpx.Request] = []
    service = _service(_handler(calls, response))
    info = await service.initiate_call(to_number=TO, from_number=FROM, webhook_url=ANSWER)
    posts = [c for c in calls if c.method == "POST"]
    assert len(posts) == 1
    return info, posts[0]


@pytest.mark.asyncio
async def test_posts_json_to_the_texml_application_scoped_path() -> None:
    _, request = await _dial({"call_sid": "v3:abc", "from": FROM, "to": TO, "status": "queued"})
    assert request.method == "POST"
    assert str(request.url) == f"https://api.telnyx.com/v2/texml/calls/{APP_ID}"
    assert request.headers["content-type"] == "application/json"


@pytest.mark.asyncio
async def test_body_uses_the_documented_pascal_case_keys() -> None:
    _, request = await _dial({"call_sid": "v3:abc"})
    body = json.loads(request.content)
    assert body["To"] == TO
    assert body["From"] == FROM
    assert body["Url"] == ANSWER
    assert "record_id=r1" in body["Url"]
    assert body["StatusCallback"] == STATUS
    assert body["StatusCallbackMethod"] == "POST"
    for old in ("to", "from", "connection_id", "texml_url", "status_callback"):
        assert old not in body


@pytest.mark.asyncio
async def test_the_call_id_is_the_documented_flat_call_sid() -> None:
    info, _ = await _dial({"call_sid": "v3:abc", "from": FROM, "to": TO, "status": "queued"})
    assert info.call_id == "v3:abc"
    assert info.call_control_id == "v3:abc"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"data": {"call_sid": "v3:nested"}},
        {"data": {"sid": "v3:nested"}},
        {"data": {"call_control_id": "v3:nested"}},
    ],
)
async def test_other_response_shapes_are_still_read(response: dict[str, Any]) -> None:
    info, _ = await _dial(response)
    assert info.call_id == "v3:nested"


@pytest.mark.asyncio
async def test_a_response_with_no_id_is_not_reported_as_an_id() -> None:
    info, _ = await _dial({"from": FROM, "to": TO, "status": "queued"})
    assert info.call_id == ""


@pytest.mark.asyncio
async def test_no_texml_application_is_a_clear_configuration_error() -> None:
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request)
        return httpx.Response(200, json={"data": []})

    service = _service(handler)
    with pytest.raises(TelnyxConfigurationError, match="TeXML application"):
        await service.initiate_call(to_number=TO, from_number=FROM, webhook_url=ANSWER)
    assert posts == []
