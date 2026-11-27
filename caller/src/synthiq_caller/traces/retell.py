"""Reads a Retell call back as a normalised trace.

``GET /v2/get-call/{call_id}`` returns ``transcript_with_tool_calls``: one list
holding what was said and what the agent did, in order. Each entry is told
apart by ``role``:

- ``agent`` / ``user`` / ``transfer_target``: speech, with ``words[].start`` and
  ``words[].end`` in seconds of call audio.
- ``tool_call_invocation``: ``tool_call_id``, ``name``, ``arguments`` (a JSON
  object as a string).
- ``tool_call_result``: ``tool_call_id``, ``content`` (a string), and
  ``successful`` when the platform sets it.
- ``node_transition``: ``new_node_id`` / ``new_node_name`` on a flow agent.
- ``dtmf``, ``sms``, ``injected``: not tool use; skipped.

Field names are from the Get Call reference as read on 24 November 2026. The
reader has not been run against a real Retell account.

Three limits come from the platform, and the reader marks them, never fills them:

- Tool entries carry no time of their own. ``ToolCall.t`` is the end of the
  last word spoken before the call, which is a lower bound, or ``None`` when
  nothing was spoken first.
- With ``data_storage_setting`` at ``basic_attributes_only`` there is no
  transcript at all; ``parse_call`` raises ``TraceUnavailableError``.
- With ``everything_except_pii`` only the ``scrubbed_`` list may exist. It is
  used, and argument values in it may be masked by the platform.
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

API = "https://api.retellai.com"

_SPEECH_ROLES = {"agent", "user", "transfer_target"}
_FINISHED = {"ended", "error"}


def _entries(call: dict[str, Any]) -> list[Any]:
    for key in ("transcript_with_tool_calls", "scrubbed_transcript_with_tool_calls"):
        value = call.get(key)
        if isinstance(value, list):
            return value
    setting = call.get("data_storage_setting") or "not stated"
    raise TraceUnavailableError(
        "Retell returned no transcript_with_tool_calls for call "
        f"{call.get('call_id') or '?'} (data_storage_setting: {setting})"
    )


def _spoken_until(entry: dict[str, Any]) -> float | None:
    ends: list[float] = []
    for word in entry.get("words") or []:
        end = word.get("end") if isinstance(word, dict) else None
        if isinstance(end, int | float):
            ends.append(float(end))
    return max(ends) if ends else None


def _result(entry: dict[str, Any]) -> tuple[Any, bool, str]:
    value = as_object(entry.get("content"))
    stated = entry.get("successful")
    ok = stated if isinstance(stated, bool) else inferred_ok(value)
    return value, ok, "" if ok else error_text(value)


def parse_call(call: dict[str, Any], call_ref: str = "") -> Trace:
    """The trace of one Retell call object. Pure: no network."""
    trace = Trace(
        call_ref=call_ref or str(call.get("call_id") or ""),
        termination=str(call.get("disconnection_reason") or ""),
        final=call.get("call_status") in _FINISHED,
    )
    # tool_call_id -> where the call sits in trace.tool_calls, until its result arrives.
    waiting: dict[str, int] = {}
    step = ""
    clock: float | None = None
    for entry in _entries(call):
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        if role == "agent" and isinstance(entry.get("content"), str) and entry["content"].strip():
            trace.agent_lines.append(entry["content"])
        if role in _SPEECH_ROLES:
            until = _spoken_until(entry)
            if until is not None:
                clock = until if clock is None else max(clock, until)
        elif role == "node_transition":
            step = str(entry.get("new_node_name") or entry.get("new_node_id") or "")
            if step and (not trace.step_path or trace.step_path[-1] != step):
                trace.step_path.append(step)
        elif role == "tool_call_invocation":
            call_id = str(entry.get("tool_call_id") or "")
            waiting[call_id] = len(trace.tool_calls)
            trace.tool_calls.append(
                missing_result(
                    name=str(entry.get("name") or ""),
                    arguments=arguments_of(entry.get("arguments")),
                    t=clock,
                    step=step,
                )
            )
        elif role == "tool_call_result":
            call_id = str(entry.get("tool_call_id") or "")
            value, ok, error = _result(entry)
            index = waiting.pop(call_id, None)
            if index is None:
                # A result whose request the platform did not return.
                trace.tool_calls.append(
                    ToolCall(
                        name="",
                        arguments=arguments_of(None),
                        result=value,
                        ok=ok,
                        step_id=step,
                        t=clock,
                        error=error,
                    )
                )
                continue
            asked = trace.tool_calls[index]
            trace.tool_calls[index] = ToolCall(
                name=asked.name,
                arguments=asked.arguments,
                result=value,
                ok=ok,
                step_id=asked.step_id,
                t=asked.t,
                error=error,
            )
    return trace


class RetellTraceReader:
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
        """Fetch the call, waiting until the platform reports it finished."""
        deadline = asyncio.get_running_loop().time() + self._wait_s
        last: dict[str, Any] = {}
        async with httpx.AsyncClient(timeout=30, transport=self._transport) as client:
            while True:
                response = await client.get(
                    f"{self._api}/v2/get-call/{call_ref}",
                    headers={"Authorization": f"Bearer {self._key}"},
                )
                if response.status_code == 200:
                    last = response.json()
                    if last.get("call_status") in _FINISHED:
                        break
                elif response.status_code != 404:
                    response.raise_for_status()
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(self._poll_s)
        if not last:
            raise RuntimeError(f"Retell call {call_ref} could not be read")
        return parse_call(last, call_ref)
