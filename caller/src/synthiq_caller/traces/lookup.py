"""Finding a phone call's trace when the carrier's call id is all the caller has.

Over a socket the engine hands the caller its own conversation id. Over a
telephone line it does not: the caller dials a number, and the id it holds is
the carrier's, for its own outbound leg. The agent's platform files the same
call under a different id, for the inbound leg it answered.

So the trace has to be looked for. A ``ConversationFinder`` does that for one
platform; ``LookupTraceReader`` puts a finder in front of that platform's
ordinary trace reader, so the rest of the caller reads a phone call's trace
exactly as it reads any other.

The rule that matters: a lookup that does not find exactly one call fails. It
never picks the nearest, the newest or the first. A call graded against another
call's trace would be worse than a call not graded at all.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from synthiq_caller.numbers import same_number
from synthiq_caller.regions import GLOBAL_API
from synthiq_caller.traces.base import Trace, TraceReader

ELEVENLABS_API = GLOBAL_API


class TraceNotFound(RuntimeError):
    """No single call on the agent's platform could be matched to the phone call."""


@dataclass(frozen=True)
class PhoneCall:
    """What the caller knows about a call it placed."""

    carrier_ref: str
    to_number: str
    from_number: str
    dialled_at_unix: float
    ended_at_unix: float = 0.0

    @staticmethod
    def from_log(carrier_ref: str, log: dict[str, Any]) -> PhoneCall | None:
        """From a stored run's ``phone`` block; None when it cannot place the call in time."""
        dialled = float(log.get("dialled_at_unix") or 0.0)
        if not dialled or not log.get("to_number"):
            return None
        return PhoneCall(
            carrier_ref=carrier_ref,
            to_number=str(log.get("to_number") or ""),
            from_number=str(log.get("from_number") or ""),
            dialled_at_unix=dialled,
            ended_at_unix=float(log.get("ended_at_unix") or 0.0),
        )


class ConversationFinder(Protocol):
    async def find(self, call: PhoneCall) -> str:
        """The platform's own id for ``call``. Raises ``TraceNotFound``."""
        ...


class ElevenLabsNumberTimeFinder:
    """Match on the number that was dialled, the number it was dialled from, and the time.

    ``GET /v1/convai/conversations`` lists conversations that started inside a
    window around the call. Each candidate's ``metadata.phone_call`` then says
    which of the agent's numbers took it (``agent_number``), who rang
    (``external_number``) and which way it went (``direction``). Exactly one
    inbound conversation must match.
    """

    def __init__(
        self,
        api_key: str,
        *,
        agent_id: str = "",
        window_s: float = 120.0,
        match_caller_number: bool = True,
        api: str = ELEVENLABS_API,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._key = api_key
        self._agent_id = agent_id
        self._window_s = window_s
        self._match_caller = match_caller_number
        self._api = api
        self._client = client

    async def _get(self, client: httpx.AsyncClient, path: str, **params: Any) -> dict[str, Any]:
        response = await client.get(
            f"{self._api}{path}", params=params or None, headers={"xi-api-key": self._key}
        )
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body

    async def _candidates(self, client: httpx.AsyncClient, call: PhoneCall) -> list[str]:
        params: dict[str, Any] = {
            "call_start_after_unix": int(call.dialled_at_unix - self._window_s),
            "call_start_before_unix": int(
                (call.ended_at_unix or call.dialled_at_unix) + self._window_s
            ),
            "page_size": 100,
        }
        if self._agent_id:
            params["agent_id"] = self._agent_id
        found: list[str] = []
        for _ in range(5):  # five pages of a hundred is far more than one window holds
            page = await self._get(client, "/convai/conversations", **params)
            found.extend(str(c.get("conversation_id") or "") for c in page.get("conversations", []))
            if not page.get("has_more") or not page.get("next_cursor"):
                break
            params["cursor"] = page["next_cursor"]
        return [c for c in found if c]

    def _matches(self, conversation: dict[str, Any], call: PhoneCall) -> bool:
        phone = (conversation.get("metadata") or {}).get("phone_call") or {}
        if not phone or str(phone.get("direction") or "inbound") != "inbound":
            return False
        if not same_number(str(phone.get("agent_number") or ""), call.to_number):
            return False
        if not self._match_caller:
            return True
        return same_number(str(phone.get("external_number") or ""), call.from_number)

    async def find(self, call: PhoneCall) -> str:
        client = self._client or httpx.AsyncClient(timeout=30)
        try:
            candidates = await self._candidates(client, call)
            matched = [
                candidate
                for candidate in candidates
                if self._matches(
                    await self._get(client, f"/convai/conversations/{candidate}"), call
                )
            ]
        finally:
            if self._client is None:
                await client.aclose()
        whose = f"to {call.to_number}" + (f" from {call.from_number}" if self._match_caller else "")
        if not matched:
            raise TraceNotFound(
                f"trace not found: no inbound conversation {whose} started within "
                f"{self._window_s:.0f}s of the call ({len(candidates)} conversation(s) in the "
                "window)"
            )
        if len(matched) > 1:
            raise TraceNotFound(
                f"trace not found: {len(matched)} conversations {whose} started within "
                f"{self._window_s:.0f}s of the call; refusing to guess which is this one"
            )
        return matched[0]


class LookupTraceReader:
    """A trace reader for calls known only by the carrier's id."""

    def __init__(
        self,
        finder: ConversationFinder,
        inner: TraceReader,
        call_for: Callable[[str], PhoneCall | None],
        *,
        find_wait_s: float = 45.0,
        retry_s: float = 3.0,
        wait: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._finder = finder
        self._inner = inner
        self._call_for = call_for
        self._find_wait_s = find_wait_s
        self._retry_s = retry_s
        self._wait = wait
        self._found: dict[str, str] = {}

    async def _find(self, call: PhoneCall) -> str:
        """Look until found or out of time: platforms list a call a moment after it ends."""
        waited = 0.0
        while True:
            try:
                return await self._finder.find(call)
            except TraceNotFound:
                if waited >= self._find_wait_s:
                    raise
            await self._wait(self._retry_s)
            waited += self._retry_s

    async def read(self, call_ref: str) -> Trace:
        if call_ref not in self._found:
            call = self._call_for(call_ref)
            if call is None:
                raise TraceNotFound(
                    f"trace not found: nothing is known about phone call {call_ref} "
                    "(no number or time to look it up by)"
                )
            self._found[call_ref] = await self._find(call)
        return await self._inner.read(self._found[call_ref])


def calls_in(directory: Path) -> Callable[[str], PhoneCall | None]:
    """The phone calls a directory of stored runs holds, by carrier id."""

    def call_for(call_ref: str) -> PhoneCall | None:
        for path in sorted(directory.glob("run-*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (payload.get("record") or {}).get("call_ref") == call_ref and payload.get("phone"):
                return PhoneCall.from_log(call_ref, payload["phone"])
        return None

    return call_for
