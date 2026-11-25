"""A small world for the account-lifecycle tests: real sign-in, no auth overrides.

The shared ``authenticated_test_client`` replaces the authentication dependency,
which is exactly what these tests must not do: they are about what the
dependency accepts. Everything here goes through ``/auth/login`` and real bearer
headers, with an in-memory mailer so a reset link can be read without a network.
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.auth import get_password_hash
from app.db.redis import get_redis
from app.db.session import get_db
from app.main import app
from app.models.user import User
from app.models.workspace import Workspace
from app.services.mail import FakeMailer, get_mailer

PASSWORD = "correct-horse-battery"  # noqa: S105 - a test fixture, not a credential


@dataclass
class Person:
    user: User
    workspace: Workspace
    password: str = PASSWORD


@dataclass
class Lifecycle:
    client: AsyncClient
    sessions: async_sessionmaker[AsyncSession]
    mailer: FakeMailer
    alice: Person
    bob: Person
    admin: User
    admin_password: str

    async def login(self, email: str, password: str = PASSWORD) -> dict[str, Any]:
        response = await self.client.post(
            "/api/v1/auth/login", data={"username": email, "password": password}
        )
        assert response.status_code == 200, response.text
        return response.json()

    async def login_headers(self, person: Person) -> dict[str, str]:
        tokens = await self.login(person.user.email, person.password)
        return {"Authorization": f"Bearer {tokens['access_token']}"}

    def reset_token_from_mail(self, index: int = -1) -> str:
        text = self.mailer.outbox[index].text
        match = re.search(r"reset-password\?token=([A-Za-z0-9_\-]+)", text)
        assert match, text
        return match.group(1)


async def _person(session: AsyncSession, name: str) -> Person:
    user = User(
        email=f"{name}@example.com",
        hashed_password=get_password_hash(PASSWORD),
        full_name=name.title(),
        is_active=True,
        is_superuser=False,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    workspace = Workspace(user_id=user.id, name=f"{name} workspace", is_default=True)
    session.add(workspace)
    await session.commit()
    await session.refresh(workspace)
    return Person(user=user, workspace=workspace)


@pytest_asyncio.fixture
async def lifecycle(test_engine: Any, test_redis: Any) -> AsyncGenerator[Lifecycle, None]:
    sessions = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    mailer = FakeMailer()

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with sessions() as session:
            yield session

    async def override_get_redis() -> Any:
        return test_redis

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_redis] = override_get_redis
    app.dependency_overrides[get_mailer] = lambda: mailer

    async with sessions() as session:
        alice = await _person(session, "alice")
        bob = await _person(session, "bob")
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
            yield Lifecycle(
                client=client,
                sessions=sessions,
                mailer=mailer,
                alice=alice,
                bob=bob,
                admin=admin,
                admin_password="root-correct-horse",  # noqa: S106
            )
    finally:
        app.dependency_overrides.clear()
