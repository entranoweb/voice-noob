"""Reads an ElevenLabs conversation back as a normalised trace.

``GET /v1/convai/conversations/{id}`` returns the transcript with every tool
request and result. On a workflow agent, tool calls are nested: an agent turn
carries ``tool_results[]``, each with ``result_value.steps[]``; a step of type
``edge`` names the next workflow step, and a step of type ``nested_tools``
carries ``requests[]`` and ``results[]`` for the tools that step ran. On a
plain agent, ``tool_calls[]`` and ``tool_results[]`` sit directly on the turn.

A webhook tool's stored ``result_value`` can be no more than ``{"success": true}``
even when the tool answered in full: what the agent acts on is assigned to
variables, and the platform records those assignments beside the result as
``dynamic_variable_updates[]`` (``variable_name``, ``new_value``). They are read
into ``ToolCall.outputs``; without them "found" and "not found" look the same.
``parse_conversation`` handles both and is a pure function, so it is tested
against stored conversations with no network.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from synthiq_caller.regions import GLOBAL_API
from synthiq_caller.traces.base import ToolCall, Trace

API = GLOBAL_API

# Housekeeping the platform does for itself; not the agent's own tool use.
_PLATFORM_TOOLS = {"transfer_to_agent", "progress_workflow"}


def _as_object(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _is_platform(name: str) -> bool:
    return name in _PLATFORM_TOOLS or name.startswith("notify_condition")


def _ok(result: dict[str, Any], value: Any) -> bool:
    if result.get("is_error"):
        return False
    if isinstance(value, dict):
        if value.get("success") is False:
            return False
        if value.get("status") == "error":
            return False
    return True


def _error_text(result: dict[str, Any], value: Any) -> str:
    raw = result.get("raw_error_message") or ""
    if raw:
        return str(raw)
    if isinstance(value, dict) and value.get("success") is False:
        return str(value.get("code") or value.get("error") or "")
    return ""


def _outputs(result: dict[str, Any]) -> dict[str, Any]:
    """The variables this tool call set, last assignment winning."""
    found: dict[str, Any] = {}
    for update in result.get("dynamic_variable_updates") or []:
        if isinstance(update, dict) and update.get("variable_name"):
            found[str(update["variable_name"])] = update.get("new_value")
    return found


def _request(request: dict[str, Any]) -> dict[str, Any]:
    """Method and address of a webhook tool. Headers are never copied."""
    details = request.get("tool_details")
    if not isinstance(details, dict) or not details.get("url"):
        return {}
    return {"method": str(details.get("method") or ""), "url": str(details["url"])}


class TraceNotFinal(RuntimeError):
    """The platform had not finished writing the call when the wait ran out."""


def parse_conversation(conversation: dict[str, Any], call_ref: str = "") -> Trace:
    trace = Trace(
        call_ref=call_ref or str(conversation.get("conversation_id") or ""),
        termination=str(
            (conversation.get("metadata") or {}).get("termination_reason")
            or conversation.get("termination_reason")
            or ""
        ),
        final=conversation.get("status") in ("done", "failed"),
    )
    for turn in conversation.get("transcript") or []:
        at = turn.get("time_in_call_secs")
        node = str(turn.get("workflow_node_id") or "")
        if node and (not trace.step_path or trace.step_path[-1] != node):
            trace.step_path.append(node)
        direct_calls = {str(call.get("tool_name")): call for call in turn.get("tool_calls") or []}
        direct_requests = {
            name: _as_object(call.get("params_as_json")) or {}
            for name, call in direct_calls.items()
        }
        for result in turn.get("tool_results") or []:
            value = _as_object(result.get("result_value"))
            steps = value.get("steps") if isinstance(value, dict) else None
            if isinstance(steps, list):
                _walk_steps(trace, steps, at)
                continue
            name = str(result.get("tool_name") or "")
            if _is_platform(name):
                continue
            arguments = direct_requests.get(name, {})
            trace.tool_calls.append(
                ToolCall(
                    name=name,
                    arguments=arguments if isinstance(arguments, dict) else {},
                    result=value,
                    ok=_ok(result, value),
                    step_id=node,
                    t=at,
                    error=_error_text(result, value),
                    outputs=_outputs(result),
                    request=_request(direct_calls.get(name, {})),
                )
            )
    return trace


def _walk_steps(trace: Trace, steps: list[Any], at: Any) -> None:
    for step in steps:
        if not isinstance(step, dict):
            continue
        kind = step.get("type")
        if kind == "edge":
            target = str(step.get("target_node_id") or "")
            if target and (not trace.step_path or trace.step_path[-1] != target):
                trace.step_path.append(target)
        elif kind == "nested_tools":
            node = str(step.get("node_id") or "")
            requests = [
                r for r in step.get("requests") or [] if not _is_platform(str(r.get("tool_name")))
            ]
            results = [
                r for r in step.get("results") or [] if not _is_platform(str(r.get("tool_name")))
            ]
            by_id = {str(r.get("request_id")): r for r in requests if r.get("request_id")}
            for index, result in enumerate(results):
                # Pair by the platform's own request id; position is the fallback.
                request = by_id.get(str(result.get("request_id"))) or (
                    requests[index] if index < len(requests) else {}
                )
                value = _as_object(result.get("result_value"))
                arguments = _as_object(request.get("params_as_json")) or {}
                trace.tool_calls.append(
                    ToolCall(
                        name=str(result.get("tool_name") or request.get("tool_name") or ""),
                        arguments=arguments if isinstance(arguments, dict) else {},
                        result=value,
                        ok=_ok(result, value),
                        step_id=node,
                        t=at,
                        error=_error_text(result, value),
                        outputs=_outputs(result),
                        request=_request(request),
                    )
                )


class ElevenLabsTraceReader:
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
        """Fetch the conversation, waiting until the platform has finished writing it.

        A conversation is final when its status is ``done`` or ``failed``. Until
        then its transcript can be missing the last turns and every tool result,
        so one that never becomes final is an error, not a short trace.
        """
        return parse_conversation(await self.fetch_raw(call_ref), call_ref)

    async def fetch_raw(self, call_ref: str) -> dict[str, Any]:
        """The conversation exactly as the platform returned it, once it is final.

        ``read`` parses this; ``capture`` keeps it. Same wait, same refusals.
        """
        deadline = asyncio.get_running_loop().time() + self._wait_s
        last: dict[str, Any] = {}
        final = False
        async with httpx.AsyncClient(timeout=30, transport=self._transport) as client:
            while True:
                response = await client.get(
                    f"{self._api}/convai/conversations/{call_ref}",
                    headers={"xi-api-key": self._key},
                )
                if response.status_code == 200:
                    last = response.json()
                    if last.get("status") in ("done", "failed"):
                        final = True
                        break
                elif response.status_code not in (404, 425):
                    response.raise_for_status()
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(self._poll_s)
        if not last:
            raise RuntimeError(f"conversation {call_ref} could not be read")
        if not final:
            raise TraceNotFinal(
                f"conversation {call_ref} was still {last.get('status')!r} "
                f"after {self._wait_s:.0f}s"
            )
        return last

    async def fetch_audio(self, call_ref: str) -> bytes:
        """The call's recording as the platform sends it (one mixed track, format unstated)."""
        async with httpx.AsyncClient(timeout=120, transport=self._transport) as client:
            response = await client.get(
                f"{self._api}/convai/conversations/{call_ref}/audio",
                headers={"xi-api-key": self._key},
            )
            response.raise_for_status()
            return response.content
