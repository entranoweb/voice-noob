"""Health endpoints answer without a login; their bodies must not echo errors."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import DBAPIError

from app.db.session import get_db
from app.main import app

PARAMS = ("Pat Example", "+15550100042")
LEAKS = (
    "Pat Example",
    "15550100042",
    "INSERT INTO",
    "[parameters",
    "[SQL",
    "redis.internal.invalid",
)


def _db_error() -> Exception:
    return DBAPIError("INSERT INTO contacts (name, phone) VALUES ($1, $2)", PARAMS, Exception("x"))


async def _get(path: str, *, db_fails: bool = True, redis_fails: bool = False) -> Any:
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_db_error() if db_fails else None)

    class Redis:
        async def ping(self) -> None:
            if redis_fails:
                raise ConnectionError("Error connecting to redis.internal.invalid:6379")

    async def override_db() -> Any:
        yield session

    async def override_redis() -> Redis:
        return Redis()

    from app.api import health
    from app.db.redis import get_redis

    saved = health.get_redis
    health.get_redis = override_redis  # type: ignore[assignment]
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_redis] = override_redis
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://voice.example.com"
        ) as client:
            return await client.get(path)
    finally:
        health.get_redis = saved
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_redis, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/health/db", "/health/ready", "/health/detailed"])
async def test_database_failure_body_has_no_sql_or_parameters(path: str) -> None:
    response = await _get(path)

    assert response.status_code == 503
    for leak in LEAKS:
        assert leak not in response.text
    assert "database" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/health/redis", "/health/ready", "/health/detailed"])
async def test_redis_failure_body_has_no_hostname(path: str) -> None:
    response = await _get(path, db_fails=False, redis_fails=True)

    assert response.status_code == 503
    assert "redis.internal.invalid" not in response.text
    assert "redis" in response.text
