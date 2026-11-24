"""The Retell and Vapi trace readers, on doc-derived payloads. No network.

The fixtures were written by hand from each platform's published field list.
They are not recordings of a real account, so these tests show that the readers
follow the documented shape, not that the documentation matches production.
"""

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from synthiq_caller.traces import retell, vapi
from synthiq_caller.traces.common import (
    UNAVAILABLE,
    UNAVAILABLE_KEY,
    TraceUnavailableError,
    unavailable_calls,
)

FIXTURES = Path(__file__).parent / "fixtures"


def stored(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


@pytest.fixture
def retell_call() -> dict[str, Any]:
    return stored("retell_get_call_doc_derived.json")


@pytest.fixture
def vapi_report() -> dict[str, Any]:
    return stored("vapi_end_of_call_report_doc_derived.json")


# --- Retell -----------------------------------------------------------------


def test_retell_pairs_each_request_with_its_result(retell_call: dict[str, Any]) -> None:
    trace = retell.parse_call(retell_call)

    assert trace.call_ref == "Jabr9TXYYJHfvl6Syypi88rdAHYHmcq6"
    assert trace.termination == "agent_hangup"
    assert trace.step_path == ["Identify", "Book"]

    lookup, book, end = trace.tool_calls
    assert lookup.name == "identify_patient"
    assert lookup.arguments == {"name": "Alex Example", "date_of_birth": "1987-07-05"}
    assert lookup.result == {"success": True, "status": "unique", "patient_ref": "p_1"}
    assert lookup.ok
    assert lookup.step_id == "Identify"

    assert book.name == "book_appointment"
    assert not book.ok
    assert book.error == "SLOT_NOT_AVAILABLE"
    assert book.step_id == "Book"
    assert end.name == "end_call"


def test_retell_tool_time_is_the_end_of_the_speech_before_it(retell_call: dict[str, Any]) -> None:
    lookup, book, _ = retell.parse_call(retell_call).tool_calls
    # Retell puts no time on a tool entry; the last word before each call ended at these seconds.
    assert lookup.t == 7.6
    assert book.t == 11.6


def test_retell_request_without_a_result_is_marked_not_guessed(retell_call: dict[str, Any]) -> None:
    trace = retell.parse_call(retell_call)
    end = trace.tool_calls[-1]
    assert end.result == UNAVAILABLE
    assert not end.ok
    assert end.error.startswith(UNAVAILABLE)
    assert unavailable_calls(trace) == [end]


def test_retell_platform_flag_wins_over_the_reply_text(retell_call: dict[str, Any]) -> None:
    for entry in retell_call["transcript_with_tool_calls"]:
        if entry.get("tool_call_id") == "call_lookup_1" and entry["role"] == "tool_call_result":
            entry["successful"] = False
    lookup = retell.parse_call(retell_call).tool_calls[0]
    assert not lookup.ok


def test_retell_unreadable_arguments_are_marked(retell_call: dict[str, Any]) -> None:
    for entry in retell_call["transcript_with_tool_calls"]:
        if entry.get("tool_call_id") == "call_lookup_1" and entry["role"] == "tool_call_invocation":
            entry["arguments"] = "[REDACTED]"
    trace = retell.parse_call(retell_call)
    lookup = trace.tool_calls[0]
    assert set(lookup.arguments) == {UNAVAILABLE_KEY}
    assert lookup.ok, "the result is still known"
    assert lookup in unavailable_calls(trace)


def test_retell_uses_the_scrubbed_list_when_it_is_the_only_one(retell_call: dict[str, Any]) -> None:
    retell_call["scrubbed_transcript_with_tool_calls"] = retell_call.pop(
        "transcript_with_tool_calls"
    )
    retell_call["data_storage_setting"] = "everything_except_pii"
    assert [c.name for c in retell.parse_call(retell_call).tool_calls] == [
        "identify_patient",
        "book_appointment",
        "end_call",
    ]


def test_retell_with_no_transcript_kept_raises(retell_call: dict[str, Any]) -> None:
    del retell_call["transcript_with_tool_calls"]
    retell_call["data_storage_setting"] = "basic_attributes_only"
    with pytest.raises(TraceUnavailableError, match="basic_attributes_only"):
        retell.parse_call(retell_call)


def test_retell_call_that_used_no_tools_is_an_empty_trace_not_an_error(
    retell_call: dict[str, Any],
) -> None:
    retell_call["transcript_with_tool_calls"] = [
        e for e in retell_call["transcript_with_tool_calls"] if e["role"] in ("agent", "user")
    ]
    trace = retell.parse_call(retell_call)
    assert trace.tool_calls == []
    assert trace.step_path == []


async def test_retell_reader_waits_for_the_call_to_end(retell_call: dict[str, Any]) -> None:
    ongoing = {"call_id": retell_call["call_id"], "call_status": "ongoing"}
    replies = [httpx.Response(404), httpx.Response(200, json=ongoing)]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return replies.pop(0) if replies else httpx.Response(200, json=retell_call)

    reader = retell.RetellTraceReader(
        "test-key", poll_s=0.0, transport=httpx.MockTransport(handler)
    )
    trace = await reader.read("Jabr9TXYYJHfvl6Syypi88rdAHYHmcq6")

    assert len(seen) == 3
    assert seen[0].url == "https://api.retellai.com/v2/get-call/Jabr9TXYYJHfvl6Syypi88rdAHYHmcq6"
    assert seen[0].headers["authorization"] == "Bearer test-key"
    assert len(trace.tool_calls) == 3


async def test_retell_reader_raises_on_a_refused_key() -> None:
    reader = retell.RetellTraceReader(
        "bad", poll_s=0.0, transport=httpx.MockTransport(lambda _: httpx.Response(401))
    )
    with pytest.raises(httpx.HTTPStatusError):
        await reader.read("any")


# --- Vapi -------------------------------------------------------------------


def test_vapi_report_pairs_each_request_with_its_result(vapi_report: dict[str, Any]) -> None:
    trace = vapi.parse_call(vapi_report)

    assert trace.call_ref == "call_doc_example_1"
    assert trace.termination == "assistant-ended-call"
    assert trace.step_path == []

    lookup, book, note = trace.tool_calls
    assert lookup.name == "identify_patient"
    assert lookup.arguments == {"name": "Alex Example", "postcode": "AB12 3CD"}
    assert lookup.result == {"success": True, "status": "unique", "patient_ref": "p_1"}
    assert lookup.ok
    assert lookup.t == 12.5

    assert book.name == "book_appointment"
    assert not book.ok
    assert book.error == "SLOT_NOT_AVAILABLE"
    assert book.t == 19.5
    assert note.name == "add_note"


def test_vapi_request_without_a_result_is_marked_not_guessed(vapi_report: dict[str, Any]) -> None:
    trace = vapi.parse_call(vapi_report)
    note = trace.tool_calls[-1]
    assert note.arguments == {"text": "prefers mornings"}
    assert note.result == UNAVAILABLE
    assert not note.ok
    assert unavailable_calls(trace) == [note]


def test_vapi_reads_the_same_trace_from_the_call_object(vapi_report: dict[str, Any]) -> None:
    message = vapi_report["message"]
    call_object = {
        "id": "call_doc_example_1",
        "status": "ended",
        "endedReason": message["endedReason"],
        "artifact": copy.deepcopy(message["artifact"]),
    }
    from_report = vapi.parse_call(vapi_report)
    assert vapi.parse_call(call_object) == from_report
    assert vapi.parse_call(message) == from_report


def test_vapi_falls_back_to_the_top_level_message_list(vapi_report: dict[str, Any]) -> None:
    message = vapi_report["message"]
    call_object = {"id": "c1", "status": "ended", "messages": message["artifact"]["messages"]}
    assert len(vapi.parse_call(call_object).tool_calls) == 3


def test_vapi_does_not_depend_on_the_role_text(vapi_report: dict[str, Any]) -> None:
    for entry in vapi_report["message"]["artifact"]["messages"]:
        if "toolCalls" in entry or "toolCallId" in entry:
            entry["role"] = "something-else"
    assert len(vapi.parse_call(vapi_report).tool_calls) == 3


def test_vapi_arguments_already_an_object_are_kept(vapi_report: dict[str, Any]) -> None:
    for entry in vapi_report["message"]["artifact"]["messages"]:
        for request in entry.get("toolCalls", []):
            request["function"]["arguments"] = json.loads(request["function"]["arguments"])
    lookup = vapi.parse_call(vapi_report).tool_calls[0]
    assert lookup.arguments == {"name": "Alex Example", "postcode": "AB12 3CD"}


def test_vapi_plain_text_error_result_is_kept_as_text(vapi_report: dict[str, Any]) -> None:
    for entry in vapi_report["message"]["artifact"]["messages"]:
        if entry.get("toolCallId") == "call_lookup_1":
            entry["result"] = "No result returned."
    lookup = vapi.parse_call(vapi_report).tool_calls[0]
    # Vapi states no success flag, and plain text is not something to read failure into.
    assert lookup.result == "No result returned."
    assert lookup.ok


def test_vapi_with_no_messages_kept_raises(vapi_report: dict[str, Any]) -> None:
    del vapi_report["message"]["artifact"]["messages"]
    with pytest.raises(TraceUnavailableError):
        vapi.parse_call(vapi_report)


async def test_vapi_reader_waits_for_the_call_to_end(vapi_report: dict[str, Any]) -> None:
    message = vapi_report["message"]
    ended = {
        "id": "call_doc_example_1",
        "status": "ended",
        "endedReason": message["endedReason"],
        "artifact": message["artifact"],
    }
    replies = [httpx.Response(200, json={"id": "call_doc_example_1", "status": "in-progress"})]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return replies.pop(0) if replies else httpx.Response(200, json=ended)

    reader = vapi.VapiTraceReader("test-key", poll_s=0.0, transport=httpx.MockTransport(handler))
    trace = await reader.read("call_doc_example_1")

    assert len(seen) == 2
    assert seen[0].url == "https://api.vapi.ai/call/call_doc_example_1"
    assert seen[0].headers["authorization"] == "Bearer test-key"
    assert trace.termination == "assistant-ended-call"
    assert len(trace.tool_calls) == 3
