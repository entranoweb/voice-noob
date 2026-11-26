"""A number can only be registered by the workspace whose carrier account holds it.

The carrier is a fake: each credential set maps to the numbers that account
holds. Nothing here reaches a network, and every number is a +1555010xxxx
placeholder.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.auth import create_access_token, get_password_hash
from app.api.telephony import get_agent_by_phone_number
from app.core.auth import user_id_to_uuid
from app.db.redis import get_redis
from app.db.session import get_db
from app.main import app
from app.models.agent import Agent
from app.models.phone_number import PhoneNumber, PhoneNumberStatus
from app.models.user import User
from app.models.user_settings import UserSettings
from app.models.workspace import AgentWorkspace, Workspace
from app.services.telephony.number_ownership import (
    CarrierLookupError,
    TelnyxNumberLookup,
    TwilioNumberLookup,
    find_unverified_numbers,
    get_carrier_lookup_factory,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

NUMBER_A = "+15550100001"
NUMBER_B = "+15550100002"
NUMBER_C = "+15550100003"


@dataclass
class FakeCarrier:
    """Which numbers each credential set holds, and what happens when asked."""

    accounts: dict[str, set[str]] = field(default_factory=dict)
    error: Exception | None = None
    asked: list[tuple[str, str]] = field(default_factory=list)

    def factory(self, provider: str, settings: UserSettings | None) -> Any:
        if settings is None:
            return None
        key = settings.telnyx_api_key if provider == "telnyx" else settings.twilio_account_sid
        if not key:
            return None
        carrier = self

        class Lookup:
            async def holds_number(self, number: str) -> bool:
                carrier.asked.append((key, number))
                if carrier.error is not None:
                    raise carrier.error
                return number in carrier.accounts.get(key, set())

        return Lookup()


@dataclass
class Tenant:
    user: User
    headers: dict[str, str]
    workspace: Workspace
    second_workspace: Workspace
    agent: Agent
    second_agent: Agent


@dataclass
class World:
    client: AsyncClient
    sessions: async_sessionmaker[AsyncSession]
    carrier: FakeCarrier
    alice: Tenant
    bob: Tenant


async def _tenant(session: AsyncSession, name: str) -> Tenant:
    user = User(
        email=f"{name}@example.com",
        hashed_password=get_password_hash(f"{name}-correct-horse"),
        full_name=name.title(),
        is_active=True,
        is_superuser=False,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    workspaces = [Workspace(user_id=user.id, name=f"{name} {n}") for n in ("one", "two")]
    agents = [
        Agent(
            user_id=user.id,
            name=f"{name} agent {n}",
            system_prompt="You answer the phone politely.",
            pricing_tier="premium",
            is_active=True,
            enabled_tools=[],
            enabled_tool_ids={},
        )
        for n in ("one", "two")
    ]
    session.add_all([*workspaces, *agents])
    await session.commit()
    for obj in (*workspaces, *agents):
        await session.refresh(obj)
    session.add(
        AgentWorkspace(agent_id=agents[0].id, workspace_id=workspaces[0].id, is_default=True)
    )
    session.add(
        AgentWorkspace(agent_id=agents[1].id, workspace_id=workspaces[0].id, is_default=False)
    )
    # Each workspace saves its own carrier credentials.
    owner = user_id_to_uuid(user.id)
    for index, workspace in enumerate(workspaces):
        session.add(
            UserSettings(
                user_id=owner,
                workspace_id=workspace.id,
                telnyx_api_key=f"telnyx-{name}-{index}",
                twilio_account_sid=f"AC{name}{index}",
                twilio_auth_token=f"token-{name}-{index}",
            )
        )
    await session.commit()
    return Tenant(
        user=user,
        headers={"Authorization": f"Bearer {create_access_token(user.id)}"},
        workspace=workspaces[0],
        second_workspace=workspaces[1],
        agent=agents[0],
        second_agent=agents[1],
    )


@pytest_asyncio.fixture
async def world(test_engine: Any, test_redis: Any) -> AsyncGenerator[World, None]:
    sessions = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    carrier = FakeCarrier()

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with sessions() as session:
            yield session

    async def override_get_redis() -> Any:
        return test_redis

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_redis] = override_get_redis
    app.dependency_overrides[get_carrier_lookup_factory] = lambda: carrier.factory

    async with sessions() as session:
        alice = await _tenant(session, "alice")
        bob = await _tenant(session, "bob")

    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield World(client=client, sessions=sessions, carrier=carrier, alice=alice, bob=bob)
    finally:
        app.dependency_overrides.clear()


def _holds(world: World, tenant: Tenant, *numbers: str, provider: str = "telnyx") -> None:
    key = f"telnyx-{tenant.user.full_name.lower()}-0"
    if provider == "twilio":
        key = f"AC{tenant.user.full_name.lower()}0"
    world.carrier.accounts.setdefault(key, set()).update(numbers)


async def _register(
    world: World, tenant: Tenant, number: str, provider: str = "telnyx", **fields: Any
) -> httpx.Response:
    return await world.client.post(
        "/api/v1/phone-numbers",
        headers=tenant.headers,
        json={
            "phone_number": number,
            "provider": provider,
            "provider_id": "carrier-id",
            "workspace_id": str(tenant.workspace.id),
            **fields,
        },
    )


async def _count_rows(world: World) -> int:
    async with world.sessions() as session:
        return (await session.execute(select(func.count()).select_from(PhoneNumber))).scalar_one()


class TestRegistration:
    @pytest.mark.asyncio
    async def test_another_workspaces_number_is_refused(self, world: World) -> None:
        _holds(world, world.bob, NUMBER_A)

        response = await _register(world, world.alice, NUMBER_A)

        assert response.status_code == 422
        assert "not found on this workspace's carrier account" in response.json()["detail"]
        assert await _count_rows(world) == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", ["telnyx", "twilio"])
    @pytest.mark.parametrize("spelling", [NUMBER_A, NUMBER_A.lstrip("+")])
    async def test_own_number_is_accepted(self, world: World, provider: str, spelling: str) -> None:
        _holds(world, world.alice, NUMBER_A, provider=provider)

        response = await _register(world, world.alice, spelling, provider=provider)

        assert response.status_code == 201, response.text
        assert await _count_rows(world) == 1

    @pytest.mark.asyncio
    async def test_the_same_users_other_workspace_cannot_vouch_for_it(self, world: World) -> None:
        """Workspaces hold separate carrier accounts, so one cannot prove another's."""
        _holds(world, world.alice, NUMBER_A)  # held by workspace one's account

        response = await _register(
            world, world.alice, NUMBER_A, workspace_id=str(world.alice.second_workspace.id)
        )

        assert response.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            httpx.ConnectTimeout("slow"),
            httpx.ConnectError("down"),
            CarrierLookupError("carrier answered 500"),
            RuntimeError("anything at all"),
        ],
    )
    async def test_a_carrier_error_refuses_with_its_own_message(
        self, world: World, error: Exception
    ) -> None:
        _holds(world, world.alice, NUMBER_A)  # it IS theirs; the carrier just cannot say
        world.carrier.error = error

        response = await _register(world, world.alice, NUMBER_A)

        assert response.status_code == 503
        assert (
            response.json()["detail"]
            == "Could not verify the phone number with the carrier, try again."
        )
        assert await _count_rows(world) == 0

    @pytest.mark.asyncio
    async def test_no_carrier_credentials_refuses_with_a_clear_message(self, world: World) -> None:
        async with world.sessions() as session:
            rows = (await session.execute(select(UserSettings))).scalars().all()
            for row in rows:
                await session.delete(row)
            await session.commit()

        response = await _register(world, world.alice, NUMBER_A)

        assert response.status_code == 422
        assert "Settings" in response.json()["detail"]
        assert "telnyx" in response.json()["detail"]
        assert world.carrier.asked == []

    @pytest.mark.asyncio
    async def test_credentials_for_the_other_carrier_do_not_count(self, world: World) -> None:
        async with world.sessions() as session:
            rows = (await session.execute(select(UserSettings))).scalars().all()
            for row in rows:
                row.twilio_account_sid = None
                row.twilio_auth_token = None
            await session.commit()
        _holds(world, world.alice, NUMBER_A, provider="twilio")

        response = await _register(world, world.alice, NUMBER_A, provider="twilio")

        assert response.status_code == 422
        assert "twilio" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_non_holder_learns_nothing_about_other_registrations(
        self, world: World
    ) -> None:
        """Bob really holds and registers it; Alice gets the same 422 as for any number."""
        _holds(world, world.bob, NUMBER_A)
        assert (await _register(world, world.bob, NUMBER_A)).status_code == 201

        taken = await _register(world, world.alice, NUMBER_A)
        unknown = await _register(world, world.alice, NUMBER_B)

        assert taken.status_code == unknown.status_code == 422
        assert taken.json() == unknown.json()

    @pytest.mark.asyncio
    async def test_a_registration_without_a_workspace_is_refused(self, world: World) -> None:
        """No user-level fallback: carrier credentials are per workspace."""
        async with world.sessions() as session:
            session.add(
                UserSettings(
                    user_id=user_id_to_uuid(world.alice.user.id),
                    workspace_id=None,
                    telnyx_api_key="telnyx-user-level",
                )
            )
            await session.commit()
        world.carrier.accounts["telnyx-user-level"] = {NUMBER_A}

        response = await world.client.post(
            "/api/v1/phone-numbers",
            headers=world.alice.headers,
            json={"phone_number": NUMBER_A, "provider": "telnyx", "provider_id": "id"},
        )

        assert response.status_code == 422
        assert response.json()["detail"] == "Choose a workspace for this phone number."
        assert world.carrier.asked == []
        assert await _count_rows(world) == 0

    @pytest.mark.asyncio
    async def test_a_malformed_number_is_refused_before_the_carrier_is_asked(
        self, world: World
    ) -> None:
        response = await _register(world, world.alice, "not-a-number")

        assert response.status_code == 422
        assert world.carrier.asked == []


class TestUpdate:
    async def _registered(self, world: World) -> str:
        _holds(world, world.alice, NUMBER_A)
        response = await _register(world, world.alice, NUMBER_A)
        assert response.status_code == 201
        world.carrier.asked.clear()
        return str(response.json()["id"])

    @pytest.mark.asyncio
    async def test_reassigning_between_agents_does_not_ask_the_carrier(self, world: World) -> None:
        number_id = await self._registered(world)
        for agent in (world.alice.agent, world.alice.second_agent):
            response = await world.client.put(
                f"/api/v1/phone-numbers/{number_id}",
                headers=world.alice.headers,
                json={"assigned_agent_id": str(agent.id)},
            )
            assert response.status_code == 200
            assert response.json()["assigned_agent_id"] == str(agent.id)
        # Even a carrier outage cannot block moving a line between agents.
        world.carrier.error = CarrierLookupError("down")
        response = await world.client.put(
            f"/api/v1/phone-numbers/{number_id}",
            headers=world.alice.headers,
            json={"assigned_agent_id": str(world.alice.agent.id), "notes": "n"},
        )
        assert response.status_code == 200
        assert world.carrier.asked == []

    @pytest.mark.asyncio
    async def test_moving_to_a_workspace_whose_account_lacks_it_is_refused(
        self, world: World
    ) -> None:
        number_id = await self._registered(world)

        response = await world.client.put(
            f"/api/v1/phone-numbers/{number_id}",
            headers=world.alice.headers,
            json={"workspace_id": str(world.alice.second_workspace.id)},
        )

        assert response.status_code == 422
        async with world.sessions() as session:
            stored = await session.get(PhoneNumber, uuid.UUID(number_id))
            assert stored is not None
            assert stored.workspace_id == world.alice.workspace.id

    @pytest.mark.asyncio
    async def test_moving_to_a_workspace_whose_account_holds_it_works(self, world: World) -> None:
        number_id = await self._registered(world)
        world.carrier.accounts.setdefault("telnyx-alice-1", set()).add(NUMBER_A)

        response = await world.client.put(
            f"/api/v1/phone-numbers/{number_id}",
            headers=world.alice.headers,
            json={"workspace_id": str(world.alice.second_workspace.id)},
        )

        assert response.status_code == 200
        assert response.json()["workspace_id"] == str(world.alice.second_workspace.id)

    @pytest.mark.asyncio
    async def test_taking_a_number_out_of_its_workspace_is_refused(self, world: World) -> None:
        number_id = await self._registered(world)

        response = await world.client.put(
            f"/api/v1/phone-numbers/{number_id}",
            headers=world.alice.headers,
            json={"workspace_id": ""},
        )

        assert response.status_code == 422
        assert response.json()["detail"] == "Choose a workspace for this phone number."

    @pytest.mark.asyncio
    async def test_bringing_back_a_released_number_is_verified_again(self, world: World) -> None:
        number_id = await self._registered(world)
        released = await world.client.put(
            f"/api/v1/phone-numbers/{number_id}",
            headers=world.alice.headers,
            json={"status": PhoneNumberStatus.RELEASED.value},
        )
        assert released.status_code == 200
        world.carrier.accounts.clear()  # the number has left the account meanwhile

        response = await world.client.put(
            f"/api/v1/phone-numbers/{number_id}",
            headers=world.alice.headers,
            json={"status": PhoneNumberStatus.ACTIVE.value},
        )

        assert response.status_code == 422


def _mock(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestCarrierClients:
    """The real clients, against a mocked transport shaped like the carriers' docs."""

    @pytest.mark.asyncio
    async def test_telnyx_asks_verify_ownership_and_reads_found(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                json={
                    "data": {
                        "found": [{"number_val_e164": NUMBER_A, "id": "1"}],
                        "not_found": [],
                        "record_type": "number_ownership_verification",
                    }
                },
            )

        lookup = TelnyxNumberLookup("key-1", client=_mock(handler))

        assert await lookup.holds_number(NUMBER_A) is True
        request = seen[0]
        assert request.method == "POST"
        assert (
            str(request.url) == "https://api.telnyx.com/v2/phone_numbers/actions/verify_ownership"
        )
        assert request.headers["authorization"] == "Bearer key-1"
        assert json.loads(request.content) == {"phone_numbers": [NUMBER_A]}

    @pytest.mark.asyncio
    async def test_telnyx_not_found_and_a_different_found_number_are_not_held(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "found": [{"number_val_e164": NUMBER_B, "id": "2"}],
                        "not_found": [NUMBER_A],
                    }
                },
            )

        assert await TelnyxNumberLookup("k", client=_mock(handler)).holds_number(NUMBER_A) is False

    @pytest.mark.asyncio
    async def test_twilio_lists_incoming_numbers_and_compares_exactly(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            # Twilio's PhoneNumber filter is a partial match: a longer number
            # containing ours comes back too and must not count.
            return httpx.Response(
                200,
                json={"incoming_phone_numbers": [{"phone_number": NUMBER_A + "9", "sid": "PN1"}]},
            )

        lookup = TwilioNumberLookup("ACsid", "tok", client=_mock(handler))

        assert await lookup.holds_number(NUMBER_A) is False
        request = seen[0]
        assert request.url.path == "/2010-04-01/Accounts/ACsid/IncomingPhoneNumbers.json"
        assert request.url.params["PhoneNumber"] == NUMBER_A
        assert request.headers["authorization"].startswith("Basic ")

    @pytest.mark.asyncio
    async def test_twilio_exact_match_is_held(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"incoming_phone_numbers": [{"phone_number": NUMBER_A, "sid": "PN1"}]}
            )

        assert await TwilioNumberLookup("AC", "t", client=_mock(handler)).holds_number(NUMBER_A)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [500, 502, 429])
    async def test_a_failing_carrier_raises_rather_than_answering_no(self, status: int) -> None:
        lookup = TelnyxNumberLookup("k", client=_mock(lambda _r: httpx.Response(status)))
        with pytest.raises(CarrierLookupError):
            await lookup.holds_number(NUMBER_A)

    @pytest.mark.asyncio
    async def test_an_unreadable_answer_raises_rather_than_answering_no(self) -> None:
        lookup = TwilioNumberLookup(
            "AC", "t", client=_mock(lambda _r: httpx.Response(200, text="x"))
        )
        with pytest.raises(CarrierLookupError):
            await lookup.holds_number(NUMBER_A)

    @pytest.mark.asyncio
    async def test_a_timeout_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        with pytest.raises(CarrierLookupError):
            await TelnyxNumberLookup("k", client=_mock(handler)).holds_number(NUMBER_A)

    @pytest.mark.asyncio
    async def test_rejected_credentials_are_reported_as_such(self, world: World) -> None:
        """A 401 from the carrier reads as a credentials problem, not as an outage."""
        from app.services.telephony.number_ownership import verify_number_on_workspace_account

        def factory(provider: str, settings: Any) -> Any:
            return TelnyxNumberLookup("k", client=_mock(lambda _r: httpx.Response(401)))

        async with world.sessions() as session:
            with pytest.raises(Exception, match="rejected") as caught:
                await verify_number_on_workspace_account(
                    session,
                    user_id=user_id_to_uuid(world.alice.user.id),
                    workspace_id=world.alice.workspace.id,
                    provider="telnyx",
                    number=NUMBER_A,
                    factory=factory,
                )
            assert caught.value.status_code == 422  # type: ignore[attr-defined]


class TestReport:
    @pytest.mark.asyncio
    async def test_it_lists_what_registration_would_refuse_and_changes_nothing(
        self, world: World
    ) -> None:
        _holds(world, world.alice, NUMBER_A)  # legitimate
        # Legacy rows written before the check: Alice holds none of these.
        async with world.sessions() as session:
            owner = user_id_to_uuid(world.alice.user.id)
            session.add_all(
                [
                    PhoneNumber(
                        user_id=owner,
                        workspace_id=world.alice.workspace.id,
                        phone_number=NUMBER_A,
                        provider="telnyx",
                        provider_id="ok",
                    ),
                    PhoneNumber(
                        user_id=owner,
                        workspace_id=world.alice.workspace.id,
                        phone_number=NUMBER_B,
                        provider="telnyx",
                        provider_id="not-held",
                    ),
                    PhoneNumber(
                        user_id=owner,
                        workspace_id=world.alice.workspace.id,
                        phone_number=NUMBER_C,
                        provider="telnyx",
                        provider_id="released",
                        status=PhoneNumberStatus.RELEASED.value,
                    ),
                    PhoneNumber(
                        user_id=user_id_to_uuid(world.bob.user.id),
                        workspace_id=world.bob.workspace.id,
                        phone_number="+15550100004",
                        provider="twilio",
                        provider_id="PNx",
                    ),
                ]
            )
            await session.commit()

        async with world.sessions() as session:
            before = [
                (r.id, r.phone_number, r.status, r.workspace_id, r.assigned_agent_id)
                for r in (await session.execute(select(PhoneNumber))).scalars()
            ]
            failures = await find_unverified_numbers(session, factory=world.carrier.factory)
            await session.rollback()
        async with world.sessions() as session:
            after = [
                (r.id, r.phone_number, r.status, r.workspace_id, r.assigned_agent_id)
                for r in (await session.execute(select(PhoneNumber))).scalars()
            ]

        assert sorted(f.phone_number for f in failures) == [NUMBER_B, "+15550100004"]
        assert all(f.reason for f in failures)
        assert all(f.workspace_id for f in failures)
        assert sorted(before) == sorted(after)

    @pytest.mark.asyncio
    async def test_an_unreachable_carrier_is_listed_not_passed(self, world: World) -> None:
        async with world.sessions() as session:
            session.add(
                PhoneNumber(
                    user_id=user_id_to_uuid(world.alice.user.id),
                    workspace_id=world.alice.workspace.id,
                    phone_number=NUMBER_A,
                    provider="telnyx",
                    provider_id="x",
                )
            )
            await session.commit()
        world.carrier.error = CarrierLookupError("down")

        async with world.sessions() as session:
            failures = await find_unverified_numbers(session, factory=world.carrier.factory)

        assert [f.reason for f in failures] == [
            "Could not verify the phone number with the carrier, try again."
        ]


class TestAgentPhoneFieldIsNotAnotherWayIn:
    """`Agent.phone_number_id` routes calls, so it must not accept what registration refuses."""

    async def _agent_put(self, world: World, reference: str, agent: Agent | None = None) -> Any:
        target = agent or world.alice.agent
        return await world.client.put(
            f"/api/v1/agents/{target.id}",
            headers=world.alice.headers,
            json={"phone_number_id": reference},
        )

    @pytest.mark.asyncio
    async def test_a_spoofed_provider_id_cannot_claim_a_number_never_proven(
        self, world: World
    ) -> None:
        """Alice proves NUMBER_A, but writes NUMBER_B into the free-text provider_id."""
        _holds(world, world.alice, NUMBER_A)
        registered = await _register(world, world.alice, NUMBER_A, provider_id=NUMBER_B)
        assert registered.status_code == 201

        put = await self._agent_put(world, NUMBER_B)
        post = await world.client.post(
            "/api/v1/agents",
            headers=world.alice.headers,
            json={
                "name": "Claimer",
                "pricing_tier": "premium",
                "system_prompt": "You answer the phone politely.",
                "phone_number_id": NUMBER_B,
            },
        )

        assert put.status_code == 403
        assert post.status_code == 403
        async with world.sessions() as session:
            assert await get_agent_by_phone_number(NUMBER_B, session) is None

    @pytest.mark.asyncio
    async def test_a_released_number_cannot_be_written_into_an_agent(self, world: World) -> None:
        _holds(world, world.alice, NUMBER_A)
        number_id = (await _register(world, world.alice, NUMBER_A)).json()["id"]
        await world.client.put(
            f"/api/v1/phone-numbers/{number_id}",
            headers=world.alice.headers,
            json={"status": PhoneNumberStatus.RELEASED.value},
        )

        by_number = await self._agent_put(world, NUMBER_A)
        by_carrier_id = await self._agent_put(world, "carrier-id")

        assert by_number.status_code == 403
        assert by_carrier_id.status_code == 403

    @pytest.mark.asyncio
    async def test_an_agent_cannot_take_a_number_from_another_workspace(self, world: World) -> None:
        _holds(world, world.alice, NUMBER_A)
        assert (await _register(world, world.alice, NUMBER_A)).status_code == 201
        async with world.sessions() as session:
            elsewhere = Agent(
                user_id=world.alice.user.id,
                name="Other workspace agent",
                system_prompt="You answer the phone politely.",
                pricing_tier="premium",
                is_active=True,
                enabled_tools=[],
                enabled_tool_ids={},
            )
            session.add(elsewhere)
            await session.commit()
            await session.refresh(elsewhere)
            session.add(
                AgentWorkspace(
                    agent_id=elsewhere.id,
                    workspace_id=world.alice.second_workspace.id,
                    is_default=True,
                )
            )
            await session.commit()

        response = await self._agent_put(world, NUMBER_A, agent=elsewhere)

        assert response.status_code == 422
        assert "different workspace" in response.json()["detail"]


class TestNumberLifecycle:
    async def _registered(self, world: World) -> str:
        _holds(world, world.alice, NUMBER_A)
        response = await _register(world, world.alice, NUMBER_A)
        assert response.status_code == 201
        world.carrier.asked.clear()
        return str(response.json()["id"])

    @pytest.mark.asyncio
    async def test_an_unknown_status_is_refused(self, world: World) -> None:
        number_id = await self._registered(world)

        response = await world.client.put(
            f"/api/v1/phone-numbers/{number_id}",
            headers=world.alice.headers,
            json={"status": "bogus"},
        )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_suspended_back_to_active_is_verified_again(self, world: World) -> None:
        number_id = await self._registered(world)
        url = f"/api/v1/phone-numbers/{number_id}"
        suspended = await world.client.put(
            url, headers=world.alice.headers, json={"status": "suspended"}
        )
        assert suspended.status_code == 200
        world.carrier.accounts.clear()  # the number left the account while suspended

        refused = await world.client.put(
            url, headers=world.alice.headers, json={"status": "active"}
        )
        _holds(world, world.alice, NUMBER_A)
        allowed = await world.client.put(
            url, headers=world.alice.headers, json={"status": "active"}
        )

        assert refused.status_code == 422
        assert allowed.status_code == 200

    @pytest.mark.asyncio
    async def test_a_number_is_assigned_only_to_an_agent_of_its_own_workspace(
        self, world: World
    ) -> None:
        number_id = await self._registered(world)
        async with world.sessions() as session:
            elsewhere = Agent(
                user_id=world.alice.user.id,
                name="Other workspace agent",
                system_prompt="You answer the phone politely.",
                pricing_tier="premium",
                is_active=True,
                enabled_tools=[],
                enabled_tool_ids={},
            )
            session.add(elsewhere)
            await session.commit()
            await session.refresh(elsewhere)
            session.add(
                AgentWorkspace(
                    agent_id=elsewhere.id,
                    workspace_id=world.alice.second_workspace.id,
                    is_default=True,
                )
            )
            await session.commit()

        response = await world.client.put(
            f"/api/v1/phone-numbers/{number_id}",
            headers=world.alice.headers,
            json={"assigned_agent_id": str(elsewhere.id)},
        )

        assert response.status_code == 422
        assert "different workspace" in response.json()["detail"]
        async with world.sessions() as session:
            stored = await session.get(PhoneNumber, uuid.UUID(number_id))
            assert stored is not None
            assert stored.assigned_agent_id is None

    @pytest.mark.asyncio
    async def test_the_number_is_stored_in_e164_form(self, world: World) -> None:
        _holds(world, world.alice, NUMBER_A)

        response = await _register(world, world.alice, NUMBER_A.lstrip("+"))

        assert response.status_code == 201
        assert response.json()["phone_number"] == NUMBER_A
