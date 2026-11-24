"""Rate limits are one count for the deployment, and survive losing Redis.

Counts used to live in each worker's memory, so "2/minute" meant two a minute
per worker. Two limiters built the way two workers build theirs stand in for two
processes here: nothing in a ``Limiter`` is shared between instances except the
store it is pointed at.
"""

from __future__ import annotations

import os
import uuid

import pytest
import redis
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.core import limiter as limiter_module
from app.core.config import settings
from app.core.limiter import build_limiter, rate_limit_storage_uri

# Nothing listens on port 1, so a connection is refused at once.
UNREACHABLE_REDIS = "redis://127.0.0.1:1/0"
TOO_MANY = 429


def _worker(limiter: Limiter, path: str) -> TestClient:
    """One worker process: its own app and its own limiter, two requests a minute."""
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

    @app.get(path)
    @limiter.limit("2/minute")
    async def limited(request: Request) -> dict[str, bool]:
        return {"ok": True}

    return TestClient(app)


def _real_redis_uri() -> str:
    """The Redis this run can reach. CI must have one; a laptop may not."""
    uri = str(settings.REDIS_URL)
    try:
        redis.Redis.from_url(uri, socket_connect_timeout=0.5).ping()
    except redis.RedisError as exc:
        if os.getenv("CI"):
            pytest.fail(f"CI has no Redis at {uri}, so shared limits are untested: {exc}")
        pytest.skip(f"no Redis at {uri}; this runs in CI")
    return uri


class TestWhereCountsAreKept:
    def test_redis_unless_told_otherwise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "RATE_LIMIT_STORAGE_URI", None)

        assert rate_limit_storage_uri() == str(settings.REDIS_URL)
        assert rate_limit_storage_uri().startswith("redis://")

    def test_the_override_is_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "RATE_LIMIT_STORAGE_URI", "memory://")

        assert rate_limit_storage_uri() == "memory://"

    def test_the_application_limiter_is_built_from_that_setting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Left unset, a worker's limiter counts in Redis, not in its own memory."""
        monkeypatch.setattr(settings, "RATE_LIMIT_STORAGE_URI", None)

        storage = type(build_limiter()._storage).__name__

        assert storage == "RedisStorage"


class TestTwoWorkersShareOneCount:
    def test_the_second_worker_sees_what_the_first_one_served(self) -> None:
        uri = _real_redis_uri()
        # A path of its own, so reruns against one Redis do not meet old counts.
        path = f"/limited-{uuid.uuid4().hex}"
        first = _worker(build_limiter(uri), path)
        second = _worker(build_limiter(uri), path)

        assert first.get(path).status_code == 200
        assert second.get(path).status_code == 200
        # Two served across the deployment: the limit is spent on both workers.
        assert first.get(path).status_code == TOO_MANY
        assert second.get(path).status_code == TOO_MANY


class TestRedisIsDown:
    def test_requests_are_still_served_and_still_limited(self) -> None:
        worker = _worker(build_limiter(UNREACHABLE_REDIS), "/limited")

        statuses = [worker.get("/limited").status_code for _ in range(3)]

        # Not three 500s (the store error must not reach the caller), and not
        # three 200s (losing the store must not remove the limit).
        assert statuses == [200, 200, TOO_MANY]

    def test_an_unreachable_store_fails_fast(self) -> None:
        """The Redis client is called on the event loop; it must not hang it."""
        assert limiter_module._REDIS_TIMEOUT_SECONDS <= 0.5
        options = build_limiter(UNREACHABLE_REDIS)._storage_options

        assert options["socket_connect_timeout"] == limiter_module._REDIS_TIMEOUT_SECONDS
        assert options["socket_timeout"] == limiter_module._REDIS_TIMEOUT_SECONDS
