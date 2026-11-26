"""What is done about agents already on a legacy tier, and how the owner counts them.

Refusing the tier when an agent is configured does nothing for the agents that
were configured before. They keep working as they do today, and are surfaced in
two places: a preflight warning, and a read-only count the owner runs before the
rule ships.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from app.core.auth import user_id_to_uuid
from app.models.agent import Agent
from app.models.phone_number import PhoneNumber, PhoneNumberStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


preflight = _load("preflight_telnyx")
report = _load("report_legacy_tier_agents")


def _dialable(tier: str, name: str = "Front Desk") -> tuple[Any, str, str]:
    agent = SimpleNamespace(id=uuid.uuid4(), name=name, pricing_tier=tier)
    return agent, "+15551234567", "agent.phone_number_id"


class TestPreflight:
    def test_a_legacy_agent_on_a_number_is_a_warning_naming_it(self) -> None:
        legacy = _dialable("balanced")

        results = preflight.legacy_tier_results([_dialable("premium"), legacy])

        assert [r.level for r in results] == [preflight.Level.WARN]
        assert str(legacy[0].id) in results[0].detail
        assert "balanced" in results[0].detail
        assert "premium, premium-mini" in results[0].detail

    def test_it_is_never_a_failure(self) -> None:
        """The call still works as it did; the preflight must not block dialling."""
        results = preflight.legacy_tier_results([_dialable("budget"), _dialable("balanced")])

        assert all(r.level is not preflight.Level.FAIL for r in results)

    def test_realtime_tiers_pass(self) -> None:
        results = preflight.legacy_tier_results([_dialable("premium"), _dialable("premium-mini")])

        assert [r.level for r in results] == [preflight.Level.PASS]

    def test_an_agent_reached_by_two_routes_is_listed_once(self) -> None:
        agent, _number, _source = _dialable("budget")

        results = preflight.legacy_tier_results(
            [(agent, "+15551234567", "a"), (agent, "+15551234567", "b")]
        )

        assert results[0].detail.count(str(agent.id)) == 1


async def _agent(session: AsyncSession, owner: Any, tier: str, **kwargs: Any) -> Agent:
    agent = Agent(
        name=f"{tier} agent",
        system_prompt="You answer the phone.",
        user_id=owner.id,
        is_active=True,
        pricing_tier=tier,
        **kwargs,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


class TestReportScript:
    @pytest.mark.asyncio
    async def test_it_counts_legacy_agents_and_how_many_have_a_number(
        self, test_session: AsyncSession, create_test_user: Any
    ) -> None:
        owner = await create_test_user()
        await _agent(test_session, owner, "premium")
        await _agent(test_session, owner, "premium-mini", phone_number_id="pn-a")
        no_phone = await _agent(test_session, owner, "budget")
        legacy_route = await _agent(test_session, owner, "balanced", phone_number_id="pn-b")
        assigned = await _agent(test_session, owner, "balanced")
        released = await _agent(test_session, owner, "budget")
        for target, status in (
            (assigned, PhoneNumberStatus.ACTIVE.value),
            (released, PhoneNumberStatus.RELEASED.value),
        ):
            test_session.add(
                PhoneNumber(
                    phone_number=f"+1555123{target.id.int % 10000:04d}",
                    provider="telnyx",
                    provider_id=f"pn-{uuid.uuid4()}",
                    user_id=user_id_to_uuid(owner.id),
                    assigned_agent_id=target.id,
                    status=status,
                )
            )
        await test_session.commit()

        found = await report.collect(test_session)

        assert found.count == 4
        assert {str(a) for a in found.agent_ids} == {
            str(no_phone.id),
            str(legacy_route.id),
            str(assigned.id),
            str(released.id),
        }
        # An agent counts as having a number by either route, as routing does, and
        # a released number routes nowhere.
        assert found.with_phone == 2
        assert found.by_tier == {"budget": 2, "balanced": 2}

    @pytest.mark.asyncio
    async def test_it_writes_nothing(
        self, test_session: AsyncSession, create_test_user: Any
    ) -> None:
        owner = await create_test_user()
        legacy = await _agent(test_session, owner, "budget")

        await report.collect(test_session)

        assert not test_session.new
        assert not test_session.dirty
        assert not test_session.deleted
        await test_session.refresh(legacy)
        assert legacy.pricing_tier == "budget"

    @pytest.mark.asyncio
    async def test_none_is_reported_as_zero(self, test_session: AsyncSession) -> None:
        found = await report.collect(test_session)

        assert (found.count, found.with_phone, found.agent_ids) == (0, 0, [])
        assert "0 agent(s)" in report.render(found)

    @pytest.mark.asyncio
    async def test_the_printout_carries_ids_and_no_names(
        self, test_session: AsyncSession, create_test_user: Any
    ) -> None:
        owner = await create_test_user()
        legacy = await _agent(test_session, owner, "balanced", phone_number_id="pn-c")

        text = report.render(await report.collect(test_session))

        assert str(legacy.id) in text
        assert "1 agent(s)" in text
        assert "balanced agent" not in text
        assert "pn-c" not in text
