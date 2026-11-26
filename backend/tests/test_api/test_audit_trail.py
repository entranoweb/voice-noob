"""The audit trail: what is recorded, who can read it, and that it cannot be changed."""

import json
from typing import Any

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.auth import get_password_hash
from app.core.audit import AuditAction, record_audit, sanitize_details
from app.models.audit_event import AuditEvent, AuditEventImmutableError
from app.models.user import User

SECRET = "sk-live-must-never-reach-the-trail-9999"


def _sessions(engine: Any) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _events(engine: Any) -> list[AuditEvent]:
    async with _sessions(engine)() as session:
        result = await session.execute(sa.select(AuditEvent).order_by(AuditEvent.created_at))
        return list(result.scalars().all())


@pytest.mark.asyncio
async def test_credential_change_is_recorded_without_the_credential(
    authenticated_test_client: tuple[AsyncClient, User], test_engine: Any
) -> None:
    client, user = authenticated_test_client

    response = await client.post(
        "/api/v1/settings", json={"openai_api_key": SECRET, "telnyx_api_key": ""}
    )
    assert response.status_code == 200

    events = await _events(test_engine)
    assert [event.action for event in events] == [AuditAction.CREDENTIAL_UPDATE]
    event = events[0]
    assert event.actor_user_id == user.id
    assert event.target_type == "provider_settings"
    assert event.success is True
    assert event.details == {"set": ["openai_api_key"], "cleared": ["telnyx_api_key"]}
    assert SECRET not in json.dumps(event.details)


@pytest.mark.asyncio
async def test_agent_changes_and_number_assignment_are_recorded(
    authenticated_test_client: tuple[AsyncClient, User], test_engine: Any
) -> None:
    client, _user = authenticated_test_client

    created = await client.post(
        "/api/v1/agents",
        json={"name": "Reception", "pricing_tier": "premium", "system_prompt": "Be helpful."},
    )
    assert created.status_code == 201, created.text
    agent_id = created.json()["id"]

    updated = await client.put(
        f"/api/v1/agents/{agent_id}",
        json={"system_prompt": "A prompt with something private in it", "phone_number_id": "pn-1"},
    )
    assert updated.status_code == 200, updated.text

    deleted = await client.delete(f"/api/v1/agents/{agent_id}")
    assert deleted.status_code == 204

    events = await _events(test_engine)
    assert [event.action for event in events] == [
        AuditAction.AGENT_CREATE,
        AuditAction.AGENT_UPDATE,
        AuditAction.PHONE_NUMBER_ASSIGN,
        AuditAction.AGENT_DELETE,
    ]
    assert all(event.target_id == agent_id for event in events)
    # Field names only: the prompt text is not copied into the trail.
    assert events[1].details == {"fields": ["phone_number_id", "system_prompt"]}
    assert events[2].details == {"from": None, "to": "pn-1"}


@pytest.mark.asyncio
async def test_the_trail_is_owner_scoped_and_paginated(
    authenticated_test_client: tuple[AsyncClient, User], test_engine: Any
) -> None:
    client, user = authenticated_test_client
    async with _sessions(test_engine)() as session:
        for index in range(3):
            await record_audit(
                session,
                AuditAction.AGENT_UPDATE,
                actor_user_id=user.id,
                target_type="agent",
                target_id=f"mine-{index}",
            )
        await record_audit(
            session,
            AuditAction.AGENT_UPDATE,
            actor_user_id=user.id + 1000,
            target_type="agent",
            target_id="someone-elses",
        )
        await record_audit(
            session, AuditAction.DATA_EXPORT, actor_user_id=user.id, target_type="account"
        )
        await session.commit()

    page = await client.get("/api/v1/audit/events", params={"limit": 2})
    assert page.status_code == 200
    body = page.json()
    assert body["total"] == 4
    assert len(body["events"]) == 2
    assert body["limit"] == 2
    assert body["offset"] == 0

    everything = await client.get("/api/v1/audit/events", params={"limit": 200})
    targets = {event["target_id"] for event in everything.json()["events"]}
    assert "someone-elses" not in targets
    assert {"mine-0", "mine-1", "mine-2"} <= targets

    filtered = await client.get("/api/v1/audit/events", params={"action": AuditAction.DATA_EXPORT})
    assert filtered.json()["total"] == 1


@pytest.mark.asyncio
async def test_the_trail_requires_a_signed_in_user(test_client: AsyncClient) -> None:
    response = await test_client.get("/api/v1/audit/events")

    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_failed_and_successful_logins_are_recorded(
    test_client: AsyncClient, test_session: AsyncSession, test_engine: Any
) -> None:
    password = "correct horse battery staple"
    user = User(
        email="login-audit@example.com",
        hashed_password=get_password_hash(password),
        full_name="Login Audit",
        is_active=True,
        is_superuser=False,
    )
    test_session.add(user)
    await test_session.commit()
    await test_session.refresh(user)

    refused = await test_client.post(
        "/api/v1/auth/login", data={"username": user.email, "password": "wrong"}
    )
    assert refused.status_code == 401
    accepted = await test_client.post(
        "/api/v1/auth/login", data={"username": user.email, "password": password}
    )
    assert accepted.status_code == 200

    events = await _events(test_engine)
    assert [(event.action, event.success) for event in events] == [
        (AuditAction.LOGIN_FAILED, False),
        (AuditAction.LOGIN_SUCCESS, True),
    ]
    assert all(event.actor_user_id == user.id for event in events)
    assert password not in json.dumps([event.details for event in events])


@pytest.mark.asyncio
async def test_audit_rows_cannot_be_changed_or_deleted(test_engine: Any) -> None:
    async with _sessions(test_engine)() as session:
        await record_audit(session, AuditAction.DATA_EXPORT, actor_user_id=1)
        await session.commit()

    async with _sessions(test_engine)() as session:
        event = (await session.execute(sa.select(AuditEvent))).scalar_one()
        event.action = "rewritten.history"
        with pytest.raises(AuditEventImmutableError):
            await session.flush()

    async with _sessions(test_engine)() as session:
        event = (await session.execute(sa.select(AuditEvent))).scalar_one()
        await session.delete(event)
        with pytest.raises(AuditEventImmutableError):
            await session.flush()

    assert [event.action for event in await _events(test_engine)] == [AuditAction.DATA_EXPORT]


def test_details_are_masked_at_any_depth() -> None:
    clean = sanitize_details(
        {
            "fields": ["openai_api_key"],
            "openai_api_key": SECRET,
            "nested": {"refresh_token": SECRET, "count": 3},
            "password": "short",
        }
    )

    assert SECRET not in json.dumps(clean)
    assert clean["openai_api_key"] == "****9999"
    assert clean["nested"] == {"refresh_token": "****9999", "count": 3}
    assert clean["password"] == "****"
    assert clean["fields"] == ["openai_api_key"]


@pytest.mark.asyncio
async def test_audit_lines_are_logged_whatever_the_application_log_level(
    test_engine: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    # The suite runs with DEBUG off, so the application logger drops INFO, as
    # production does. The audit line must still come out.
    async with _sessions(test_engine)() as session:
        await record_audit(
            session,
            AuditAction.DATA_ERASURE,
            actor_user_id=42,
            target_type="account",
            details={"api_key": SECRET},
        )
        await session.commit()

    lines = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{") and '"audit_event"' in line
    ]
    assert len(lines) == 1
    assert lines[0]["action"] == AuditAction.DATA_ERASURE
    assert lines[0]["actor_user_id"] == 42
    assert lines[0]["level"] == "info"
    assert SECRET not in json.dumps(lines[0])
