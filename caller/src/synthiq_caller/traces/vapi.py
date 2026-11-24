"""Reads a Vapi call back as a normalised trace.

Two sources give the same message list:

- ``GET /call/{id}`` returns the call, with ``artifact.messages`` (and an older
  top-level ``messages``).
- The ``end-of-call-report`` server message carries ``message.artifact.messages``.

``parse_call`` takes either: the call object, the whole webhook body, or the
``message`` inside it. Each entry of ``artifact.messages`` is one of five
shapes in Vapi's API description:

- user / bot / system speech: ``message``, ``time``, ``secondsFromStart``.
- a tool-call message: ``toolCalls[]``, each with ``id`` and
  ``function.name`` / ``function.arguments`` (a JSON object as a string).
- a tool-call result: ``toolCallId``, ``name``, ``result`` (a string).

Entries are told apart by those fields, not by the text of ``role``, which the
API description types as a plain string.

Field names are from Vapi's published API description and the end-of-call-report
page as read on 24 November 2026. The reader has not been run against a real
Vapi account.

Limits that come from the platform, marked and never filled:

- Vapi gives no success flag for a tool. ``ToolCall.ok`` is read from the
  tool's own reply (see ``common.inferred_ok``).
- Messages exist only when the assistant's artifact plan kept them and the
  account is not on zero data retention. With neither list present,
  ``parse_call`` raises ``TraceUnavailableError``.
- There is no flow step on a message, so ``step_path`` is empty.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from synthiq_caller.traces.base import ToolCall, Trace
from synthiq_caller.traces.common import (
    TraceUnavailableError,
    arguments_of,
    as_object,
    error_text,
    inferred_ok,
    missing_result,
)

API = "https://api.vapi.ai"


def _unwrap(payload: dict[str, Any]) -> dict[str, Any]:
    """The object that holds ``artifact``: the webhook's ``message``, or the call itself."""
    inner = payload.get("message")
    if isinstance(inner, dict) and ("artifact" in inner or inner.get("type")):
        return inner
    return payload


def _messages(source: dict[str, Any]) -> list[Any]:
    artifact = source.get("artifact")
    if isinstance(artifact, dict) and isinstance(artifact.get("messages"), list):
        return list(artifact["messages"])
    if isinstance(source.get("messages"), list):
        return list(source["messages"])
    raise TraceUnavailableError(
        "Vapi returned no artifact.messages for this call "
        "(the artifact plan may not keep them, or the account keeps no data)"
    )


def _call_id(source: dict[str, Any]) -> str:
    call = source.get("call")
    if isinstance(call, dict) and call.get("id"):
        return str(call["id"])
    return str(source.get("id") or "")


def _seconds(entry: dict[str, Any]) -> float | None:
    value = entry.get("secondsFromStart")
    return float(value) if isinstance(value, int | float) else None


def parse_call(payload: dict[str, Any], call_ref: str = "") -> Trace:
    """The trace of one Vapi call object or end-of-call report. Pure: no network."""
    source = _unwrap(payload)
    trace = Trace(
        call_ref=call_ref or _call_id(source),
        termination=str(source.get("endedReason") or ""),
    )
    # tool call id -> where the call sits in trace.tool_calls, until its result arrives.
    waiting: dict[str, int] = {}
    for entry in _messages(source):
        if not isinstance(entry, dict):
            continue
        requests = entry.get("toolCalls")
        if isinstance(requests, list):
            at = _seconds(entry)
            for request in requests:
                if not isinstance(request, dict):
                    continue
                function = request.get("function")
                function = function if isinstance(function, dict) else {}
                waiting[str(request.get("id") or "")] = len(trace.tool_calls)
                trace.tool_calls.append(
                    missing_result(
                        name=str(function.get("name") or ""),
                        arguments=arguments_of(function.get("arguments")),
                        t=at,
                        step="",
                    )
                )
        elif "toolCallId" in entry:
            value = as_object(entry.get("result"))
            ok = inferred_ok(value)
            error = "" if ok else error_text(value)
            index = waiting.pop(str(entry.get("toolCallId") or ""), None)
            if index is None:
                # A result whose request the platform did not return.
                trace.tool_calls.append(
                    ToolCall(
                        name=str(entry.get("name") or ""),
                        arguments=arguments_of(None),
                        result=value,
                        ok=ok,
                        t=_seconds(entry),
                        error=error,
                    )
                )
                continue
            asked = trace.tool_calls[index]
            trace.tool_calls[index] = ToolCall(
                name=asked.name or str(entry.get("name") or ""),
                arguments=asked.arguments,
                result=value,
                ok=ok,
                t=asked.t,
                error=error,
            )
    return trace


class VapiTraceReader:
    def __init__(
        self,
        api_key: str,
        *,
        api: str = API,
        wait_s: float = 60.0,
        poll_s: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = api_key
        self._api = api
        self._wait_s = wait_s
        self._poll_s = poll_s
        self._transport = transport

    async def read(self, call_ref: str) -> Trace:
        """Fetch the call, waiting until the platform reports it ended."""
        deadline = asyncio.get_running_loop().time() + self._wait_s
        last: dict[str, Any] = {}
        async with httpx.AsyncClient(timeout=30, transport=self._transport) as client:
            while True:
                response = await client.get(
                    f"{self._api}/call/{call_ref}",
                    headers={"Authorization": f"Bearer {self._key}"},
                )
                if response.status_code == 200:
                    last = response.json()
                    if last.get("status") == "ended":
                        break
                elif response.status_code != 404:
                    response.raise_for_status()
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(self._poll_s)
        if not last:
            raise RuntimeError(f"Vapi call {call_ref} could not be read")
        return parse_call(last, call_ref)
