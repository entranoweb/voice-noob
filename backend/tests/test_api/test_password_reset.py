"""Password reset by emailed link.

Risks, in the order they would hurt:

1. A link that works twice, after it expired, or after a newer one was issued.
2. A reset that leaves the old sessions alive (the person who stole the password
   keeps their access).
3. The request endpoint telling a stranger which emails have accounts.
4. A disabled account getting back in through a reset.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from app.core.audit import AuditAction
from app.core.limiter import limiter
from app.models.audit_event import AuditEvent
from app.models.password_reset_token import PasswordResetToken
from app.models.user import User
from tests.test_api.conftest import Lifecycle

FORGOT = "/api/v1/auth/forgot-password"
RESET = "/api/v1/auth/reset-password"
NEW_PASSWORD = "a-brand-new-passphrase"  # noqa: S105 - a test fixture


async def _rows(lc: Lifecycle, model: Any) -> list[Any]:
    async with lc.sessions() as session:
        return list((await session.execute(sa.select(model))).scalars().all())


async def _actions(lc: Lifecycle) -> list[str]:
    events = await _rows(lc, AuditEvent)
    return [event.action for event in sorted(events, key=lambda e: e.created_at)]


async def _forgot(lc: Lifecycle, email: str) -> Any:
    return await lc.client.post(FORGOT, json={"email": email})


async def _reset(lc: Lifecycle, token: str, password: str = NEW_PASSWORD) -> Any:
    return await lc.client.post(RESET, json={"token": token, "new_password": password})


@pytest.mark.asyncio
async def test_happy_path_emails_a_link_and_the_new_password_signs_in(lifecycle: Lifecycle) -> None:
    lc = lifecycle
    email = lc.alice.user.email

    requested = await _forgot(lc, email)

    assert requested.status_code == 202
    assert [mail.to for mail in lc.mailer.outbox] == [email]
    token = lc.reset_token_from_mail()
    assert len(token) >= 43, "32 random bytes encode to at least 43 characters"

    done = await _reset(lc, token)

    assert done.status_code == 200, done.text
    signed_in = await lc.client.post(
        "/api/v1/auth/login", data={"username": email, "password": NEW_PASSWORD}
    )
    old = await lc.client.post(
        "/api/v1/auth/login", data={"username": email, "password": lc.alice.password}
    )
    assert signed_in.status_code == 200
    assert old.status_code == 401
    # A reset does not sign anybody in by itself.
    assert "access_token" not in done.json()
    actions = await _actions(lc)
    assert AuditAction.PASSWORD_RESET_REQUESTED in actions
    assert AuditAction.PASSWORD_RESET_COMPLETED in actions


@pytest.mark.asyncio
async def test_only_a_hash_of_the_link_is_stored(lifecycle: Lifecycle) -> None:
    lc = lifecycle
    await _forgot(lc, lc.alice.user.email)
    token = lc.reset_token_from_mail()

    rows = await _rows(lc, PasswordResetToken)

    assert len(rows) == 1
    assert token not in rows[0].token_hash
    assert len(rows[0].token_hash) == 64
    events = await _rows(lc, AuditEvent)
    assert token not in repr([event.details for event in events])


@pytest.mark.asyncio
async def test_the_link_lives_thirty_minutes(lifecycle: Lifecycle) -> None:
    lc = lifecycle
    await _forgot(lc, lc.alice.user.email)

    (row,) = await _rows(lc, PasswordResetToken)

    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
    created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=UTC)
    assert expires - created == timedelta(minutes=30)


@pytest.mark.asyncio
async def test_an_expired_link_is_refused_and_the_password_is_unchanged(
    lifecycle: Lifecycle,
) -> None:
    lc = lifecycle
    await _forgot(lc, lc.alice.user.email)
    token = lc.reset_token_from_mail()
    async with lc.sessions() as session:
        await session.execute(
            sa.update(PasswordResetToken).values(
                expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
        await session.commit()

    refused = await _reset(lc, token)

    assert refused.status_code == 400
    still = await lc.client.post(
        "/api/v1/auth/login",
        data={"username": lc.alice.user.email, "password": lc.alice.password},
    )
    assert still.status_code == 200


@pytest.mark.asyncio
async def test_a_link_works_once(lifecycle: Lifecycle) -> None:
    lc = lifecycle
    await _forgot(lc, lc.alice.user.email)
    token = lc.reset_token_from_mail()

    first = await _reset(lc, token)
    second = await _reset(lc, token, "yet-another-passphrase")

    assert first.status_code == 200
    assert second.status_code == 400
    signed_in = await lc.client.post(
        "/api/v1/auth/login",
        data={"username": lc.alice.user.email, "password": NEW_PASSWORD},
    )
    assert signed_in.status_code == 200, "the second attempt must not have changed the password"


@pytest.mark.asyncio
async def test_a_new_link_cancels_the_older_one(lifecycle: Lifecycle) -> None:
    lc = lifecycle
    await _forgot(lc, lc.alice.user.email)
    older = lc.reset_token_from_mail()
    await _forgot(lc, lc.alice.user.email)
    newer = lc.reset_token_from_mail()
    assert older != newer

    with_older = await _reset(lc, older)
    with_newer = await _reset(lc, newer)

    assert with_older.status_code == 400
    assert with_newer.status_code == 200


@pytest.mark.asyncio
async def test_a_reset_ends_every_session_and_refresh_token(lifecycle: Lifecycle) -> None:
    lc = lifecycle
    email = lc.alice.user.email
    first = await lc.login(email)
    second = await lc.login(email)
    headers = {"Authorization": f"Bearer {first['access_token']}"}
    assert (await lc.client.get("/api/v1/auth/me", headers=headers)).status_code == 200
    await _forgot(lc, email)

    done = await _reset(lc, lc.reset_token_from_mail())

    assert done.status_code == 200
    assert (await lc.client.get("/api/v1/auth/me", headers=headers)).status_code == 401
    for tokens in (first, second):
        refreshed = await lc.client.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert refreshed.status_code == 401
    fresh = await lc.login(email, NEW_PASSWORD)
    me = await lc.client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {fresh['access_token']}"}
    )
    assert me.status_code == 200, "a sign-in after the reset must work"


@pytest.mark.asyncio
async def test_an_unknown_email_gets_the_same_answer_and_no_mail(lifecycle: Lifecycle) -> None:
    lc = lifecycle

    known = await _forgot(lc, lc.alice.user.email)
    unknown = await _forgot(lc, "nobody-here@example.com")

    assert (known.status_code, known.json()) == (unknown.status_code, unknown.json())
    assert [mail.to for mail in lc.mailer.outbox] == [lc.alice.user.email]
    assert len(await _rows(lc, PasswordResetToken)) == 1


@pytest.mark.asyncio
async def test_an_unknown_email_is_still_recorded_in_the_trail(lifecycle: Lifecycle) -> None:
    lc = lifecycle

    await _forgot(lc, "nobody-here@example.com")

    events = [e for e in await _rows(lc, AuditEvent) if e.action == AuditAction.PASSWORD_RESET_REQUESTED]
    assert len(events) == 1
    assert events[0].actor_user_id is None
    assert "nobody-here" not in repr(events[0].details), "an address nobody owns is not logged"


@pytest.mark.asyncio
async def test_a_malformed_email_is_the_only_thing_that_answers_differently(
    lifecycle: Lifecycle,
) -> None:
    lc = lifecycle

    response = await lc.client.post(FORGOT, json={"email": "not-an-email"})

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_the_request_endpoint_is_rate_limited(lifecycle: Lifecycle) -> None:
    lc = lifecycle

    statuses = [(await _forgot(lc, f"nobody{i}@example.com")).status_code for i in range(8)]

    assert statuses[:5] == [202] * 5
    assert 429 in statuses[5:]


@pytest.mark.asyncio
async def test_one_account_cannot_be_mail_bombed(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    lc = lifecycle
    monkeypatch.setattr(limiter, "enabled", False)

    for _ in range(9):
        response = await _forgot(lc, lc.alice.user.email)
        assert response.status_code == 202

    assert len(lc.mailer.outbox) == 5


@pytest.mark.asyncio
async def test_a_disabled_account_gets_no_mail_and_cannot_reset_its_way_back(
    lifecycle: Lifecycle,
) -> None:
    lc = lifecycle
    email = lc.bob.user.email
    # A link issued while the account was active ...
    await _forgot(lc, email)
    token = lc.reset_token_from_mail()
    async with lc.sessions() as session:
        user = (await session.execute(sa.select(User).where(User.email == email))).scalar_one()
        user.is_active = False
        await session.commit()

    again = await _forgot(lc, email)
    refused = await _reset(lc, token)

    assert again.status_code == 202
    assert len(lc.mailer.outbox) == 1, "no second mail for a disabled account"
    assert refused.status_code == 400
    async with lc.sessions() as session:
        user = (await session.execute(sa.select(User).where(User.email == email))).scalar_one()
    assert user.is_active is False
    login = await lc.client.post(
        "/api/v1/auth/login", data={"username": email, "password": NEW_PASSWORD}
    )
    assert login.status_code == 401


@pytest.mark.asyncio
async def test_a_weak_password_is_refused_and_does_not_use_up_the_link(
    lifecycle: Lifecycle,
) -> None:
    lc = lifecycle
    await _forgot(lc, lc.alice.user.email)
    token = lc.reset_token_from_mail()

    weak = await _reset(lc, token, "short")
    good = await _reset(lc, token)

    assert weak.status_code == 422
    assert good.status_code == 200


@pytest.mark.asyncio
async def test_a_made_up_token_is_refused(lifecycle: Lifecycle) -> None:
    lc = lifecycle

    response = await _reset(lc, "x" * 43)

    assert response.status_code == 400
