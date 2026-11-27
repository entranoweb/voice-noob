"""GET /api/v1/calls?search= matches agent name, contact name and numbers, across every page."""

from __future__ import annotations

import secrets
import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.auth import user_id_to_uuid
from app.models.agent import Agent
from app.models.call_record import CallRecord
from app.models.contact import Contact
from app.models.user import User

if TYPE_CHECKING:
    from httpx import AsyncClient

URL = "/api/v1/calls"


async def _seed(test_engine: Any, user: User) -> dict[str, str]:
    """Three calls: one with a named agent, one with a named contact, one plain."""
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    ids: dict[str, str] = {}
    async with maker() as session:
        agent = Agent(
            user_id=user.id,
            name="Front Desk Helper",
            pricing_tier="premium",
            system_prompt="x",
        )
        contact = Contact(
            user_id=user.id, first_name="Zelda", last_name="Quill", phone_number="+15550001111"
        )
        session.add_all([agent, contact])
        await session.flush()
        rows = {
            "agent": {"agent_id": agent.id, "from_number": "+15551110001"},
            "contact": {"contact_id": contact.id, "from_number": "+15551110002"},
            "plain": {"from_number": "+15557654321"},
        }
        for key, extra in rows.items():
            call_id = uuid.uuid4()
            ids[key] = str(call_id)
            session.add(
                CallRecord(
                    id=call_id,
                    user_id=user_id_to_uuid(user.id),
                    provider="telnyx",
                    provider_call_id=f"v3:{call_id.hex[:8]}",
                    direction="inbound",
                    status="completed",
                    to_number="+15559990000",
                    **extra,
                )
            )
        await session.commit()
    return ids


async def _found(client: AsyncClient, **params: str) -> tuple[set[str], int]:
    body = (await client.get(URL, params=params)).json()
    return {c["id"] for c in body["calls"]}, body["total"]


class TestCallSearch:
    @pytest.mark.asyncio
    async def test_matches_agent_name_case_insensitively(
        self, authenticated_test_client: tuple[AsyncClient, User], test_engine: Any
    ) -> None:
        client, user = authenticated_test_client
        ids = await _seed(test_engine, user)

        found, total = await _found(client, search="front desk")

        assert found == {ids["agent"]}
        assert total == 1

    @pytest.mark.asyncio
    async def test_matches_contact_full_name_and_number(
        self, authenticated_test_client: tuple[AsyncClient, User], test_engine: Any
    ) -> None:
        client, user = authenticated_test_client
        ids = await _seed(test_engine, user)

        assert (await _found(client, search="zelda quill"))[0] == {ids["contact"]}
        assert (await _found(client, search="7654321"))[0] == {ids["plain"]}

    @pytest.mark.asyncio
    async def test_total_counts_matches_not_every_call(
        self, authenticated_test_client: tuple[AsyncClient, User], test_engine: Any
    ) -> None:
        client, user = authenticated_test_client
        await _seed(test_engine, user)

        _, total = await _found(client, search="+1555111")

        assert total == 2

    @pytest.mark.asyncio
    async def test_percent_is_literal_and_blank_search_is_ignored(
        self, authenticated_test_client: tuple[AsyncClient, User], test_engine: Any
    ) -> None:
        client, user = authenticated_test_client
        await _seed(test_engine, user)

        assert (await _found(client, search="%"))[1] == 0
        assert (await _found(client, search="   "))[1] == 3

    @pytest.mark.asyncio
    async def test_another_users_calls_never_match(
        self, authenticated_test_client: tuple[AsyncClient, User], test_engine: Any
    ) -> None:
        client, _ = authenticated_test_client
        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as session:
            call_id = uuid.uuid4()
            session.add(
                CallRecord(
                    id=call_id,
                    user_id=uuid.uuid4(),
                    provider="telnyx",
                    provider_call_id="v3:other",
                    direction="inbound",
                    status="completed",
                    from_number="+15553334444",
                    to_number="+15559990000",
                )
            )
            await session.commit()

        assert (await _found(client, search="3334444"))[1] == 0

    @pytest.mark.asyncio
    async def test_nul_byte_is_dropped_not_a_server_error(
        self, authenticated_test_client: tuple[AsyncClient, User], test_engine: Any
    ) -> None:
        client, user = authenticated_test_client
        ids = await _seed(test_engine, user)

        response = await client.get(URL, params={"search": "front\x00 desk"})

        assert response.status_code == 200
        assert {c["id"] for c in response.json()["calls"]} == {ids["agent"]}

    @pytest.mark.asyncio
    async def test_someone_elses_agent_name_does_not_match_my_call(
        self, authenticated_test_client: tuple[AsyncClient, User], test_engine: Any
    ) -> None:
        client, user = authenticated_test_client
        await _seed(test_engine, user)
        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as session:
            # My call, but pointing at an agent another user owns (a stale link).
            stranger = User(
                email="stranger@example.com",
                hashed_password=secrets.token_hex(8),
                full_name="Stranger",
                is_active=True,
            )
            session.add(stranger)
            await session.flush()
            other = Agent(
                user_id=stranger.id, name="Zephyr", pricing_tier="premium", system_prompt="x"
            )
            session.add(other)
            await session.flush()
            call_id = uuid.uuid4()
            session.add(
                CallRecord(
                    id=call_id,
                    user_id=user_id_to_uuid(user.id),
                    agent_id=other.id,
                    provider="telnyx",
                    provider_call_id="v3:stale",
                    direction="inbound",
                    status="completed",
                    from_number="+15550009999",
                    to_number="+15559990000",
                )
            )
            await session.commit()

        assert (await _found(client, search="zephyr"))[1] == 0
