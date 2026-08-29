"""Tests for reading a real call's metrics over the API.

The contract that matters here is the one a client can get wrong: ``null`` means
the metric could not be measured, ``0.0`` means it was measured and was bad. The
endpoint must never collapse the two.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.auth import user_id_to_uuid
from app.models.call_record import CallRecord, CallStatus
from app.monitoring.audio_turns import AudioTurnRecorder

if TYPE_CHECKING:
    from httpx import AsyncClient

    from app.models.user import User


def _recorded_turns() -> list[dict[str, Any]]:
    recorder = AudioTurnRecorder()
    recorder.agent_audio_delta(byte_count=8000, at=0.0)
    recorder.agent_turn_ended(at=1.0)
    recorder.caller_speech_started(at=1.2)
    recorder.caller_speech_stopped(at=2.0)
    recorder.caller_transcript("book me for Tuesday", at=2.1)
    recorder.agent_audio_delta(byte_count=8000, at=2.4)
    recorder.agent_turn_ended(at=3.0)
    return recorder.conversation()


async def _store_call(
    test_engine: Any,
    user: User,
    *,
    turns: list[dict[str, Any]] | None,
) -> uuid.UUID:
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    call_id = uuid.uuid4()
    async with maker() as session:
        session.add(
            CallRecord(
                id=call_id,
                user_id=user_id_to_uuid(user.id),
                provider="telnyx",
                provider_call_id=f"v3:{call_id.hex[:8]}",
                direction="inbound",
                status=CallStatus.COMPLETED.value,
                from_number="+15551230000",
                to_number="+15559997777",
                duration_seconds=30,
                turns=turns,
            ),
        )
        await session.commit()
    return call_id


class TestCallMetricsEndpoint:
    @pytest.mark.asyncio
    async def test_a_call_with_audio_reports_measured_latency(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_engine: Any,
    ) -> None:
        client, user = authenticated_test_client
        call_id = await _store_call(test_engine, user, turns=_recorded_turns())

        response = await client.get(f"/api/v1/calls/{call_id}/metrics")

        assert response.status_code == 200
        body = response.json()
        assert body["has_audio"] is True

        scores = {score["metric"]: score for score in body["scores"]}
        assert scores["time_to_first_audio"]["value"] is not None
        assert scores["time_to_first_audio"]["unit"] == "ms"

    @pytest.mark.asyncio
    async def test_an_unmeasurable_metric_is_null_not_zero(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_engine: Any,
    ) -> None:
        client, user = authenticated_test_client
        call_id = await _store_call(test_engine, user, turns=_recorded_turns())

        response = await client.get(f"/api/v1/calls/{call_id}/metrics")
        scores = {score["metric"]: score for score in response.json()["scores"]}

        # A human caller has no script, so there is no reference for word error
        # rate. Null, never zero.
        assert scores["transcription_accuracy"]["value"] is None
        assert scores["transcription_accuracy"]["passed"] is None

    @pytest.mark.asyncio
    async def test_a_call_with_no_audio_measures_none_of_the_three(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_engine: Any,
    ) -> None:
        client, user = authenticated_test_client
        call_id = await _store_call(test_engine, user, turns=None)

        response = await client.get(f"/api/v1/calls/{call_id}/metrics")
        body = response.json()
        assert body["has_audio"] is False

        scores = {score["metric"]: score for score in body["scores"]}
        for name in ("transcription_accuracy", "time_to_first_audio", "interruption_handling"):
            assert scores[name]["value"] is None

    @pytest.mark.asyncio
    async def test_another_users_call_is_not_readable(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_engine: Any,
    ) -> None:
        client, _ = authenticated_test_client
        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        call_id = uuid.uuid4()
        async with maker() as session:
            session.add(
                CallRecord(
                    id=call_id,
                    user_id=uuid.uuid4(),
                    provider="telnyx",
                    provider_call_id="v3:someone-else",
                    direction="inbound",
                    status=CallStatus.COMPLETED.value,
                    from_number="+1",
                    to_number="+2",
                    duration_seconds=1,
                ),
            )
            await session.commit()

        response = await client.get(f"/api/v1/calls/{call_id}/metrics")
        assert response.status_code == 404
