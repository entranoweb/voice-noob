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
from app.core.config import TRANSCRIPT_MAX_CHARS_CEILING, Settings
from app.core.limits import (
    JSON_MAX_CHARS,
    MARKER_RESERVE,
    MAX_JSON_BODY_BYTES,
    TRANSCRIPT_MAX_CHARS,
    BoundedDict,
    OverCapError,
    enforce_merge_cap_on_update,
    truncate_with_marker,
    truncation_marker,
)
from app.middleware.body_limit import BodySizeLimitMiddleware
from app.monitoring.audio_turns import AudioTurnRecorder
from app.monitoring.call_trace import Speaker
from app.services.gpt_realtime import GPTRealtimeSession
from app.services.qa.metrics.base import MetricContext, TurnData
from app.services.qa.metrics.context import turns_from_conversation
from app.services.qa.metrics.experience.transcription_accuracy import TranscriptionAccuracy

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
        text = "x" * 500
        assert truncate_with_marker(text, 500) == text

    def test_text_over_the_limit_is_cut_so_the_whole_fits_the_limit(self) -> None:
        result = truncate_with_marker("x" * 600, 500)
        kept = 500 - MARKER_RESERVE
        assert result == "x" * kept + "\n" + truncation_marker(600 - kept)
        assert len(result) <= 500

    def test_cutting_twice_changes_nothing(self) -> None:
        once = truncate_with_marker("x" * 100_000, 5_000)
        assert truncate_with_marker(once, 5_000) == once


class TestBridgeTranscript:
    def test_a_transcript_at_the_budget_is_kept_whole_and_unmarked(self) -> None:
        session = _session()
        # One line "[User]: ..." filling the whole budget, label included.
        session.add_user_transcript("u" * (TRANSCRIPT_MAX_CHARS - MARKER_RESERVE - len("[User]: ")))

        text = session.get_transcript()

        assert "truncated" not in text
        assert len(text) == TRANSCRIPT_MAX_CHARS - MARKER_RESERVE

    def test_a_transcript_over_the_cap_is_stored_cut_with_a_marker(self) -> None:
        session = _session()
        session.add_user_transcript("u" * TRANSCRIPT_MAX_CHARS)
        session.add_assistant_transcript("a" * 1234)
        session.add_user_transcript("later words")

        text = session.get_transcript()
        kept = sum(len(entry["content"]) for entry in session.get_transcript_entries())

        assert len(text) <= TRANSCRIPT_MAX_CHARS
        assert text.endswith(truncation_marker(TRANSCRIPT_MAX_CHARS + 1234 + 11 - kept))
        assert "a" * 1234 not in text

    def test_many_turns_over_the_cap_get_one_accurate_marker_and_no_second_cut(self) -> None:
        """Sixty caller turns of 2,000, sixty agent turns of 2,500 and thirty of 4,000.

        That is 390,000 characters of content, with the labels more. The stored text
        is cut once, at an entry, with one marker whose count is the content left out,
        and the write-time rule finds nothing more to cut.
        """
        session = _session()
        total = 0
        for _ in range(60):
            session.add_user_transcript("u" * 2000)
            session.add_assistant_transcript("a" * 2500)
            total += 4500
        for _ in range(30):
            session.add_assistant_transcript("b" * 4000)
            total += 4000

        text = session.get_transcript()
        kept = sum(len(entry["content"]) for entry in session.get_transcript_entries())

        assert total == 390_000
        assert len(text) <= TRANSCRIPT_MAX_CHARS
        assert text.count("[truncated:") == 1
        assert text.endswith(truncation_marker(total - kept))
        # What the call record stores (telephony_ws applies this) is this text, untouched.
        assert truncate_with_marker(text, TRANSCRIPT_MAX_CHARS) == text
        # Cut between entries: every line before the marker starts with its label.
        body = text.rsplit("\n\n", 1)[0]
        assert all(part.startswith(("[User]: ", "[Assistant]: ")) for part in body.split("\n\n"))

    def test_streamed_assistant_text_over_the_cap_is_counted_not_kept(self) -> None:
        session = _session()
        for _ in range(TRANSCRIPT_MAX_CHARS // 1000 + 5):
            session.accumulate_assistant_text("a" * 1000)
        session.flush_assistant_text()

        text = session.get_transcript()
        kept = sum(len(entry["content"]) for entry in session.get_transcript_entries())

        assert len(text) <= TRANSCRIPT_MAX_CHARS
        assert text.endswith(truncation_marker((TRANSCRIPT_MAX_CHARS // 1000 + 5) * 1000 - kept))
        # Memory is bounded too: nothing is held past the cap while a turn streams.
        assert len(session._current_assistant_text) == 0


class TestRecorderText:
    def _long_call(self) -> list[dict[str, Any]]:
        recorder = AudioTurnRecorder()
        recorder.caller_speech_started(at=0.0)
        recorder.caller_speech_stopped(at=1.0)
        recorder.caller_transcript("c" * TRANSCRIPT_MAX_CHARS, at=1.5)
        recorder.agent_transcript_delta("a" * 700, at=2.0)
        recorder.caller_speech_started(at=3.0)
        recorder.caller_speech_stopped(at=4.0)
        recorder.caller_transcript("later words", at=4.5)
        return recorder.conversation()

    def test_turn_text_over_the_cap_is_cut_with_the_marker_inside_the_cap(self) -> None:
        turns = self._long_call()
        stored = sum(
            len(turn[key] or "") for turn in turns for key in ("text_intended", "text_transcribed")
        )
        assert stored <= TRANSCRIPT_MAX_CHARS
        assert "[truncated:" in (turns[-1]["text_transcribed"] or "")

    def test_a_turn_that_lost_words_is_flagged_not_left_blank(self) -> None:
        turns = self._long_call()
        assert [turn.get("text_truncated", False) for turn in turns] == [True, True, True]

    def test_a_turn_inside_the_cap_is_not_flagged(self) -> None:
        recorder = AudioTurnRecorder()
        recorder.caller_speech_started(at=0.0)
        recorder.caller_speech_stopped(at=1.0)
        recorder.caller_transcript("hello", at=1.5)
        assert "text_truncated" not in recorder.conversation()[0]


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


class TestTranscriptCapSetting:
    def test_the_default_is_200_000(self) -> None:
        assert Settings(_env_file=None).TRANSCRIPT_MAX_CHARS == 200_000

    def test_a_value_inside_the_bounds_is_taken(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRANSCRIPT_MAX_CHARS", "250000")
        assert Settings(_env_file=None).TRANSCRIPT_MAX_CHARS == 250_000

    @pytest.mark.parametrize("value", ["5000", "0", "-1"])
    def test_a_value_below_the_floor_says_so(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("TRANSCRIPT_MAX_CHARS", value)
        with pytest.raises(ValidationError, match="TRANSCRIPT_MAX_CHARS must be at least 10000"):
            Settings(_env_file=None)

    def test_a_value_above_the_ceiling_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRANSCRIPT_MAX_CHARS", str(TRANSCRIPT_MAX_CHARS_CEILING + 1))
        with pytest.raises(ValidationError, match="TRANSCRIPT_MAX_CHARS must be at most 400000"):
            Settings(_env_file=None)

    def test_the_ceiling_still_fits_the_body_cap(self) -> None:
        # Four bytes a character is the worst case for UTF-8, ignoring escaping.
        assert TRANSCRIPT_MAX_CHARS_CEILING * 4 < MAX_JSON_BODY_BYTES


class TestMergeCapOnUpdate:
    def test_what_is_stored_sent_back_passes_even_over_the_cap(self) -> None:
        stored = {"k": "v" * (JSON_MAX_CHARS + 10)}
        enforce_merge_cap_on_update("settings", dict(stored), stored)

    def test_a_changed_oversized_entry_is_refused(self) -> None:
        stored = {"k": "v" * (JSON_MAX_CHARS + 10)}
        with pytest.raises(OverCapError):
            enforce_merge_cap_on_update("settings", {"k": "w" * (JSON_MAX_CHARS + 10)}, stored)

    def test_a_small_change_to_a_small_object_passes(self) -> None:
        enforce_merge_cap_on_update("settings", {"a": "2"}, {"a": "1", "b": "x"})

    def test_a_small_change_that_shrinks_an_oversized_object_passes(self) -> None:
        stored = {"big": "v" * (JSON_MAX_CHARS + 10), "a": "1"}
        enforce_merge_cap_on_update("settings", {"big": "short"}, stored)

    def test_the_merged_object_may_not_grow_past_the_cap(self) -> None:
        stored = {"a": "v" * (JSON_MAX_CHARS - 20)}
        with pytest.raises(OverCapError):
            enforce_merge_cap_on_update("settings", {"b": "x" * 100}, stored)


class TestTruncatedTurnsAreVoidForTheGrader:
    def test_a_turn_whose_words_were_cut_is_not_graded_as_a_mishearing(self) -> None:
        cut = TurnData(
            index=0,
            speaker=Speaker.CALLER,
            text_intended="book me for tuesday at ten",
            text_transcribed="book me",
            text_truncated=True,
        )
        clean = TurnData(
            index=1,
            speaker=Speaker.CALLER,
            text_intended="thank you",
            text_transcribed="thank you",
        )
        score = TranscriptionAccuracy().compute(
            MetricContext(run_id="r", turns=(cut, clean), has_audio=True)
        )
        assert score.value == 0.0
        assert score.detail["turns_compared"] == 1
        assert score.detail["turns_void_truncated"] == 1

    def test_a_run_whose_every_turn_was_cut_is_not_measurable(self) -> None:
        cut = TurnData(
            index=0,
            speaker=Speaker.CALLER,
            text_intended="book me for tuesday",
            text_transcribed="book",
            text_truncated=True,
        )
        score = TranscriptionAccuracy().compute(
            MetricContext(run_id="r", turns=(cut,), has_audio=True)
        )
        assert score.value is None
        assert score.detail["turns_void_truncated"] == 1

    def test_the_flag_survives_from_the_recorder_to_the_metric_turn(self) -> None:
        turns = turns_from_conversation(
            [
                {"speaker": "caller", "text_transcribed": "x", "text_truncated": True},
                {"speaker": "agent", "text_intended": "y"},
            ]
        )
        assert [turn.text_truncated for turn in turns] == [True, False]


class TestTheCapIsLoggedOncePerSession:
    def test_many_cut_entries_log_one_warning(self) -> None:
        from unittest.mock import MagicMock

        session = _session()
        session.logger = MagicMock()
        session.add_user_transcript("u" * TRANSCRIPT_MAX_CHARS)
        for _ in range(50):
            session.add_assistant_transcript("a" * 100)

        warnings = [
            c
            for c in session.logger.warning.call_args_list
            if c.args[0] == "transcript_cap_reached"
        ]
        assert len(warnings) == 1
