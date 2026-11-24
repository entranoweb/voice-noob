"""Export, erasure and retention: that they run, touch only the right account,
and that the status page reports what happened."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import fakeredis
import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.audit import AuditAction
from app.core.auth import user_id_to_uuid
from app.models.agent import Agent
from app.models.appointment import Appointment
from app.models.audit_event import AuditEvent
from app.models.call_evaluation import CallEvaluation
from app.models.call_interaction import CallInteraction
from app.models.call_record import CallRecord
from app.models.compliance_run import (
    KIND_ERASURE,
    KIND_EXPORT,
    KIND_RETENTION,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    TRIGGER_SCHEDULE,
    ComplianceRun,
)
from app.models.contact import Contact
from app.models.phone_number import PhoneNumber
from app.models.privacy_settings import ConsentRecord, PrivacySettings
from app.models.user import User
from app.models.user_integration import UserIntegration
from app.models.user_settings import UserSettings
from app.models.workspace import Workspace
from app.services import data_protection
from app.services.data_protection import sweep_if_due

OWNED_TABLES = [
    Agent,
    Workspace,
    Contact,
    Appointment,
    CallInteraction,
    CallRecord,
    CallEvaluation,
    PhoneNumber,
    UserSettings,
    UserIntegration,
    ConsentRecord,
    PrivacySettings,
]


def _sessions(engine: Any) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _count(engine: Any, model: Any) -> int:
    async with _sessions(engine)() as session:
        return (await session.execute(sa.select(sa.func.count()).select_from(model))).scalar() or 0


async def _runs(engine: Any, kind: str) -> list[ComplianceRun]:
    async with _sessions(engine)() as session:
        result = await session.execute(
            sa.select(ComplianceRun)
            .where(ComplianceRun.kind == kind)
            .order_by(ComplianceRun.started_at)
        )
        return list(result.scalars().all())


def _call(user_id: int, *, agent_id: uuid.UUID | None, created_at: datetime) -> CallRecord:
    return CallRecord(
        id=uuid.uuid4(),
        user_id=user_id_to_uuid(user_id),
        provider="twilio",
        provider_call_id="CA" + uuid.uuid4().hex,
        direction="inbound",
        status="completed",
        from_number="+441632960001",
        to_number="+441632960002",
        duration_seconds=60,
        agent_id=agent_id,
        created_at=created_at,
    )


async def _seed_account(engine: Any, user_id: int) -> dict[str, Any]:
    """One of everything an account can hold, across both owner-id types."""
    now = datetime.now(UTC)
    owner_uuid = user_id_to_uuid(user_id)
    async with _sessions(engine)() as session:
        workspace = Workspace(id=uuid.uuid4(), name=f"ws-{user_id}", user_id=user_id, settings={})
        agent = Agent(
            id=uuid.uuid4(),
            user_id=user_id,
            name=f"agent-{user_id}",
            system_prompt="Be helpful.",
            pricing_tier="balanced",
        )
        session.add_all([workspace, agent])
        await session.flush()

        contact = Contact(
            user_id=user_id,
            workspace_id=workspace.id,
            first_name="Pat",
            phone_number="+441632960003",
        )
        loose_contact = Contact(user_id=user_id, first_name="Sam", phone_number="+441632960004")
        session.add_all([contact, loose_contact])
        await session.flush()

        recent_call = _call(user_id, agent_id=agent.id, created_at=now)
        old_call = _call(user_id, agent_id=None, created_at=now - timedelta(days=500))
        session.add_all([recent_call, old_call])
        await session.flush()

        session.add_all(
            [
                Appointment(
                    contact_id=contact.id,
                    workspace_id=workspace.id,
                    scheduled_at=now + timedelta(days=1),
                    duration_minutes=30,
                    status="scheduled",
                ),
                CallInteraction(
                    contact_id=loose_contact.id,
                    call_started_at=now - timedelta(days=500),
                    outcome="answered",
                ),
                CallInteraction(
                    contact_id=contact.id,
                    workspace_id=workspace.id,
                    call_started_at=now,
                    outcome="answered",
                ),
                CallEvaluation(
                    id=uuid.uuid4(),
                    call_id=old_call.id,
                    overall_score=80,
                    passed=True,
                    evaluation_model="test",
                ),
                PhoneNumber(
                    user_id=owner_uuid,
                    phone_number=f"+44163296{user_id:04d}",
                    provider="twilio",
                    provider_id=f"PN{user_id}",
                ),
                UserSettings(user_id=owner_uuid, openai_api_key="sk-test-key-for-this-account"),
                UserIntegration(
                    user_id=owner_uuid,
                    integration_id="hubspot",
                    integration_name="HubSpot",
                    credentials={"access_token": "token-for-this-account"},
                ),
                ConsentRecord(user_id=user_id, consent_type="data_processing", granted=True),
                PrivacySettings(user_id=user_id, data_retention_days=365),
            ]
        )
        await session.commit()
        return {"recent_call": recent_call.id, "old_call": old_call.id}


async def _other_user(engine: Any) -> User:
    async with _sessions(engine)() as session:
        other = User(
            email="other-tenant@example.com",
            hashed_password="not-a-real-hash",  # noqa: S106
            full_name="Other Tenant",
            is_active=True,
            is_superuser=False,
        )
        session.add(other)
        await session.commit()
        await session.refresh(other)
        return other


def _check(status_body: dict[str, Any], framework: str, check_id: str) -> dict[str, Any]:
    return next(item for item in status_body[framework]["checks"] if item["id"] == check_id)


@pytest.mark.asyncio
async def test_export_returns_the_accounts_data_and_records_the_run(
    authenticated_test_client: tuple[AsyncClient, User], test_engine: Any
) -> None:
    client, user = authenticated_test_client
    other = await _other_user(test_engine)
    await _seed_account(test_engine, user.id)
    await _seed_account(test_engine, other.id)

    before = (await client.get("/api/v1/compliance/status")).json()
    assert _check(before, "gdpr", "data_export")["status"] == "warning"
    assert _check(before, "ccpa", "data_access")["status"] == "warning"

    response = await client.get("/api/v1/compliance/export")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [agent["name"] for agent in body["agents"]] == [f"agent-{user.id}"]
    assert [workspace["name"] for workspace in body["workspaces"]] == [f"ws-{user.id}"]
    assert sorted(contact["first_name"] for contact in body["contacts"]) == ["Pat", "Sam"]
    assert len(body["call_records"]) == 2
    assert len(body["call_interactions"]) == 2
    assert len(body["appointments"]) == 1
    assert len(body["phone_numbers"]) == 1
    assert [item["integration_id"] for item in body["integrations"]] == ["hubspot"]
    # Whether a key is set, never the key.
    assert body["settings"]["openai_api_key_set"] is True
    assert "sk-test-key-for-this-account" not in response.text
    assert "token-for-this-account" not in response.text

    runs = await _runs(test_engine, KIND_EXPORT)
    assert [(run.status, run.user_id) for run in runs] == [(STATUS_SUCCEEDED, user.id)]
    assert runs[0].counts is not None
    assert runs[0].counts["call_records"] == 2

    after = (await client.get("/api/v1/compliance/status")).json()
    assert _check(after, "gdpr", "data_export")["status"] == "complete"


@pytest.mark.asyncio
async def test_a_failed_export_is_recorded_and_the_status_page_says_so(
    authenticated_test_client: tuple[AsyncClient, User],
    test_engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, user = authenticated_test_client

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        message = "simulated failure"
        raise RuntimeError(message)

    monkeypatch.setattr("app.api.compliance._build_export", _boom)
    with pytest.raises(RuntimeError, match="simulated failure"):
        await client.get("/api/v1/compliance/export")
    monkeypatch.undo()

    runs = await _runs(test_engine, KIND_EXPORT)
    assert [(run.status, run.error, run.user_id) for run in runs] == [
        (STATUS_FAILED, "RuntimeError", user.id)
    ]
    status_body = (await client.get("/api/v1/compliance/status")).json()
    assert _check(status_body, "gdpr", "data_export")["status"] == "incomplete"


@pytest.mark.asyncio
async def test_erasure_removes_the_accounts_data_and_nobody_elses(
    authenticated_test_client: tuple[AsyncClient, User], test_engine: Any
) -> None:
    client, user = authenticated_test_client
    other = await _other_user(test_engine)
    await _seed_account(test_engine, user.id)
    await _seed_account(test_engine, other.id)
    before = {model.__tablename__: await _count(test_engine, model) for model in OWNED_TABLES}
    assert all(count >= 2 for count in before.values()), before

    response = await client.delete("/api/v1/compliance/data")

    assert response.status_code == 200, response.text
    deleted = response.json()["deleted_counts"]
    assert deleted["agents"] == 1
    assert deleted["workspaces"] == 1
    assert deleted["contacts"] == 2
    assert deleted["call_records"] == 2
    assert deleted["call_evaluations"] == 1
    assert deleted["phone_numbers"] == 1
    assert deleted["integrations"] == 1
    assert deleted["user_settings"] == 1

    # Exactly the other account's half of every table is left.
    after = {model.__tablename__: await _count(test_engine, model) for model in OWNED_TABLES}
    assert after == {table: count // 2 for table, count in before.items()}
    async with _sessions(test_engine)() as session:
        remaining = (await session.execute(sa.select(Agent.user_id))).scalars().all()
        assert remaining == [other.id]
        assert (await session.execute(sa.select(User.id))).scalars().all() == [user.id, other.id]

    runs = await _runs(test_engine, KIND_ERASURE)
    assert [(run.status, run.user_id) for run in runs] == [(STATUS_SUCCEEDED, user.id)]
    async with _sessions(test_engine)() as session:
        actions = (await session.execute(sa.select(AuditEvent.action))).scalars().all()
    assert AuditAction.DATA_ERASURE in actions

    status_body = (await client.get("/api/v1/compliance/status")).json()
    assert _check(status_body, "gdpr", "data_deletion")["status"] == "complete"


@pytest.mark.asyncio
async def test_scheduled_sweep_deletes_only_expired_call_data_and_runs_once_per_interval(
    authenticated_test_client: tuple[AsyncClient, User], test_engine: Any
) -> None:
    client, user = authenticated_test_client
    ids = await _seed_account(test_engine, user.id)
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)

    before = (await client.get("/api/v1/compliance/status")).json()
    retention = _check(before, "gdpr", "data_retention")
    assert retention["status"] == "incomplete"
    assert "not run" in retention["description"]

    async with _sessions(test_engine)() as session:
        first = await sweep_if_due(session, redis)
    async with _sessions(test_engine)() as session:
        second = await sweep_if_due(session, redis)

    assert first is not None
    assert first.status == STATUS_SUCCEEDED
    assert first.trigger == TRIGGER_SCHEDULE
    assert first.counts == {
        "call_interactions": 1,
        "call_evaluations": 1,
        "call_records": 1,
        "accounts": 1,
    }
    # Not due again inside the interval, and the lock was released.
    assert second is None
    assert await redis.get(data_protection.RETENTION_LOCK_KEY) is None

    async with _sessions(test_engine)() as session:
        calls = (await session.execute(sa.select(CallRecord.id))).scalars().all()
        assert calls == [ids["recent_call"]]
        # Consent is evidence, not call data: the sweep leaves it.
        assert (await session.execute(sa.select(ConsentRecord))).scalars().all()

    after = (await client.get("/api/v1/compliance/status")).json()
    assert _check(after, "gdpr", "data_retention")["status"] == "complete"
    assert len(await _runs(test_engine, KIND_RETENTION)) == 1


@pytest.mark.asyncio
async def test_sweep_does_not_run_while_another_process_holds_the_lock(test_engine: Any) -> None:
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    await redis.set(data_protection.RETENTION_LOCK_KEY, "another-worker")

    async with _sessions(test_engine)() as session:
        assert await sweep_if_due(session, redis) is None

    assert await _runs(test_engine, KIND_RETENTION) == []
    assert await redis.get(data_protection.RETENTION_LOCK_KEY) == "another-worker"


@pytest.mark.asyncio
async def test_manual_retention_covers_only_the_callers_account(
    authenticated_test_client: tuple[AsyncClient, User], test_engine: Any
) -> None:
    client, user = authenticated_test_client
    other = await _other_user(test_engine)
    mine = await _seed_account(test_engine, user.id)
    theirs = await _seed_account(test_engine, other.id)

    response = await client.post("/api/v1/compliance/retention/cleanup")

    assert response.status_code == 200, response.text
    assert response.json()["deleted_counts"]["call_records"] == 1
    async with _sessions(test_engine)() as session:
        calls = set((await session.execute(sa.select(CallRecord.id))).scalars().all())
    assert calls == {mine["recent_call"], theirs["recent_call"], theirs["old_call"]}
    # A manual run for one account is not the scheduled sweep.
    status_body = (await client.get("/api/v1/compliance/status")).json()
    assert _check(status_body, "gdpr", "data_retention")["status"] == "incomplete"
