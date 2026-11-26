"""A fresh install is usable: the first account has a workspace, and sign-up reports itself.

``synthiq-caller upload`` needs a workspace id. On a new install nobody had one
until somebody made it by hand in the dashboard (or the database).
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.auth import get_password_hash
from app.core.audit import AuditAction
from app.core.config import settings
from app.core.first_admin import ensure_first_admin
from app.db.redis import get_redis
from app.db.session import get_db
from app.main import app
from app.models.audit_event import AuditEvent
from app.models.user import User
from app.models.workspace import Workspace
from tests.test_api.test_caller_runs import _bundle

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

ADMIN_PASSWORD = "a-long-password-of-our-own"


@pytest_asyncio.fixture
async def install(
    test_engine: Any, test_redis: Any, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[tuple[async_sessionmaker[AsyncSession], AsyncClient], None]:
    """An empty database, as on first start."""
    monkeypatch.setattr(settings, "ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", ADMIN_PASSWORD)
    sessions = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with sessions() as session:
            yield session

    async def override_get_redis() -> Any:
        return test_redis

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_redis] = override_get_redis
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield sessions, client
    finally:
        app.dependency_overrides.clear()


async def _start(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        await ensure_first_admin(session)


async def _workspaces(sessions: async_sessionmaker[AsyncSession]) -> list[Workspace]:
    async with sessions() as session:
        return list((await session.execute(sa.select(Workspace))).scalars().all())


async def _bootstrap_events(sessions: async_sessionmaker[AsyncSession]) -> list[AuditEvent]:
    async with sessions() as session:
        result = await session.execute(
            sa.select(AuditEvent).where(AuditEvent.action == AuditAction.WORKSPACE_BOOTSTRAP)
        )
        return list(result.scalars().all())


@pytest.mark.asyncio
async def test_the_first_start_gives_the_first_account_a_default_workspace(
    install: tuple[async_sessionmaker[AsyncSession], AsyncClient],
) -> None:
    sessions, _client = install

    await _start(sessions)

    (workspace,) = await _workspaces(sessions)
    async with sessions() as session:
        admin = (await session.execute(sa.select(User))).scalars().one()
    assert workspace.user_id == admin.id
    assert workspace.is_default is True
    events = await _bootstrap_events(sessions)
    assert len(events) == 1
    assert events[0].workspace_id == workspace.id


@pytest.mark.asyncio
async def test_starting_again_creates_nothing_more(
    install: tuple[async_sessionmaker[AsyncSession], AsyncClient],
) -> None:
    sessions, _client = install

    for _ in range(3):
        await _start(sessions)

    assert len(await _workspaces(sessions)) == 1
    assert len(await _bootstrap_events(sessions)) == 1


@pytest.mark.asyncio
async def test_two_starts_at_once_still_make_one_workspace(
    install: tuple[async_sessionmaker[AsyncSession], AsyncClient],
) -> None:
    sessions, _client = install
    await _start(sessions)
    async with sessions() as session:
        await session.execute(sa.delete(Workspace))
        await session.commit()

    await asyncio.gather(_start(sessions), _start(sessions), _start(sessions))

    assert len(await _workspaces(sessions)) == 1
    assert len(await _bootstrap_events(sessions)) == 2, "one at first start, one after the wipe"


@pytest.mark.asyncio
async def test_an_account_made_before_this_change_gets_its_workspace_at_the_next_start(
    install: tuple[async_sessionmaker[AsyncSession], AsyncClient],
) -> None:
    sessions, _client = install
    async with sessions() as session:
        session.add(
            User(
                email="owner@example.com",
                hashed_password=get_password_hash(ADMIN_PASSWORD),
                is_active=True,
                is_superuser=True,
            )
        )
        await session.commit()

    await _start(sessions)

    assert len(await _workspaces(sessions)) == 1


@pytest.mark.asyncio
async def test_an_install_that_already_has_a_workspace_is_left_alone(
    install: tuple[async_sessionmaker[AsyncSession], AsyncClient],
) -> None:
    sessions, _client = install
    await _start(sessions)
    async with sessions() as session:
        other = User(
            email="other@example.com", hashed_password=get_password_hash("x" * 12), is_active=True
        )
        session.add(other)
        await session.commit()
        await session.refresh(other)
        session.add(Workspace(user_id=other.id, name="theirs", is_default=True))
        await session.execute(sa.delete(Workspace).where(Workspace.name != "theirs"))
        await session.commit()

    await _start(sessions)

    assert [w.name for w in await _workspaces(sessions)] == ["theirs"]


@pytest.mark.asyncio
async def test_after_a_fresh_start_the_first_user_can_upload_with_no_manual_setup(
    install: tuple[async_sessionmaker[AsyncSession], AsyncClient],
) -> None:
    sessions, client = install
    await _start(sessions)
    login = await client.post(
        "/api/v1/auth/login", data={"username": "owner@example.com", "password": ADMIN_PASSWORD}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    listed = await client.get("/api/v1/workspaces", headers=headers)
    workspace_id = listed.json()[0]["id"]
    uploaded = await client.post(
        "/api/v1/caller/runs",
        params={"workspace_id": workspace_id},
        content=json.dumps(_bundle()).encode(),
        headers={**headers, "Content-Type": "application/json"},
    )

    assert len(listed.json()) == 1
    assert uploaded.status_code == 201, uploaded.text


class TestSignUpIsReported:
    @pytest.mark.asyncio
    async def test_closed(
        self,
        install: tuple[async_sessionmaker[AsyncSession], AsyncClient],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _sessions, client = install
        monkeypatch.setattr(settings, "ALLOW_PUBLIC_SIGNUP", False)

        response = await client.get("/api/v1/auth/config")

        assert response.status_code == 200
        assert response.json() == {"signup_open": False}

    @pytest.mark.asyncio
    async def test_open(
        self,
        install: tuple[async_sessionmaker[AsyncSession], AsyncClient],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _sessions, client = install
        monkeypatch.setattr(settings, "ALLOW_PUBLIC_SIGNUP", True)

        response = await client.get("/api/v1/auth/config")

        assert response.json() == {"signup_open": True}

    @pytest.mark.asyncio
    async def test_the_answer_is_the_same_one_register_acts_on(
        self,
        install: tuple[async_sessionmaker[AsyncSession], AsyncClient],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _sessions, client = install
        monkeypatch.setattr(settings, "ALLOW_PUBLIC_SIGNUP", False)
        body = {"email": "new@example.com", "username": "new", "password": "long-enough-1"}

        reported = (await client.get("/api/v1/auth/config")).json()["signup_open"]
        registered = await client.post("/api/v1/auth/register", json=body)

        assert reported is False
        assert registered.status_code == 403
