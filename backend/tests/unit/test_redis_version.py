"""The embed key limiter uses ``EXPIRE ... NX`` (Redis 7.0+): older Redis must stop startup."""

from __future__ import annotations

from typing import Any

import pytest

from app.db.redis import (
    RedisVersionError,
    parse_redis_version,
    require_min_redis_version,
)


class _FakeRedis:
    def __init__(self, version: str) -> None:
        self.version = version

    async def info(self, section: str) -> dict[str, Any]:
        assert section == "server"
        return {"redis_version": self.version}

    async def ping(self) -> bool:
        return True


@pytest.mark.parametrize(
    ("text", "expected"),
    [("7.2.4", (7, 2, 4)), ("7.0-rc1", (7, 0)), ("6.2.14", (6, 2, 14)), ("", ())],
)
def test_versions_parse(text: str, expected: tuple[int, ...]) -> None:
    assert parse_redis_version(text) == expected


@pytest.mark.asyncio
async def test_redis_6_2_is_refused_with_the_requirement_named() -> None:
    with pytest.raises(RedisVersionError, match=r"7\.0 or newer"):
        await require_min_redis_version(_FakeRedis("6.2.14"))


@pytest.mark.asyncio
async def test_an_unreadable_version_is_refused() -> None:
    with pytest.raises(RedisVersionError):
        await require_min_redis_version(_FakeRedis(""))


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["7.0.0", "7.2.4", "8.0.1"])
async def test_redis_7_and_newer_pass(version: str) -> None:
    assert await require_min_redis_version(_FakeRedis(version)) == version


@pytest.mark.asyncio
async def test_the_app_refuses_to_start_on_redis_6_2(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import main

    async def old_redis() -> _FakeRedis:
        return _FakeRedis("6.2.14")

    monkeypatch.setattr(main, "get_redis", old_redis)

    with pytest.raises(RedisVersionError, match=r"7\.0 or newer"):
        async with main.lifespan(main.app):
            pytest.fail("the app started on Redis 6.2")
