"""The user-wide call figures behind the dashboard: over every call, not a page of them."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.auth import user_id_to_uuid
from app.models.call_record import CallRecord

if TYPE_CHECKING:
    from httpx import AsyncClient

    from app.models.user import User

STATS = "/api/v1/calls/stats"


async def _store(
    test_engine: Any, user_uuid: uuid.UUID, *, status: str, seconds: int, direction: str = "inbound"
) -> None:
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    call_id = uuid.uuid4()
    async with maker() as session:
        session.add(
            CallRecord(
                id=call_id,
                user_id=user_uuid,
                provider="telnyx",
                provider_call_id=f"v3:{call_id.hex[:8]}",
                direction=direction,
                status=status,
                from_number="+15551230000",
                to_number="+15559997777",
                duration_seconds=seconds,
            )
        )
        await session.commit()


class TestCallStats:
    @pytest.mark.asyncio
    async def test_counts_and_average_cover_every_call_not_the_five_newest(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_engine: Any,
    ) -> None:
        client, user = authenticated_test_client
        mine = user_id_to_uuid(user.id)
        # Eight completed calls of 100 s and two failed ones of 0 s: more than a
        # five-call page, and the page-sized sample would give a different answer.
        for _ in range(8):
            await _store(test_engine, mine, status="completed", seconds=100)
        for _ in range(2):
            await _store(test_engine, mine, status="failed", seconds=0, direction="outbound")

        body = (await client.get(STATS)).json()

        assert body["total_calls"] == 10
        assert body["completed_calls"] == 8
        assert body["inbound_calls"] == 8
        assert body["outbound_calls"] == 2
        assert body["total_duration_seconds"] == 800
        assert body["average_duration_seconds"] == 80.0

    @pytest.mark.asyncio
    async def test_no_calls_gives_zeros_not_an_error(
        self, authenticated_test_client: tuple[AsyncClient, User]
    ) -> None:
        client, _ = authenticated_test_client

        response = await client.get(STATS)

        assert response.status_code == 200
        assert response.json()["total_calls"] == 0
        assert response.json()["average_duration_seconds"] == 0

    @pytest.mark.asyncio
    async def test_another_users_calls_are_not_counted(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_engine: Any,
    ) -> None:
        client, user = authenticated_test_client
        await _store(test_engine, user_id_to_uuid(user.id), status="completed", seconds=10)
        await _store(test_engine, uuid.uuid4(), status="completed", seconds=999)

        body = (await client.get(STATS)).json()

        assert body["total_calls"] == 1
        assert body["total_duration_seconds"] == 10
