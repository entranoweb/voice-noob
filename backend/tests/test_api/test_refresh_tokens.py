"""Refresh tokens: rotation, and what happens to a stolen one.

Risks, in the order they would hurt:

1. A token someone copied keeps working next to the real one, unnoticed.
2. A signed-out, disabled or reset account keeps a way back in.
3. A refresh token stored in a form that can be replayed from a database leak.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from app.core.audit import AuditAction
from app.models.audit_event import AuditEvent
from app.models.refresh_token import RefreshToken
from app.models.user import User
from tests.test_api.conftest import Lifecycle

REFRESH = "/api/v1/auth/refresh"
LOGOUT = "/api/v1/auth/logout"


async def _refresh(lc: Lifecycle, token: str) -> Any:
    return await lc.client.post(REFRESH, json={"refresh_token": token})


async def _rows(lc: Lifecycle) -> list[RefreshToken]:
    async with lc.sessions() as session:
        return list((await session.execute(sa.select(RefreshToken))).scalars().all())


async def _me(lc: Lifecycle, access_token: str) -> int:
    response = await lc.client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"}
    )
    return response.status_code


@pytest.mark.asyncio
async def test_each_refresh_issues_a_new_pair_and_retires_the_old_token(
    lifecycle: Lifecycle,
) -> None:
    lc = lifecycle
    first = await lc.login(lc.alice.user.email)

    second = (await _refresh(lc, first["refresh_token"])).json()
    third = (await _refresh(lc, second["refresh_token"])).json()

    assert len({first["refresh_token"], second["refresh_token"], third["refresh_token"]}) == 3
    assert await _me(lc, second["access_token"]) == 200
    assert await _me(lc, third["access_token"]) == 200
    rows = await _rows(lc)
    assert len(rows) == 3
    assert len({row.family_id for row in rows}) == 1, "one sign-in is one family"
    assert sum(row.used_at is not None for row in rows) == 2, "only the newest is unused"


@pytest.mark.asyncio
async def test_a_replayed_token_is_refused_and_takes_its_whole_family_with_it(
    lifecycle: Lifecycle,
) -> None:
    lc = lifecycle
    email = lc.alice.user.email
    stolen = (await lc.login(email))["refresh_token"]
    other_device = await lc.login(email)
    # The real user refreshes; the thief presents the old copy afterwards.
    current = (await _refresh(lc, stolen)).json()

    replay = await _refresh(lc, stolen)
    after_replay = await _refresh(lc, current["refresh_token"])

    assert replay.status_code == 401
    assert after_replay.status_code == 401, "the family is revoked, the real user included"
    untouched = await _refresh(lc, other_device["refresh_token"])
    assert untouched.status_code == 200, "another sign-in is another family"
    async with lc.sessions() as session:
        events = (
            (
                await session.execute(
                    sa.select(AuditEvent).where(
                        AuditEvent.action == AuditAction.REFRESH_REUSE_DETECTED
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    assert events[0].success is False
    assert events[0].actor_user_id == lc.alice.user.id
    assert stolen not in repr(events[0].details)


@pytest.mark.asyncio
async def test_tokens_are_stored_only_as_hashes(lifecycle: Lifecycle) -> None:
    lc = lifecycle
    tokens = await lc.login(lc.alice.user.email)

    (row,) = await _rows(lc)

    assert row.token_hash == hashlib.sha256(tokens["refresh_token"].encode()).hexdigest()
    assert tokens["refresh_token"] not in row.token_hash
    assert len(tokens["refresh_token"]) >= 43


@pytest.mark.asyncio
async def test_logout_revokes_the_family(lifecycle: Lifecycle) -> None:
    lc = lifecycle
    tokens = await lc.login(lc.alice.user.email)
    rotated = (await _refresh(lc, tokens["refresh_token"])).json()

    out = await lc.client.post(LOGOUT, json={"refresh_token": rotated["refresh_token"]})

    assert out.status_code == 204
    assert (await _refresh(lc, rotated["refresh_token"])).status_code == 401
    assert (await _refresh(lc, tokens["refresh_token"])).status_code == 401


@pytest.mark.asyncio
async def test_logout_with_an_unknown_token_is_quietly_accepted(lifecycle: Lifecycle) -> None:
    lc = lifecycle

    out = await lc.client.post(LOGOUT, json={"refresh_token": "x" * 43})

    assert out.status_code == 204


@pytest.mark.asyncio
async def test_a_disabled_user_cannot_refresh_even_after_being_enabled_again(
    lifecycle: Lifecycle,
) -> None:
    lc = lifecycle
    tokens = await lc.login(lc.bob.user.email)
    admin_headers = await _admin_headers(lc)

    disabled = await lc.client.post(
        f"/api/v1/auth/users/{lc.bob.user.id}/disable", headers=admin_headers
    )
    refused = await _refresh(lc, tokens["refresh_token"])
    enabled = await lc.client.post(
        f"/api/v1/auth/users/{lc.bob.user.id}/enable", headers=admin_headers
    )
    still_refused = await _refresh(lc, tokens["refresh_token"])

    assert disabled.status_code == 200
    assert refused.status_code == 401
    assert enabled.status_code == 200
    assert still_refused.status_code == 401, "disabling ends the sessions it found"
    assert await _me(lc, tokens["access_token"]) == 401


async def _admin_headers(lc: Lifecycle) -> dict[str, str]:
    tokens = await lc.login(lc.admin.email, lc.admin_password)
    return {"Authorization": f"Bearer {tokens['access_token']}"}


@pytest.mark.asyncio
async def test_an_expired_token_is_refused(lifecycle: Lifecycle) -> None:
    lc = lifecycle
    tokens = await lc.login(lc.alice.user.email)
    async with lc.sessions() as session:
        await session.execute(
            sa.update(RefreshToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()

    assert (await _refresh(lc, tokens["refresh_token"])).status_code == 401


@pytest.mark.asyncio
async def test_rotation_cannot_extend_a_sign_in_past_its_absolute_limit(
    lifecycle: Lifecycle,
) -> None:
    lc = lifecycle
    tokens = await lc.login(lc.alice.user.email)
    async with lc.sessions() as session:
        await session.execute(
            sa.update(RefreshToken).values(
                family_expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
        await session.commit()

    assert (await _refresh(lc, tokens["refresh_token"])).status_code == 401


@pytest.mark.asyncio
async def test_garbage_is_refused_without_an_error(lifecycle: Lifecycle) -> None:
    lc = lifecycle

    assert (await _refresh(lc, "not-a-token")).status_code == 401
    assert (await _refresh(lc, "")).status_code in (401, 422)


@pytest.mark.asyncio
async def test_a_new_access_token_has_the_normal_lifetime(lifecycle: Lifecycle) -> None:
    import jwt

    from app.core.config import settings

    lc = lifecycle
    tokens = await lc.login(lc.alice.user.email)

    refreshed = (await _refresh(lc, tokens["refresh_token"])).json()

    claims = jwt.decode(refreshed["access_token"], settings.SECRET_KEY, algorithms=["HS256"])
    remaining = claims["exp"] - datetime.now(UTC).timestamp()
    assert settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60 - 60 < remaining <= (
        settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    )


@pytest.mark.asyncio
async def test_two_requests_with_one_token_at_once_let_only_one_through(
    lifecycle: Lifecycle,
) -> None:
    lc = lifecycle
    tokens = await lc.login(lc.alice.user.email)

    first, second = await asyncio.gather(
        _refresh(lc, tokens["refresh_token"]), _refresh(lc, tokens["refresh_token"])
    )

    assert sorted([first.status_code, second.status_code]) == [200, 401]
    async with lc.sessions() as session:
        user = (
            await session.execute(sa.select(User).where(User.id == lc.alice.user.id))
        ).scalar_one()
    assert user.is_active
    assert all(row.revoked_at is not None for row in await _rows(lc)), (
        "the loser of the race is a replay: the family is revoked"
    )
