"""Legacy pricing tiers are refused when an agent is configured, not mid-call.

`realtime_model_for_tier` hands the premium model to anything that is not
"premium-mini", so a phone call to a "budget" or "balanced" agent ran, and billed,
as premium. The browser and the embed already refuse those tiers; the phone path
now refuses them at configuration time, with a message that names what is allowed.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.auth import user_id_to_uuid
from app.models.agent import Agent
from app.models.phone_number import PhoneNumber, PhoneNumberStatus

if TYPE_CHECKING:
    from httpx import AsyncClient

    from app.models.user import User

AGENTS = "/api/v1/agents"


def _payload(tier: str, **extra: Any) -> dict[str, Any]:
    return {"name": "Reception", "pricing_tier": tier, "system_prompt": "Be helpful.", **extra}


def _sessions(engine: Any) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _agent_row(engine: Any, user: User, tier: str, **extra: Any) -> Agent:
    """An agent written straight to the table, as one configured before the rule was."""
    async with _sessions(engine)() as session:
        agent = Agent(
            name="Old Agent",
            system_prompt="You answer the phone.",
            user_id=user.id,
            is_active=True,
            pricing_tier=tier,
            **extra,
        )
        session.add(agent)
        await session.commit()
        await session.refresh(agent)
        return agent


def _assert_names_allowed_tiers(detail: str, tier: str) -> None:
    assert tier in detail
    assert "premium, premium-mini" in detail


class TestCreate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("tier", ["budget", "balanced"])
    async def test_a_legacy_tier_is_refused_with_the_allowed_tiers(
        self, authenticated_test_client: tuple[AsyncClient, User], tier: str
    ) -> None:
        client, _user = authenticated_test_client

        response = await client.post(AGENTS, json=_payload(tier))

        assert response.status_code == 422, response.text
        _assert_names_allowed_tiers(response.json()["detail"], tier)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tier", ["premium", "premium-mini"])
    async def test_the_realtime_tiers_are_accepted(
        self, authenticated_test_client: tuple[AsyncClient, User], tier: str
    ) -> None:
        client, _user = authenticated_test_client

        response = await client.post(AGENTS, json=_payload(tier))

        assert response.status_code == 201, response.text
        assert response.json()["pricing_tier"] == tier


class TestUpdate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("tier", ["budget", "balanced"])
    async def test_moving_an_agent_onto_a_legacy_tier_is_refused(
        self, authenticated_test_client: tuple[AsyncClient, User], tier: str
    ) -> None:
        client, _user = authenticated_test_client
        created = await client.post(AGENTS, json=_payload("premium"))
        agent_id = created.json()["id"]

        response = await client.put(f"{AGENTS}/{agent_id}", json={"pricing_tier": tier})

        assert response.status_code == 422, response.text
        _assert_names_allowed_tiers(response.json()["detail"], tier)
        after = await client.get(f"{AGENTS}/{agent_id}")
        assert after.json()["pricing_tier"] == "premium"

    @pytest.mark.asyncio
    async def test_a_phone_number_cannot_be_put_on_a_legacy_agent(
        self, authenticated_test_client: tuple[AsyncClient, User], test_engine: Any
    ) -> None:
        client, user = authenticated_test_client
        agent = await _agent_row(test_engine, user, "balanced")

        response = await client.put(f"{AGENTS}/{agent.id}", json={"phone_number_id": "pn-opaque-1"})

        assert response.status_code == 422, response.text
        _assert_names_allowed_tiers(response.json()["detail"], "balanced")

    @pytest.mark.asyncio
    async def test_moving_to_premium_and_assigning_a_number_together_is_accepted(
        self, authenticated_test_client: tuple[AsyncClient, User], test_engine: Any
    ) -> None:
        client, user = authenticated_test_client
        agent = await _agent_row(test_engine, user, "balanced")

        response = await client.put(
            f"{AGENTS}/{agent.id}",
            json={"pricing_tier": "premium", "phone_number_id": "pn-opaque-1"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["pricing_tier"] == "premium"

    @pytest.mark.asyncio
    async def test_an_existing_legacy_agent_can_still_be_edited(
        self, authenticated_test_client: tuple[AsyncClient, User], test_engine: Any
    ) -> None:
        """The dashboard resends the whole form: a rename must not be a refusal."""
        client, user = authenticated_test_client
        agent = await _agent_row(test_engine, user, "balanced", phone_number_id="pn-opaque-1")

        response = await client.put(
            f"{AGENTS}/{agent.id}",
            json={
                "name": "Renamed",
                "pricing_tier": "balanced",
                "phone_number_id": "pn-opaque-1",
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["name"] == "Renamed"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tier", ["premium", "premium-mini"])
    async def test_the_realtime_tiers_are_accepted_with_a_number(
        self, authenticated_test_client: tuple[AsyncClient, User], tier: str
    ) -> None:
        client, _user = authenticated_test_client
        created = await client.post(AGENTS, json=_payload("premium"))

        response = await client.put(
            f"{AGENTS}/{created.json()['id']}",
            json={"pricing_tier": tier, "phone_number_id": "pn-opaque-1"},
        )

        assert response.status_code == 200, response.text


class TestPhoneNumberAssignment:
    @staticmethod
    async def _number(engine: Any, user: User) -> PhoneNumber:
        async with _sessions(engine)() as session:
            record = PhoneNumber(
                phone_number="+15551234567",
                provider="telnyx",
                provider_id=f"pn-{uuid.uuid4()}",
                user_id=user_id_to_uuid(user.id),
                status=PhoneNumberStatus.ACTIVE.value,
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record

    @pytest.mark.asyncio
    async def test_assigning_a_number_to_a_legacy_agent_is_refused(
        self, authenticated_test_client: tuple[AsyncClient, User], test_engine: Any
    ) -> None:
        client, user = authenticated_test_client
        agent = await _agent_row(test_engine, user, "budget")
        record = await self._number(test_engine, user)

        response = await client.put(
            f"/api/v1/phone-numbers/{record.id}", json={"assigned_agent_id": str(agent.id)}
        )

        assert response.status_code == 422, response.text
        _assert_names_allowed_tiers(response.json()["detail"], "budget")

    @pytest.mark.asyncio
    async def test_assigning_a_number_to_a_premium_agent_is_accepted(
        self, authenticated_test_client: tuple[AsyncClient, User], test_engine: Any
    ) -> None:
        client, user = authenticated_test_client
        agent = await _agent_row(test_engine, user, "premium-mini")
        record = await self._number(test_engine, user)

        response = await client.put(
            f"/api/v1/phone-numbers/{record.id}", json={"assigned_agent_id": str(agent.id)}
        )

        assert response.status_code == 200, response.text
