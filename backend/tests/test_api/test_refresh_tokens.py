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
from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa

from app.core.audit import AuditAction
from app.core.config import settings
from app.models.audit_event import AuditEvent
from app.models.refresh_token import RefreshToken
from app.models.user import User

if TYPE_CHECKING:
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


async def _age_used_tokens(lc: Lifecycle, seconds: float) -> None:
    """Make every retired token look like it was rotated ``seconds`` ago."""
    async with lc.sessions() as session:
        await session.execute(
            sa.update(RefreshToken)
            .where(RefreshToken.used_at.is_not(None))
            .values(used_at=datetime.now(UTC) - timedelta(seconds=seconds))
        )
        await session.commit()


async def _reuse_events(lc: Lifecycle) -> list[AuditEvent]:
    async with lc.sessions() as session:
        return list(
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
    await _age_used_tokens(lc, settings.REFRESH_ROTATION_GRACE_SECONDS + 1)

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
    async with lc.sessions() as session:
        events = (await session.execute(sa.select(AuditEvent))).scalars().all()
    assert AuditAction.LOGOUT not in [e.action for e in events], "a token nobody holds is no event"


@pytest.mark.asyncio
async def test_logout_is_written_to_the_audit_trail(lifecycle: Lifecycle) -> None:
    lc = lifecycle
    tokens = await lc.login(lc.alice.user.email)

    out = await lc.client.post(LOGOUT, json={"refresh_token": tokens["refresh_token"]})
    again = await lc.client.post(LOGOUT, json={"refresh_token": tokens["refresh_token"]})

    assert (out.status_code, again.status_code) == (204, 204)
    async with lc.sessions() as session:
        events = [
            e
            for e in (await session.execute(sa.select(AuditEvent))).scalars().all()
            if e.action == AuditAction.LOGOUT
        ]
    assert len(events) == 1, "the second logout of the same sign-in changes nothing"
    assert events[0].actor_user_id == lc.alice.user.id
    assert "refresh_token" not in repr(events[0].details)
    assert tokens["refresh_token"] not in repr(events[0].details)


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
    assert still_refused.status_code == 401, "disabling ends the sign-ins it found"
    # The short-lived access token is not revoked by a disable: it is refused while
    # the account is off, and works again for the rest of its life when switched back on
    # (test_an_account_can_be_switched_back_on). A password reset revokes it.
    assert await _me(lc, tokens["access_token"]) == 200


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

    lc = lifecycle
    tokens = await lc.login(lc.alice.user.email)

    refreshed = (await _refresh(lc, tokens["refresh_token"])).json()

    claims = jwt.decode(refreshed["access_token"], settings.SECRET_KEY, algorithms=["HS256"])
    remaining = claims["exp"] - datetime.now(UTC).timestamp()
    assert (
        settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60 - 60
        < remaining
        <= (settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60)
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

    assert sorted([first.status_code, second.status_code]) == [200, 409]
    loser = first if first.status_code == 409 else second
    winner = first if first.status_code == 200 else second
    assert set(loser.json()) == {"detail"}, "no tokens in the refusal"
    assert len(await _rows(lc)) == 2, "one successor, not two"
    assert (await _refresh(lc, winner.json()["refresh_token"])).status_code == 200
    assert not await _reuse_events(lc), "a race between a person's own tabs is not theft"


@pytest.mark.asyncio
async def test_a_token_replayed_within_the_window_gets_nothing_and_revokes_nothing(
    lifecycle: Lifecycle,
) -> None:
    lc = lifecycle
    first = await lc.login(lc.alice.user.email)
    second = (await _refresh(lc, first["refresh_token"])).json()

    # A second tab whose storage read was stale presents the token again.
    replay = await _refresh(lc, first["refresh_token"])

    assert replay.status_code == 409
    assert set(replay.json()) == {"detail"}, "no access token and no refresh token"
    assert len(await _rows(lc)) == 2, "nothing was issued"
    assert not await _reuse_events(lc)
    assert (await _refresh(lc, second["refresh_token"])).status_code == 200, "family still live"


async def _race_events(lc: Lifecycle) -> list[AuditEvent]:
    async with lc.sessions() as session:
        return list(
            (
                await session.execute(
                    sa.select(AuditEvent).where(AuditEvent.action == "auth.refresh.rotation_race")
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_each_replay_in_the_window_leaves_one_audit_row_without_token_values(
    lifecycle: Lifecycle,
) -> None:
    lc = lifecycle
    first = await lc.login(lc.alice.user.email)
    second = (await _refresh(lc, first["refresh_token"])).json()
    assert not await _race_events(lc), "a normal rotation is not a race"

    assert (await _refresh(lc, first["refresh_token"])).status_code == 409
    events = await _race_events(lc)
    assert len(events) == 1
    event = events[0]
    assert event.success is False
    assert event.actor_user_id == lc.alice.user.id
    assert event.target_type == "refresh_token_family"
    details = event.details or {}
    assert isinstance(details.get("age_ms"), int)
    assert first["refresh_token"] not in str(details) + str(event.target_id)
    assert second["refresh_token"] not in str(details) + str(event.target_id)

    assert (await _refresh(lc, first["refresh_token"])).status_code == 409
    assert len(await _race_events(lc)) == 2, "one row per replay"


@pytest.mark.asyncio
async def test_presenting_the_old_token_again_and_again_in_the_window_changes_nothing(
    lifecycle: Lifecycle,
) -> None:
    lc = lifecycle
    first = await lc.login(lc.alice.user.email)
    await _refresh(lc, first["refresh_token"])
    used_before = sorted(
        row.used_at.isoformat() for row in await _rows(lc) if row.used_at is not None
    )

    answers = [(await _refresh(lc, first["refresh_token"])).status_code for _ in range(5)]

    assert answers == [409] * 5
    assert len(await _rows(lc)) == 2
    assert used_before == sorted(
        row.used_at.isoformat() for row in await _rows(lc) if row.used_at is not None
    ), "the window does not slide: it ends the same moment however often it is tried"
    await _age_used_tokens(lc, settings.REFRESH_ROTATION_GRACE_SECONDS + 1)
    assert (await _refresh(lc, first["refresh_token"])).status_code == 401
    assert len(await _reuse_events(lc)) == 1


@pytest.mark.asyncio
async def test_a_thief_who_rotated_first_is_caught_when_the_real_owner_comes_back(
    lifecycle: Lifecycle,
) -> None:
    lc = lifecycle
    original = (await lc.login(lc.alice.user.email))["refresh_token"]
    thief_pair = (await _refresh(lc, original)).json()  # the thief rotates first

    inside = await _refresh(lc, original)  # the owner, seconds later: told to look again
    await _age_used_tokens(lc, settings.REFRESH_ROTATION_GRACE_SECONDS + 1)
    outside = await _refresh(lc, original)  # the owner, still holding the old one

    assert inside.status_code == 409
    assert outside.status_code == 401
    assert (await _refresh(lc, thief_pair["refresh_token"])).status_code == 401, (
        "the thief's copy dies with the family"
    )
    assert len(await _reuse_events(lc)) == 1


@pytest.mark.asyncio
async def test_a_disabled_user_gets_no_window(lifecycle: Lifecycle) -> None:
    lc = lifecycle
    tokens = await lc.login(lc.bob.user.email)
    await _refresh(lc, tokens["refresh_token"])
    async with lc.sessions() as session:
        user = (
            await session.execute(sa.select(User).where(User.id == lc.bob.user.id))
        ).scalar_one()
        user.is_active = False
        await session.commit()

    assert (await _refresh(lc, tokens["refresh_token"])).status_code == 401
    assert all(row.revoked_at is not None for row in await _rows(lc))


@pytest.mark.asyncio
async def test_a_revoked_family_gets_no_window(lifecycle: Lifecycle) -> None:
    lc = lifecycle
    tokens = await lc.login(lc.alice.user.email)
    await _refresh(lc, tokens["refresh_token"])
    await lc.client.post(LOGOUT, json={"refresh_token": tokens["refresh_token"]})

    assert (await _refresh(lc, tokens["refresh_token"])).status_code == 401


@pytest.mark.asyncio
async def test_the_window_does_not_outlive_the_sign_in(lifecycle: Lifecycle) -> None:
    lc = lifecycle
    tokens = await lc.login(lc.alice.user.email)
    await _refresh(lc, tokens["refresh_token"])
    async with lc.sessions() as session:
        await session.execute(
            sa.update(RefreshToken).values(
                family_expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
        await session.commit()

    assert (await _refresh(lc, tokens["refresh_token"])).status_code == 401


def test_the_rotation_window_is_bounded() -> None:
    from pydantic import ValidationError

    from app.core.config import Settings

    assert Settings(REFRESH_ROTATION_GRACE_SECONDS=0).REFRESH_ROTATION_GRACE_SECONDS == 0
    assert Settings(REFRESH_ROTATION_GRACE_SECONDS=30).REFRESH_ROTATION_GRACE_SECONDS == 30
    for bad in (-1, 31, 3600):
        with pytest.raises(ValidationError):
            Settings(REFRESH_ROTATION_GRACE_SECONDS=bad)
