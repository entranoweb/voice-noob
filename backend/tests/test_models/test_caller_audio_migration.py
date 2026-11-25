"""The caller-audio migration, up and down, on a real PostgreSQL database.

Needs ``TEST_DATABASE_URL`` (always set in CI; locally, point it at a throwaway
server). It makes its own database, runs the real ``alembic`` command against
it, and drops it, so it never touches the database the other tests use.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

BACKEND = Path(__file__).resolve().parents[2]
PARENT = "d2f5a8c31b47"
SERVER = os.getenv("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not SERVER.startswith("postgresql"),
    reason="needs PostgreSQL: set TEST_DATABASE_URL (CI does)",
)


def _script_head() -> str | None:
    """The newest revision in migrations/versions, read from the script directory."""
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head()


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
async def test_upgrade_adds_audio_and_the_30_day_default_and_downgrade_takes_them_away() -> None:
    name = f"audio_migration_{uuid.uuid4().hex[:10]}"
    admin = create_async_engine(SERVER, isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(SERVER).set(database=name).render_as_string(hide_password=False)
    engine = create_async_engine(url)

    async def table_and_column() -> tuple[bool, bool]:
        async with engine.connect() as connection:
            table = (await connection.execute(text("SELECT to_regclass('caller_audio')"))).scalar()
            column = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name = 'workspaces' AND column_name = 'audio_retention_days'"
                    )
                )
            ).scalar()
            return table is not None, bool(column)

    try:
        _alembic(url, "upgrade", PARENT)
        assert await table_and_column() == (False, False)
        # A workspace that existed before the migration.
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (email, hashed_password) "
                    "VALUES ('old@example.com', 'not-a-real-hash')"
                )
            )
            user_id = (await connection.execute(text("SELECT id FROM users"))).scalar()
            await connection.execute(
                text("INSERT INTO workspaces (id, name, user_id) VALUES (:id, 'Old', :u)"),
                {"id": str(uuid.uuid4()), "u": user_id},
            )

        _alembic(url, "upgrade", "head")

        assert await table_and_column() == (True, True)
        async with engine.connect() as connection:
            kept = (
                await connection.execute(text("SELECT audio_retention_days FROM workspaces"))
            ).scalar()
            version = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar()
            unique = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM pg_constraint WHERE conname = 'uq_caller_audio_call_track'"
                    )
                )
            ).scalar()
        assert kept == 30, "workspaces that already existed get the 30-day default"
        assert version == _script_head(), "upgrade head lands on the newest revision"
        assert unique == 1, "one recording per side of a call"

        _alembic(url, "downgrade", PARENT)

        assert await table_and_column() == (False, False)
        async with engine.connect() as connection:
            workspaces = (
                await connection.execute(text("SELECT count(*) FROM workspaces"))
            ).scalar()
        assert workspaces == 1, "downgrading does not lose workspaces"

        _alembic(url, "upgrade", "head")
        assert await table_and_column() == (True, True)
    finally:
        await engine.dispose()
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
