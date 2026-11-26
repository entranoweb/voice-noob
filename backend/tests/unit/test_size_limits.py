"""Size limits: what the bridge stores, what the recorder keeps, and the body cap.

A long call is cut and says so; it never fails. A request over a limit is refused
with a message that names the limit.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.core import limits
from app.core.limits import (
    JSON_MAX_CHARS,
    TRANSCRIPT_MAX_CHARS,
    BoundedDict,
    truncate_with_marker,
    truncation_marker,
)
from app.middleware.body_limit import BodySizeLimitMiddleware
from app.monitoring.audio_turns import AudioTurnRecorder
from app.services.gpt_realtime import GPTRealtimeSession

if TYPE_CHECKING:
    from starlette.requests import Request


def _session() -> GPTRealtimeSession:
    return GPTRealtimeSession(
        db=None,  # type: ignore[arg-type]
        user_id=1,
        agent_config={"pricing_tier": "premium"},
    )


class TestTruncate:
    def test_text_at_the_limit_is_unchanged(self) -> None:
        text = "x" * 50
        assert truncate_with_marker(text, 50) == text

    def test_text_over_the_limit_says_how_much_was_dropped(self) -> None:
        result = truncate_with_marker("x" * 60, 50)
        assert result == "x" * 50 + "\n" + truncation_marker(10)
        assert result.endswith("[truncated: 10 characters omitted]")


class TestBridgeTranscript:
    def test_a_transcript_at_the_cap_is_kept_whole_and_unmarked(self) -> None:
        session = _session()
        session.add_user_transcript("u" * (TRANSCRIPT_MAX_CHARS // 2))
        session.add_assistant_transcript("a" * (TRANSCRIPT_MAX_CHARS // 2))

        text = session.get_transcript()

        assert "truncated" not in text
        assert text.count("u") == TRANSCRIPT_MAX_CHARS // 2
        assert text.count("a") >= TRANSCRIPT_MAX_CHARS // 2

    def test_a_transcript_over_the_cap_is_stored_cut_with_a_marker(self) -> None:
        session = _session()
        session.add_user_transcript("u" * TRANSCRIPT_MAX_CHARS)
        session.add_assistant_transcript("a" * 1234)
        session.add_user_transcript("later words")

        text = session.get_transcript()

        assert text.endswith(truncation_marker(1234 + len("later words")))
        assert "u" * TRANSCRIPT_MAX_CHARS in text
        assert "a" * 1234 not in text

    def test_streamed_assistant_text_over_the_cap_is_counted_not_kept(self) -> None:
        session = _session()
        for _ in range(TRANSCRIPT_MAX_CHARS // 1000 + 5):
            session.accumulate_assistant_text("a" * 1000)
        session.flush_assistant_text()

        text = session.get_transcript()

        assert text.endswith(truncation_marker(5 * 1000))
        # Memory is bounded too: nothing is held past the cap while a turn streams.
        assert len(session._current_assistant_text) == 0


class TestRecorderText:
    def test_turn_text_over_the_cap_is_cut_and_the_last_turn_says_so(self) -> None:
        recorder = AudioTurnRecorder()
        recorder.caller_speech_started(at=0.0)
        recorder.caller_speech_stopped(at=1.0)
        recorder.caller_transcript("c" * TRANSCRIPT_MAX_CHARS, at=1.5)
        recorder.agent_transcript_delta("a" * 700, at=2.0)

        turns = recorder.conversation()

        assert turns[0]["text_transcribed"] == "c" * TRANSCRIPT_MAX_CHARS
        assert turns[1]["text_intended"] == "\n" + truncation_marker(700)
        assert recorder.turn_count() == 2


class TestBoundedDict:
    class Model(BaseModel):
        data: BoundedDict | None = None

    def test_json_at_the_limit_is_accepted(self) -> None:
        # {"k":"..."} is 8 characters around the value.
        value = {"k": "v" * (JSON_MAX_CHARS - 8)}
        assert len(json.dumps(value, separators=(",", ":"))) == JSON_MAX_CHARS
        assert self.Model(data=value).data == value

    def test_json_over_the_limit_is_refused_with_the_limit_named(self) -> None:
        with pytest.raises(ValidationError, match=f"at most {JSON_MAX_CHARS} characters"):
            self.Model(data={"k": "v" * JSON_MAX_CHARS})


def _app(max_bytes: int) -> Starlette:
    async def echo(request: Request) -> JSONResponse:
        return JSONResponse({"size": len(await request.body())})

    app = Starlette(
        routes=[Route("/echo", echo, methods=["POST"]), Route("/x/echo", echo, methods=["POST"])]
    )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_bytes, exempt_prefixes=("/x/",))
    return app


class TestBodySizeLimit:
    async def _post(self, app: Starlette, **kwargs: Any) -> Any:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            return await client.post(kwargs.pop("path", "/echo"), **kwargs)

    @pytest.mark.asyncio
    async def test_a_body_at_the_cap_is_accepted(self) -> None:
        response = await self._post(_app(100), content=b"x" * 100)
        assert response.status_code == 200
        assert response.json() == {"size": 100}

    @pytest.mark.asyncio
    async def test_a_declared_length_over_the_cap_is_refused_413(self) -> None:
        response = await self._post(_app(100), content=b"x" * 101)
        assert response.status_code == 413
        assert "larger than 100 bytes" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_chunked_body_with_no_length_is_counted_as_it_streams(self) -> None:
        async def chunks() -> Any:
            for _ in range(11):
                yield b"x" * 10

        response = await self._post(_app(100), content=chunks())
        assert response.status_code == 413

    @pytest.mark.asyncio
    async def test_a_chunked_body_at_the_cap_is_accepted(self) -> None:
        async def chunks() -> Any:
            for _ in range(10):
                yield b"x" * 10

        response = await self._post(_app(100), content=chunks())
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_an_exempt_prefix_is_not_capped(self) -> None:
        response = await self._post(_app(100), path="/x/echo", content=b"x" * 500)
        assert response.status_code == 200


def test_the_limits_are_generous_for_a_thirty_minute_call() -> None:
    # 1,500 characters a minute per speaker (fast speech), both talking all the time.
    worst_case_thirty_minutes = 2 * 1500 * 30
    assert worst_case_thirty_minutes == 90_000
    assert 2 * worst_case_thirty_minutes <= limits.TRANSCRIPT_MAX_CHARS
