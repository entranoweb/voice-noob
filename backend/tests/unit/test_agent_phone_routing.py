"""Which agent a call to a number reaches.

The product writes the number-to-agent relationship in one place and the inbound
webhook used to read another. Assigning a number to an agent in the dashboard —
the only route the API offers, since neither AgentCreate nor AgentUpdate exposes
`phone_number_id` — left the caller hearing "no agent is configured for this
number" while the dashboard showed the agent assigned.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest

from app.api.telephony import get_agent_by_phone_number
from app.core.auth import user_id_to_uuid
from app.models.agent import Agent
from app.models.phone_number import PhoneNumber, PhoneNumberStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

NUMBER = "+15551234567"


async def _agent(session: AsyncSession, owner: Any, **kwargs: Any) -> Agent:
    agent = Agent(
        name="Routing Agent",
        system_prompt="You answer the phone.",
        user_id=owner.id,
        is_active=True,
        pricing_tier="premium",
        **kwargs,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def _assign(session: AsyncSession, agent: Agent, owner: Any, *, status: str) -> None:
    session.add(
        PhoneNumber(
            phone_number=NUMBER,
            provider="telnyx",
            provider_id=f"pn-{uuid.uuid4()}",
            user_id=user_id_to_uuid(owner.id),
            assigned_agent_id=agent.id,
            status=status,
        )
    )
    await session.commit()


class TestRoutingByAssignment:
    @pytest.mark.asyncio
    async def test_an_assigned_number_reaches_its_agent(
        self, test_session: AsyncSession, create_test_user: Any
    ) -> None:
        """The route the dashboard actually writes. This used to return None."""
        owner = await create_test_user()
        agent = await _agent(test_session, owner)
        await _assign(test_session, agent, owner, status=PhoneNumberStatus.ACTIVE.value)

        found = await get_agent_by_phone_number(NUMBER, test_session)

        assert found is not None
        assert found.id == agent.id

    @pytest.mark.asyncio
    async def test_a_released_number_reaches_nobody(
        self, test_session: AsyncSession, create_test_user: Any
    ) -> None:
        """A number taken out of service should stop ringing."""
        owner = await create_test_user()
        agent = await _agent(test_session, owner)
        await _assign(test_session, agent, owner, status=PhoneNumberStatus.RELEASED.value)

        assert await get_agent_by_phone_number(NUMBER, test_session) is None

    @pytest.mark.asyncio
    async def test_the_legacy_field_still_works(
        self, test_session: AsyncSession, create_test_user: Any
    ) -> None:
        """Rows written by direct SQL, before there was an assignment table."""
        owner = await create_test_user()
        agent = await _agent(test_session, owner, phone_number_id=NUMBER)

        found = await get_agent_by_phone_number(NUMBER, test_session)

        assert found is not None
        assert found.id == agent.id

    @pytest.mark.asyncio
    @pytest.mark.parametrize("dialled", [NUMBER, "15551234567"])
    async def test_the_plus_is_optional(
        self, test_session: AsyncSession, create_test_user: Any, dialled: str
    ) -> None:
        """Telnyx is not guaranteed to send the number in E.164 with the +."""
        owner = await create_test_user()
        agent = await _agent(test_session, owner)
        await _assign(test_session, agent, owner, status=PhoneNumberStatus.ACTIVE.value)

        found = await get_agent_by_phone_number(dialled, test_session)

        assert found is not None
        assert found.id == agent.id

    @pytest.mark.asyncio
    async def test_an_unassigned_number_reaches_nobody(
        self, test_session: AsyncSession, create_test_user: Any
    ) -> None:
        owner = await create_test_user()
        await _agent(test_session, owner)

        assert await get_agent_by_phone_number(NUMBER, test_session) is None
