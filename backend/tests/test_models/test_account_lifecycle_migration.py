"""The account-lifecycle migration, up, down and up again, on a real PostgreSQL database.

Same arrangement as the caller-audio migration test: needs ``TEST_DATABASE_URL``,
makes its own database, runs the real ``alembic`` command, drops the database.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

BACKEND = Path(__file__).resolve().parents[2]
PARENT = "e3a7c9d41f68"
REVISION = "f4b8d2a65c19"
SERVER = os.getenv("TEST_DATABASE_URL", "")
TABLES = ("refresh_tokens", "password_reset_tokens", "api_tokens")

pytestmark = pytest.mark.skipif(
    not SERVER.startswith("postgresql"),
    reason="needs PostgreSQL: set TEST_DATABASE_URL (CI does)",
)


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": url, "DEBUG": "true"}
    done = subprocess.run(  # noqa: S603 - fixed arguments, our own interpreter
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.asyncio
async def test_upgrade_adds_the_token_tables_and_downgrade_removes_them() -> None:
    name = f"lifecycle_migration_{uuid.uuid4().hex[:10]}"
    admin = create_async_engine(SERVER, isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(SERVER).set(database=name).render_as_string(hide_password=False)
    engine = create_async_engine(url)

    async def state() -> tuple[set[str], int]:
        async with engine.connect() as connection:
            tables = {
                row[0]
                for row in await connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name = ANY(:names)"
                    ),
                    {"names": list(TABLES)},
                )
            }
            column = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name = 'users' AND column_name = 'token_version'"
                    )
                )
            ).scalar()
            return tables, int(column or 0)

    try:
        _alembic(url, "upgrade", PARENT)
        assert await state() == (set(), 0)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (email, hashed_password) "
                    "VALUES ('old@example.com', 'not-a-real-hash')"
                )
            )

        _alembic(url, "upgrade", REVISION)

        assert await state() == (set(TABLES), 1)
        async with engine.connect() as connection:
            version = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar()
            versions = (await connection.execute(text("SELECT token_version FROM users"))).scalars()
            existing = list(versions)
        assert version == REVISION
        assert existing == [0], "users that already existed start at token version 0"

        _alembic(url, "downgrade", PARENT)

        assert await state() == (set(), 0)
        async with engine.connect() as connection:
            users = (await connection.execute(text("SELECT count(*) FROM users"))).scalar()
        assert users == 1, "downgrading does not lose users"

        _alembic(url, "upgrade", REVISION)
        assert await state() == (set(TABLES), 1)
    finally:
        await engine.dispose()
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
