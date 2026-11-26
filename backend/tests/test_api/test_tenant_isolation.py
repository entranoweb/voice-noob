"""One customer must not reach another's agents, numbers, contacts or calls.

Two accounts, Alice and Bob, on one deployment, each signed in with a real
token (nothing here overrides authentication). Every test has Alice hand the
API an id or a phone number that belongs to Bob and checks that she is told it
does not exist, and that Bob's data is unchanged.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.auth import create_access_token, get_password_hash
from app.api.telephony import get_agent_by_phone_number
from app.core.auth import user_id_to_uuid
from app.core.config import settings
from app.db.redis import get_redis
from app.db.session import get_db
from app.main import app
from app.models.agent import Agent
from app.models.appointment import Appointment
from app.models.audit_event import AuditEvent
from app.models.contact import Contact
from app.models.phone_number import PhoneNumber, PhoneNumberStatus
from app.models.test_scenario import TestScenario
from app.models.user import User
from app.models.workspace import AgentWorkspace, Workspace

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

BOB_NUMBER = "+442079460001"
ALICE_NUMBER = "+442079460002"


@dataclass
class Tenant:
    user: User
    headers: dict[str, str]
    workspace: Workspace
    agent: Agent


@dataclass
class World:
    client: AsyncClient
    sessions: async_sessionmaker[AsyncSession]
    alice: Tenant
    bob: Tenant
    admin: User
    admin_headers: dict[str, str]


def _bearer(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


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

    workspace = Workspace(user_id=user.id, name=f"{name} clinic")
    agent = Agent(
        user_id=user.id,
        name=f"{name} receptionist",
        system_prompt=f"You answer the phone for {name}. HOUSE-RULE-{name.upper()}.",
        pricing_tier="premium",
        is_active=True,
        enabled_tools=["crm", "bookings"],
        enabled_tool_ids={},
    )
    session.add_all([workspace, agent])
    await session.commit()
    await session.refresh(workspace)
    await session.refresh(agent)
    session.add(AgentWorkspace(agent_id=agent.id, workspace_id=workspace.id, is_default=True))
    await session.commit()
    return Tenant(user=user, headers=_bearer(user), workspace=workspace, agent=agent)


@pytest_asyncio.fixture
async def world(test_engine: Any, test_redis: Any) -> AsyncGenerator[World, None]:
    sessions = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with sessions() as session:
            yield session

    async def override_get_redis() -> Any:
        return test_redis

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_redis] = override_get_redis

    async with sessions() as session:
        alice = await _tenant(session, "alice")
        bob = await _tenant(session, "bob")
        admin = User(
            email="root@example.com",
            hashed_password=get_password_hash("root-correct-horse"),
            full_name="Root",
            is_active=True,
            is_superuser=True,
        )
        session.add(admin)
        await session.commit()
        await session.refresh(admin)

    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield World(
                client=client,
                sessions=sessions,
                alice=alice,
                bob=bob,
                admin=admin,
                admin_headers=_bearer(admin),
            )
    finally:
        app.dependency_overrides.clear()


async def _register_number(world: World, tenant: Tenant, number: str, **extra: Any) -> PhoneNumber:
    async with world.sessions() as session:
        record = PhoneNumber(
            user_id=user_id_to_uuid(tenant.user.id),
            phone_number=number,
            provider="twilio",
            provider_id=f"PN{uuid.uuid4().hex}",
            status=PhoneNumberStatus.ACTIVE.value,
            **extra,
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record


class TestToolExecution:
    """POST /api/v1/tools/execute acts in the workspace of the agent it is given."""

    @pytest.mark.asyncio
    async def test_another_customers_agent_is_not_found(self, world: World) -> None:
        async with world.sessions() as session:
            session.add(
                Contact(
                    user_id=world.bob.user.id,
                    workspace_id=world.bob.workspace.id,
                    first_name="Private",
                    last_name="Patient",
                    phone_number="+447700900123",
                )
            )
            await session.commit()

        response = await world.client.post(
            "/api/v1/tools/execute",
            headers=world.alice.headers,
            json={
                "tool_name": "search_customer",
                "arguments": {"query": "Private"},
                "agent_id": str(world.bob.agent.id),
            },
        )

        assert response.status_code == 404
        assert "Private" not in response.text

    @pytest.mark.asyncio
    async def test_it_cannot_write_into_another_customers_contacts(self, world: World) -> None:
        response = await world.client.post(
            "/api/v1/tools/execute",
            headers=world.alice.headers,
            json={
                "tool_name": "create_contact",
                "arguments": {
                    "first_name": "Planted",
                    "last_name": "Row",
                    "phone_number": "+447700900999",
                },
                "agent_id": str(world.bob.agent.id),
            },
        )

        assert response.status_code == 404
        async with world.sessions() as session:
            planted = await session.execute(select(Contact).where(Contact.first_name == "Planted"))
            assert planted.scalars().first() is None

    @pytest.mark.asyncio
    async def test_the_owner_can_still_run_an_enabled_tool(self, world: World) -> None:
        response = await world.client.post(
            "/api/v1/tools/execute",
            headers=world.alice.headers,
            json={
                "tool_name": "search_customer",
                "arguments": {"query": "nobody"},
                "agent_id": str(world.alice.agent.id),
            },
        )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_a_tool_the_owner_did_not_enable_is_refused(self, world: World) -> None:
        async with world.sessions() as session:
            agent = await session.get(Agent, world.alice.agent.id)
            assert agent is not None
            agent.enabled_tools = ["crm"]
            agent.enabled_tool_ids = {"crm": ["search_customer"]}
            await session.commit()

        response = await world.client.post(
            "/api/v1/tools/execute",
            headers=world.alice.headers,
            json={
                "tool_name": "cancel_appointment",
                "arguments": {"appointment_id": 1},
                "agent_id": str(world.alice.agent.id),
            },
        )

        assert response.status_code == 403


class TestRealtimeRoutes:
    @pytest.mark.asyncio
    async def test_token_route_does_not_return_another_customers_prompt(self, world: World) -> None:
        response = await world.client.get(
            f"/api/v1/realtime/token/{world.bob.agent.id}",
            headers=world.alice.headers,
            params={"workspace_id": str(world.alice.workspace.id)},
        )

        assert response.status_code == 404
        assert "HOUSE-RULE-BOB" not in response.text

    @pytest.mark.asyncio
    async def test_session_route_does_not_run_another_customers_agent(self, world: World) -> None:
        response = await world.client.post(
            f"/api/v1/realtime/session/{world.bob.agent.id}",
            headers=world.alice.headers,
            params={"workspace_id": str(world.alice.workspace.id)},
            content=b"v=0\r\n",
        )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_transcript_route_refuses_another_customers_agent(self, world: World) -> None:
        response = await world.client.post(
            f"/api/v1/realtime/transcript/{world.bob.agent.id}",
            headers=world.alice.headers,
            json={"session_id": "s-1", "transcript": "planted", "duration_seconds": 3},
        )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_transcript_route_accepts_the_owner(self, world: World) -> None:
        """It used to compare an integer owner with a UUID and refuse everybody."""
        response = await world.client.post(
            f"/api/v1/realtime/transcript/{world.alice.agent.id}",
            headers=world.alice.headers,
            json={"session_id": "s-1", "transcript": "", "duration_seconds": 3},
        )

        assert response.status_code == 200


class TestPhoneNumberAssignment:
    @pytest.mark.asyncio
    async def test_a_number_cannot_be_assigned_to_another_customers_agent(
        self, world: World
    ) -> None:
        record = await _register_number(world, world.alice, ALICE_NUMBER)

        response = await world.client.put(
            f"/api/v1/phone-numbers/{record.id}",
            headers=world.alice.headers,
            json={"assigned_agent_id": str(world.bob.agent.id)},
        )

        assert response.status_code == 404
        assert "bob receptionist" not in response.text
        async with world.sessions() as session:
            stored = await session.get(PhoneNumber, record.id)
            assert stored is not None
            assert stored.assigned_agent_id is None

    @pytest.mark.asyncio
    async def test_a_number_can_be_assigned_to_ones_own_agent(self, world: World) -> None:
        record = await _register_number(world, world.alice, ALICE_NUMBER)

        response = await world.client.put(
            f"/api/v1/phone-numbers/{record.id}",
            headers=world.alice.headers,
            json={"assigned_agent_id": str(world.alice.agent.id)},
        )

        assert response.status_code == 200
        assert response.json()["assigned_agent_id"] == str(world.alice.agent.id)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("spelling", [BOB_NUMBER, BOB_NUMBER.lstrip("+")])
    async def test_a_second_account_cannot_register_a_held_number(
        self, world: World, spelling: str
    ) -> None:
        await _register_number(world, world.bob, BOB_NUMBER)

        response = await world.client.post(
            "/api/v1/phone-numbers",
            headers=world.alice.headers,
            json={"phone_number": spelling, "provider": "twilio", "provider_id": "PNalice"},
        )

        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_the_database_itself_refuses_two_live_owners(self, world: World) -> None:
        """The index, not only the route: a row written any other way is stopped too."""
        await _register_number(world, world.bob, BOB_NUMBER)

        with pytest.raises(IntegrityError):
            await _register_number(world, world.alice, BOB_NUMBER.lstrip("+"))

    @pytest.mark.asyncio
    async def test_a_released_number_can_be_registered_again(self, world: World) -> None:
        async with world.sessions() as session:
            session.add(
                PhoneNumber(
                    user_id=user_id_to_uuid(world.bob.user.id),
                    phone_number=BOB_NUMBER,
                    provider="twilio",
                    provider_id="PNold",
                    status=PhoneNumberStatus.RELEASED.value,
                )
            )
            await session.commit()

        response = await world.client.post(
            "/api/v1/phone-numbers",
            headers=world.alice.headers,
            json={"phone_number": BOB_NUMBER, "provider": "twilio", "provider_id": "PNnew"},
        )

        assert response.status_code == 201


def _agent_body(**extra: Any) -> dict[str, Any]:
    return {
        "name": "Second line",
        "pricing_tier": "premium",
        "system_prompt": "You answer the second line politely.",
        **extra,
    }


class TestAgentPhoneField:
    """`phone_number_id` is free text that inbound routing reads."""

    @pytest.mark.asyncio
    async def test_create_refuses_another_customers_number(self, world: World) -> None:
        await _register_number(world, world.bob, BOB_NUMBER)

        response = await world.client.post(
            "/api/v1/agents",
            headers=world.alice.headers,
            json=_agent_body(phone_number_id=BOB_NUMBER),
        )

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_create_refuses_a_number_nobody_has_registered(self, world: World) -> None:
        """A public number typed in before its real owner registers it."""
        response = await world.client.post(
            "/api/v1/agents",
            headers=world.alice.headers,
            json=_agent_body(phone_number_id="442079460099"),
        )

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_update_refuses_another_customers_number_by_any_reference(
        self, world: World
    ) -> None:
        record = await _register_number(world, world.bob, BOB_NUMBER)

        for reference in (BOB_NUMBER, BOB_NUMBER.lstrip("+"), str(record.id), record.provider_id):
            response = await world.client.put(
                f"/api/v1/agents/{world.alice.agent.id}",
                headers=world.alice.headers,
                json={"phone_number_id": reference},
            )
            assert response.status_code == 403, reference

        async with world.sessions() as session:
            agent = await session.get(Agent, world.alice.agent.id)
            assert agent is not None
            assert agent.phone_number_id is None

    @pytest.mark.asyncio
    async def test_ones_own_number_and_a_carrier_id_are_accepted(self, world: World) -> None:
        await _register_number(world, world.alice, ALICE_NUMBER)

        own = await world.client.put(
            f"/api/v1/agents/{world.alice.agent.id}",
            headers=world.alice.headers,
            json={"phone_number_id": ALICE_NUMBER},
        )
        carrier_id = await world.client.post(
            "/api/v1/agents",
            headers=world.alice.headers,
            json=_agent_body(phone_number_id="PN0123456789abcdef0123456789abcdef"),
        )

        assert own.status_code == 200
        assert carrier_id.status_code == 201


class TestInboundRouting:
    """A dialled number resolves to the account that holds it, or to nobody."""

    @pytest.mark.asyncio
    async def test_a_claim_written_into_another_accounts_agent_is_ignored(
        self, world: World
    ) -> None:
        """The takeover: Alice's agent row says it has Bob's number."""
        await _register_number(world, world.bob, BOB_NUMBER, assigned_agent_id=world.bob.agent.id)
        async with world.sessions() as session:
            agent = await session.get(Agent, world.alice.agent.id)
            assert agent is not None
            agent.phone_number_id = BOB_NUMBER  # as if written before the check existed
            await session.commit()

        async with world.sessions() as session:
            found = await get_agent_by_phone_number(BOB_NUMBER, session)

        assert found is not None
        assert found.id == world.bob.agent.id

    @pytest.mark.asyncio
    async def test_a_number_claimed_by_two_accounts_with_no_record_reaches_nobody(
        self, world: World
    ) -> None:
        async with world.sessions() as session:
            for tenant in (world.alice, world.bob):
                agent = await session.get(Agent, tenant.agent.id)
                assert agent is not None
                agent.phone_number_id = BOB_NUMBER
            await session.commit()

        async with world.sessions() as session:
            assert await get_agent_by_phone_number(BOB_NUMBER, session) is None

    @pytest.mark.asyncio
    async def test_an_assignment_to_another_accounts_agent_does_not_route(
        self, world: World
    ) -> None:
        """A record that points across accounts, however the row came to say so."""
        await _register_number(
            world, world.alice, ALICE_NUMBER, assigned_agent_id=world.bob.agent.id
        )

        async with world.sessions() as session:
            assert await get_agent_by_phone_number(ALICE_NUMBER, session) is None


class TestCampaigns:
    @pytest.mark.asyncio
    async def test_a_campaign_cannot_be_created_in_another_customers_workspace(
        self, world: World
    ) -> None:
        response = await world.client.post(
            "/api/v1/campaigns",
            headers=world.alice.headers,
            json={
                "workspace_id": str(world.bob.workspace.id),
                "agent_id": str(world.alice.agent.id),
                "name": "Recall",
                "from_phone_number": ALICE_NUMBER,
            },
        )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_a_campaign_cannot_use_another_customers_agent(self, world: World) -> None:
        response = await world.client.post(
            "/api/v1/campaigns",
            headers=world.alice.headers,
            json={
                "workspace_id": str(world.alice.workspace.id),
                "agent_id": str(world.bob.agent.id),
                "name": "Recall",
                "from_phone_number": ALICE_NUMBER,
            },
        )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_the_owner_can_create_a_campaign(self, world: World) -> None:
        """This used to answer 404 for everyone: the owner check compared types that never match."""
        response = await world.client.post(
            "/api/v1/campaigns",
            headers=world.alice.headers,
            json={
                "workspace_id": str(world.alice.workspace.id),
                "agent_id": str(world.alice.agent.id),
                "name": "Recall",
                "from_phone_number": ALICE_NUMBER,
            },
        )

        assert response.status_code == 200
        assert response.json()["name"] == "Recall"


class TestIntegrationsInAWorkspace:
    @pytest.mark.asyncio
    async def test_the_owner_can_connect_an_integration_to_their_workspace(
        self, world: World
    ) -> None:
        """It compared the integer owner with a UUID, so this always answered 404."""
        response = await world.client.post(
            "/api/v1/integrations",
            headers=world.alice.headers,
            json={
                "integration_id": "calendly",
                "integration_name": "Calendly",
                "workspace_id": str(world.alice.workspace.id),
                "credentials": {"access_token": "tok-alice"},
            },
        )

        assert response.status_code == 201
        assert response.json()["workspace_id"] == str(world.alice.workspace.id)

    @pytest.mark.asyncio
    async def test_it_cannot_be_connected_to_another_customers_workspace(
        self, world: World
    ) -> None:
        response = await world.client.post(
            "/api/v1/integrations",
            headers=world.alice.headers,
            json={
                "integration_id": "calendly",
                "integration_name": "Calendly",
                "workspace_id": str(world.bob.workspace.id),
                "credentials": {"access_token": "tok-alice"},
            },
        )

        assert response.status_code == 404


class TestCrmToolsStayInTheirWorkspace:
    """What a caller says is an id or a number; the workspace decides whether it counts."""

    @pytest.mark.asyncio
    async def test_an_appointment_id_from_another_workspace_cannot_be_cancelled(
        self, world: World
    ) -> None:
        from app.services.tools.registry import ToolRegistry

        async with world.sessions() as session:
            contact = Contact(
                user_id=world.bob.user.id,
                workspace_id=world.bob.workspace.id,
                first_name="Private",
                last_name="Patient",
                phone_number="+447700900123",
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)
            appointment = Appointment(
                contact_id=contact.id,
                workspace_id=world.bob.workspace.id,
                scheduled_at=datetime.now(UTC) + timedelta(days=2),
                duration_minutes=30,
                status="scheduled",
            )
            session.add(appointment)
            await session.commit()
            await session.refresh(appointment)
            appointment_id = appointment.id

        async with world.sessions() as session:
            registry = ToolRegistry(
                session, world.alice.user.id, workspace_id=world.alice.workspace.id
            )
            cancelled = await registry.execute_tool(
                "cancel_appointment", {"appointment_id": appointment_id, "reason": "caller said so"}
            )
            listed = await registry.execute_tool(
                "list_appointments", {"contact_phone": "+447700900123"}
            )

        assert cancelled["success"] is False
        assert not listed.get("appointments")
        async with world.sessions() as session:
            stored = await session.get(Appointment, appointment_id)
            assert stored is not None
            assert stored.status == "scheduled"


class TestSignUpAndDisabledAccounts:
    @pytest.mark.asyncio
    async def test_sign_up_is_closed_in_production(self, world: World) -> None:
        assert settings.DEBUG is False

        response = await world.client.post(
            "/api/v1/auth/register",
            json={"email": "mallory@example.com", "username": "Mallory", "password": "pw-123456"},
        )

        assert response.status_code == 403
        async with world.sessions() as session:
            made = await session.execute(select(User).where(User.email == "mallory@example.com"))
            assert made.scalars().first() is None

    @pytest.mark.asyncio
    async def test_sign_up_can_be_opened_on_purpose(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "ALLOW_PUBLIC_SIGNUP", True)

        response = await world.client.post(
            "/api/v1/auth/register",
            json={"email": "carol@example.com", "username": "Carol", "password": "pw-123456"},
        )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_the_administrator_creates_accounts_while_sign_up_is_closed(
        self, world: World
    ) -> None:
        body = {"email": "dave@example.com", "username": "Dave", "password": "pw-123456"}

        as_user = await world.client.post(
            "/api/v1/auth/users", headers=world.alice.headers, json=body
        )
        as_admin = await world.client.post(
            "/api/v1/auth/users", headers=world.admin_headers, json=body
        )

        assert as_user.status_code == 403
        assert as_admin.status_code == 201

    @pytest.mark.asyncio
    async def test_a_disabled_account_loses_access_on_its_next_request(self, world: World) -> None:
        before = await world.client.get("/api/v1/auth/me", headers=world.bob.headers)

        disabled = await world.client.post(
            f"/api/v1/auth/users/{world.bob.user.id}/disable", headers=world.admin_headers
        )
        after = await world.client.get("/api/v1/auth/me", headers=world.bob.headers)
        agents = await world.client.get("/api/v1/agents", headers=world.bob.headers)
        login = await world.client.post(
            "/api/v1/auth/login",
            data={"username": "bob@example.com", "password": "bob-correct-horse"},
        )

        assert before.status_code == 200
        assert disabled.status_code == 200
        assert disabled.json()["is_active"] is False
        async with world.sessions() as session:
            trail = (
                (
                    await session.execute(
                        select(AuditEvent).where(AuditEvent.action == "admin.account.disable")
                    )
                )
                .scalars()
                .all()
            )
        assert [(event.actor_user_id, event.target_id) for event in trail] == [
            (world.admin.id, str(world.bob.user.id))
        ]
        assert after.status_code == 401
        assert agents.status_code == 401
        assert login.status_code == 401

    @pytest.mark.asyncio
    async def test_an_account_can_be_switched_back_on(self, world: World) -> None:
        await world.client.post(
            f"/api/v1/auth/users/{world.bob.user.id}/disable", headers=world.admin_headers
        )

        enabled = await world.client.post(
            f"/api/v1/auth/users/{world.bob.user.id}/enable", headers=world.admin_headers
        )
        after = await world.client.get("/api/v1/auth/me", headers=world.bob.headers)

        assert enabled.status_code == 200
        assert after.status_code == 200

    @pytest.mark.asyncio
    async def test_only_the_administrator_can_disable_and_never_themselves(
        self, world: World
    ) -> None:
        by_user = await world.client.post(
            f"/api/v1/auth/users/{world.bob.user.id}/disable", headers=world.alice.headers
        )
        own = await world.client.post(
            f"/api/v1/auth/users/{world.admin.id}/disable", headers=world.admin_headers
        )

        assert by_user.status_code == 403
        assert own.status_code == 400
        still = await world.client.get("/api/v1/auth/me", headers=world.bob.headers)
        assert still.status_code == 200


class TestAzureEndpointSetting:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "endpoint",
        [
            "https://169.254.169.254/latest/meta-data",
            "https://127.0.0.1:6379",
            "https://10.0.0.5",
            "http://example-resource.openai.azure.com",
        ],
    )
    async def test_an_endpoint_inside_the_network_is_refused_when_saved(
        self, world: World, endpoint: str
    ) -> None:
        response = await world.client.post(
            "/api/v1/settings",
            headers=world.alice.headers,
            json={"azure_openai_endpoint": endpoint, "azure_openai_api_key": "k"},
        )

        assert response.status_code == 422


# A malformed id is the caller's mistake (4xx, never a 500), and an id that is
# someone else's answers exactly like one that does not exist.
_UNKNOWN = "00000000-0000-4000-8000-000000000000"
_NOT_A_UUID = "not-a-uuid"


def _by_id_requests(world: World, other: dict[str, str]) -> list[tuple[str, str, str, Any]]:
    """(label, method, url template with {}, json body) for every route under test."""
    ws = "workspace_id={}"
    return [
        ("call", "GET", "/api/v1/calls/{}", None),
        ("call-metrics", "GET", "/api/v1/calls/{}/metrics", None),
        ("agent-call-stats", "GET", "/api/v1/calls/agent/{}/stats", None),
        ("calls-agent-filter", "GET", "/api/v1/calls?agent_id={}", None),
        ("calls-workspace-filter", "GET", "/api/v1/calls?" + ws, None),
        ("agent", "GET", "/api/v1/agents/{}", None),
        ("phone-number", "GET", "/api/v1/phone-numbers/{}", None),
        ("phone-number-update", "PUT", "/api/v1/phone-numbers/{}", {"friendly_name": "x"}),
        ("phone-number-delete", "DELETE", "/api/v1/phone-numbers/{}", None),
        ("phone-numbers-workspace-filter", "GET", "/api/v1/phone-numbers?" + ws, None),
        ("qa-settings-get", "GET", "/api/v1/qa/workspace/{}/settings", None),
        (
            "qa-settings-put",
            "PUT",
            "/api/v1/qa/workspace/{}/settings",
            {"auto_evaluate": False},
        ),
        ("scenario-update", "PUT", "/api/v1/testing/scenarios/{}", {"name": "renamed"}),
        ("scenario-delete", "DELETE", "/api/v1/testing/scenarios/{}", None),
    ]


class TestBadIdsAreNotServerErrors:
    @pytest.mark.asyncio
    async def test_a_malformed_id_is_a_client_error_on_every_by_id_route(
        self, world: World
    ) -> None:
        for label, method, url, body in _by_id_requests(world, world.alice.headers):
            response = await world.client.request(
                method, url.format(_NOT_A_UUID), headers=world.alice.headers, json=body
            )
            assert 400 <= response.status_code < 500, (label, response.status_code)

    @pytest.mark.asyncio
    async def test_a_malformed_agent_id_in_an_outbound_call_is_a_client_error(
        self, world: World
    ) -> None:
        response = await world.client.post(
            f"/api/v1/telephony/calls?workspace_id={world.alice.workspace.id}",
            headers=world.alice.headers,
            json={"to_number": "+442079460010", "from_number": ALICE_NUMBER, "agent_id": "x"},
        )

        assert 400 <= response.status_code < 500


class TestNotYoursLooksLikeNotThere:
    """Another customer's id and an id nobody has give the same status and body."""

    @pytest.mark.asyncio
    async def test_phone_number_routes(self, world: World) -> None:
        bobs = await _register_number(world, world.bob, BOB_NUMBER)
        for method, body in (
            ("GET", None),
            ("PUT", {"friendly_name": "x"}),
            ("DELETE", None),
        ):
            theirs = await world.client.request(
                method, f"/api/v1/phone-numbers/{bobs.id}", headers=world.alice.headers, json=body
            )
            nobodys = await world.client.request(
                method, f"/api/v1/phone-numbers/{_UNKNOWN}", headers=world.alice.headers, json=body
            )
            assert theirs.status_code == nobodys.status_code == 404, method
            assert theirs.json() == nobodys.json(), method

    @pytest.mark.asyncio
    async def test_a_phone_number_cannot_be_registered_into_another_customers_workspace(
        self, world: World
    ) -> None:
        responses = []
        for workspace_id in (str(world.bob.workspace.id), _UNKNOWN):
            responses.append(
                await world.client.post(
                    "/api/v1/phone-numbers",
                    headers=world.alice.headers,
                    json={
                        "phone_number": ALICE_NUMBER,
                        "provider": "twilio",
                        "provider_id": "PN-test",
                        "workspace_id": workspace_id,
                    },
                )
            )

        assert responses[0].status_code == responses[1].status_code == 404
        assert responses[0].json() == responses[1].json()

    @pytest.mark.asyncio
    async def test_qa_workspace_settings(self, world: World) -> None:
        for method, body in (("GET", None), ("PUT", {"auto_evaluate": False})):
            theirs = await world.client.request(
                method,
                f"/api/v1/qa/workspace/{world.bob.workspace.id}/settings",
                headers=world.alice.headers,
                json=body,
            )
            nobodys = await world.client.request(
                method,
                f"/api/v1/qa/workspace/{_UNKNOWN}/settings",
                headers=world.alice.headers,
                json=body,
            )
            assert theirs.status_code == nobodys.status_code == 404, method
            assert theirs.json() == nobodys.json(), method

    @pytest.mark.asyncio
    async def test_testing_scenarios(self, world: World) -> None:
        async with world.sessions() as session:
            scenario = TestScenario(
                user_id=world.bob.user.id,
                name="bob's scenario",
                description="d",
                category="greeting",
                difficulty="easy",
                caller_persona={"name": "c"},
                conversation_flow=[],
                expected_behaviors=[],
                success_criteria={},
                is_active=True,
                is_built_in=False,
            )
            session.add(scenario)
            await session.commit()
            await session.refresh(scenario)

        for method, body in (("PUT", {"name": "renamed"}), ("DELETE", None)):
            theirs = await world.client.request(
                method,
                f"/api/v1/testing/scenarios/{scenario.id}",
                headers=world.alice.headers,
                json=body,
            )
            nobodys = await world.client.request(
                method,
                f"/api/v1/testing/scenarios/{_UNKNOWN}",
                headers=world.alice.headers,
                json=body,
            )
            assert theirs.status_code == nobodys.status_code == 404, method
            assert theirs.json() == nobodys.json(), method
