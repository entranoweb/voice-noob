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
import structlog.testing

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
async def test_body_is_exactly_the_documented_pascal_case_keys() -> None:
    _, request = await _dial({"call_sid": "v3:abc"})
    body = json.loads(request.content)
    # CreateTexmlCall accepts From, To, Url, Method, Texml. No StatusCallback*:
    # progress comes from the TeXML application's own status_callback.
    assert body == {"To": TO, "From": FROM, "Url": ANSWER}
    assert "record_id=r1" in body["Url"]


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
        {"sid": "v3:other"},
        {"call_control_id": "v3:other"},
        {"data": {"sid": "v3:nested"}},
        {"from": FROM, "to": TO, "status": "queued"},
    ],
)
async def test_only_the_flat_call_sid_is_an_id_and_its_absence_is_logged(
    response: dict[str, Any],
) -> None:
    with structlog.testing.capture_logs() as logs:
        info, _ = await _dial(response)
    assert info.call_id == ""
    assert info.call_control_id == ""
    assert any(
        log["event"] == "telnyx_create_call_response_has_no_call_sid"
        and log["log_level"] == "error"
        for log in logs
    )


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


def _app(app_id: str, name: str = "voice-noob-inbound") -> dict[str, str]:
    return {"id": app_id, "friendly_name": name}


@pytest.mark.asyncio
async def test_the_application_lookup_pages_until_it_finds_the_app() -> None:
    gets: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"call_sid": "v3:p"})
        gets.append(request)
        page = int(request.url.params["page[number]"])
        data = [_app("noise", "other-app")] if page == 1 else [_app(APP_ID)]
        return httpx.Response(200, json={"data": data, "meta": {"total_pages": 2}})

    service = _service(handler)
    info = await service.initiate_call(to_number=TO, from_number=FROM, webhook_url=ANSWER)
    assert info.call_id == "v3:p"
    assert [g.url.params["page[number]"] for g in gets] == ["1", "2"]
    assert gets[0].url.params["page[size]"] == "250"
    assert gets[0].url.params["filter[friendly_name]"] == "voice-noob-inbound"


@pytest.mark.asyncio
async def test_two_applications_with_the_name_are_refused_not_guessed() -> None:
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request)
        return httpx.Response(200, json={"data": [_app("a1"), _app("a2")]})

    service = _service(handler)
    with pytest.raises(TelnyxConfigurationError, match="2 Telnyx TeXML applications"):
        await service.initiate_call(to_number=TO, from_number=FROM, webhook_url=ANSWER)
    assert posts == []


@pytest.mark.asyncio
async def test_a_created_application_carries_the_documented_status_callback_fields() -> None:
    sent: list[tuple[str, str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        sent.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(201, json={"data": {"id": "new-app"}})

    service = _service(handler)
    app_id = await service._get_or_create_texml_application(
        "https://voice.example.test/webhooks/telnyx/voice",
        "https://voice.example.test/webhooks/telnyx/status",
    )
    assert app_id == "new-app"
    method, path, body = sent[0]
    assert (method, path) == ("POST", "/v2/texml_applications")
    assert body["status_callback"] == "https://voice.example.test/webhooks/telnyx/status"
    assert body["status_callback_method"] == "post"
    assert body["voice_method"] == "post"
    assert "status_callback_url" not in body


@pytest.mark.asyncio
async def test_an_existing_application_is_patched_with_the_documented_fields() -> None:
    sent: list[tuple[str, str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [_app(APP_ID)]})
        sent.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"data": {"id": APP_ID}})

    service = _service(handler)
    await service._get_or_create_texml_application(
        "https://voice.example.test/webhooks/telnyx/voice",
        "https://voice.example.test/webhooks/telnyx/status",
    )
    method, path, body = sent[0]
    assert (method, path) == ("PATCH", f"/v2/texml_applications/{APP_ID}")
    assert body["status_callback"] == "https://voice.example.test/webhooks/telnyx/status"
    assert body["status_callback_method"] == "post"
