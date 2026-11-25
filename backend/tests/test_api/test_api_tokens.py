"""Personal API tokens for ``synthiq-caller upload``.

Risks, in the order they would hurt:

1. A token that opens more than the upload: account settings, creating more
   tokens, anything. It is long-lived and sits in CI secrets.
2. A token that opens somebody else's workspace.
3. A token that still works after it was revoked, expired, or its owner was
   disabled.
4. A token that can be read back from the database or from the list.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa

from app.core.audit import AuditAction
from app.main import app
from app.models.api_token import ApiToken
from app.models.audit_event import AuditEvent
from app.models.user import User
from tests.test_api.test_caller_audio import tone
from tests.test_api.test_caller_runs import _bundle

if TYPE_CHECKING:
    from tests.test_api.conftest import Lifecycle, Person

TOKENS = "/api/v1/auth/api-tokens"
RUNS = "/api/v1/caller/runs"


async def _create(lc: Lifecycle, person: Person, **body: Any) -> dict[str, Any]:
    headers = await lc.login_headers(person)
    response = await lc.client.post(TOKENS, json={"name": "ci upload", **body}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _upload(lc: Lifecycle, token: str, workspace_id: Any, run_id: str = "run-a") -> Any:
    return await lc.client.post(
        RUNS,
        params={"workspace_id": str(workspace_id)},
        content=json.dumps(_bundle(run_id)).encode(),
        headers={**_bearer(token), "Content-Type": "application/json"},
    )


async def _token_row(lc: Lifecycle) -> ApiToken:
    async with lc.sessions() as session:
        return (await session.execute(sa.select(ApiToken))).scalars().one()


class TestCreateListRevoke:
    @pytest.mark.asyncio
    async def test_the_token_is_shown_once_with_a_prefix_and_stored_only_hashed(
        self, lifecycle: Lifecycle
    ) -> None:
        lc = lifecycle

        created = await _create(lc, lc.alice)

        token = created["token"]
        assert token.startswith("sqt_")
        assert len(token) >= 43 + 4
        assert token.startswith(created["token_prefix"])
        assert len(created["token_prefix"]) < len(token) // 2
        row = await _token_row(lc)
        assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert token not in (row.token_hash, row.token_prefix, row.name)
        assert row.user_id == lc.alice.user.id

        headers = await lc.login_headers(lc.alice)
        listed = (await lc.client.get(TOKENS, headers=headers)).json()
        assert [item["id"] for item in listed] == [created["id"]]
        assert token not in json.dumps(listed)
        assert listed[0]["token_prefix"] == created["token_prefix"]
        assert listed[0]["last_used_at"] is None
        assert listed[0]["revoked_at"] is None

    @pytest.mark.asyncio
    async def test_tokens_are_private_to_their_owner(self, lifecycle: Lifecycle) -> None:
        lc = lifecycle
        created = await _create(lc, lc.alice)
        bob_headers = await lc.login_headers(lc.bob)

        listed = await lc.client.get(TOKENS, headers=bob_headers)
        revoked = await lc.client.delete(f"{TOKENS}/{created['id']}", headers=bob_headers)

        assert listed.json() == []
        assert revoked.status_code == 404
        assert (await _upload(lc, created["token"], lc.alice.workspace.id)).status_code == 201

    @pytest.mark.asyncio
    async def test_creating_and_revoking_are_in_the_audit_trail_without_the_token(
        self, lifecycle: Lifecycle
    ) -> None:
        lc = lifecycle
        created = await _create(lc, lc.alice)
        headers = await lc.login_headers(lc.alice)

        revoked = await lc.client.delete(f"{TOKENS}/{created['id']}", headers=headers)

        assert revoked.status_code == 204
        async with lc.sessions() as session:
            events = list(
                (
                    await session.execute(
                        sa.select(AuditEvent)
                        .where(AuditEvent.action.like("auth.api_token.%"))
                        .order_by(AuditEvent.created_at)
                    )
                )
                .scalars()
                .all()
            )
        assert [event.action for event in events] == [
            AuditAction.API_TOKEN_CREATE,
            AuditAction.API_TOKEN_REVOKE,
        ]
        assert all(event.target_id == created["id"] for event in events)
        assert all(event.actor_user_id == lc.alice.user.id for event in events)
        assert created["token"] not in repr([event.details for event in events])

    @pytest.mark.asyncio
    async def test_a_name_is_required_and_an_expiry_is_optional(self, lifecycle: Lifecycle) -> None:
        lc = lifecycle
        headers = await lc.login_headers(lc.alice)

        nameless = await lc.client.post(TOKENS, json={}, headers=headers)
        with_expiry = await lc.client.post(
            TOKENS, json={"name": "short", "expires_in_days": 7}, headers=headers
        )

        assert nameless.status_code == 422
        assert with_expiry.status_code == 201
        assert with_expiry.json()["expires_at"] is not None

    @pytest.mark.asyncio
    async def test_creating_needs_a_signed_in_user(self, lifecycle: Lifecycle) -> None:
        lc = lifecycle

        response = await lc.client.post(TOKENS, json={"name": "x"})

        assert response.status_code in (401, 403)


class TestWhatTheTokenCanDo:
    @pytest.mark.asyncio
    async def test_it_uploads_a_run_and_its_audio_and_records_when_it_was_used(
        self, lifecycle: Lifecycle
    ) -> None:
        lc = lifecycle
        created = await _create(lc, lc.alice)

        stored = await _upload(lc, created["token"], lc.alice.workspace.id)

        assert stored.status_code == 201, stored.text
        run_pk = stored.json()["run"]["id"]
        audio = await lc.client.put(
            f"{RUNS}/{run_pk}/audio",
            params={"file": "run-001.json", "track": "agent"},
            content=tone(),
            headers={**_bearer(created["token"]), "Content-Type": "audio/wav"},
        )
        assert audio.status_code == 200, audio.text
        row = await _token_row(lc)
        assert row.last_used_at is not None

    @pytest.mark.asyncio
    async def test_it_cannot_reach_a_workspace_the_user_does_not_own(
        self, lifecycle: Lifecycle
    ) -> None:
        lc = lifecycle
        created = await _create(lc, lc.alice)

        response = await _upload(lc, created["token"], lc.bob.workspace.id)

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_it_cannot_read_what_it_uploaded(self, lifecycle: Lifecycle) -> None:
        lc = lifecycle
        created = await _create(lc, lc.alice)
        stored = await _upload(lc, created["token"], lc.alice.workspace.id)
        run_pk = stored.json()["run"]["id"]

        listing = await lc.client.get(RUNS, headers=_bearer(created["token"]))
        detail = await lc.client.get(f"{RUNS}/{run_pk}", headers=_bearer(created["token"]))
        delete = await lc.client.delete(f"{RUNS}/{run_pk}", headers=_bearer(created["token"]))

        assert (listing.status_code, detail.status_code, delete.status_code) == (401, 401, 401)

    @pytest.mark.asyncio
    async def test_it_cannot_create_more_tokens_list_or_revoke_them(
        self, lifecycle: Lifecycle
    ) -> None:
        lc = lifecycle
        created = await _create(lc, lc.alice)
        headers = _bearer(created["token"])

        made = await lc.client.post(TOKENS, json={"name": "more"}, headers=headers)
        listed = await lc.client.get(TOKENS, headers=headers)
        revoked = await lc.client.delete(f"{TOKENS}/{created['id']}", headers=headers)

        assert (made.status_code, listed.status_code, revoked.status_code) == (401, 401, 401)

    @pytest.mark.asyncio
    async def test_it_cannot_change_account_settings_or_administer(
        self, lifecycle: Lifecycle
    ) -> None:
        lc = lifecycle
        created = await _create(lc, lc.alice)
        headers = _bearer(created["token"])

        me = await lc.client.get("/api/v1/auth/me", headers=headers)
        settings_read = await lc.client.get("/api/v1/settings", headers=headers)
        settings_write = await lc.client.post(
            "/api/v1/settings", json={"openai_api_key": "x"}, headers=headers
        )
        new_user = await lc.client.post(
            "/api/v1/auth/users",
            json={"email": "x@example.com", "username": "x", "password": "long-enough-1"},
            headers=headers,
        )
        disable = await lc.client.post(
            f"/api/v1/auth/users/{lc.bob.user.id}/disable", headers=headers
        )

        assert [r.status_code for r in (me, settings_read, settings_write, new_user, disable)] == [
            401
        ] * 5

    @pytest.mark.asyncio
    async def test_no_route_that_needs_a_signed_in_user_accepts_it(
        self, lifecycle: Lifecycle
    ) -> None:
        """Every route behind ``get_current_user`` refuses the token, not just the ones named above.

        The upload routes are the only ones that opt in to it. This walks the
        whole route table, so a route added next month is covered too.
        """
        lc = lifecycle
        created = await _create(lc, lc.alice)
        allowed = {(RUNS, "post"), (f"{RUNS}/{{run_pk}}/audio", "put")}
        checked = 0
        # The OpenAPI document lists every route that sits behind the bearer scheme,
        # however deep in its dependencies, and does not depend on how FastAPI
        # stores included routers.
        for path_template, operations in app.openapi()["paths"].items():
            for method, operation in operations.items():
                if "security" not in operation or (path_template, method) in allowed:
                    continue
                path = re.sub(r"\{[^}]+\}", "1", path_template)
                response = await lc.client.request(
                    method.upper(), path, headers=_bearer(created["token"]), content=b""
                )
                assert response.status_code == 401, (
                    f"{method.upper()} {path_template}: {response.status_code}"
                )
                checked += 1
        assert checked > 50, "the sweep found suspiciously few routes"
        assert RUNS in app.openapi()["paths"]


class TestWhenTheTokenStopsWorking:
    @pytest.mark.asyncio
    async def test_a_revoked_token_is_refused(self, lifecycle: Lifecycle) -> None:
        lc = lifecycle
        created = await _create(lc, lc.alice)
        headers = await lc.login_headers(lc.alice)
        assert (await _upload(lc, created["token"], lc.alice.workspace.id)).status_code == 201

        await lc.client.delete(f"{TOKENS}/{created['id']}", headers=headers)

        refused = await _upload(lc, created["token"], lc.alice.workspace.id, "run-b")
        assert refused.status_code == 401

    @pytest.mark.asyncio
    async def test_an_expired_token_is_refused(self, lifecycle: Lifecycle) -> None:
        lc = lifecycle
        created = await _create(lc, lc.alice, expires_in_days=1)
        async with lc.sessions() as session:
            await session.execute(
                sa.update(ApiToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()

        assert (await _upload(lc, created["token"], lc.alice.workspace.id)).status_code == 401

    @pytest.mark.asyncio
    async def test_a_disabled_user_is_refused(self, lifecycle: Lifecycle) -> None:
        lc = lifecycle
        created = await _create(lc, lc.alice)
        async with lc.sessions() as session:
            await session.execute(
                sa.update(User).where(User.id == lc.alice.user.id).values(is_active=False)
            )
            await session.commit()

        assert (await _upload(lc, created["token"], lc.alice.workspace.id)).status_code == 401

    @pytest.mark.asyncio
    async def test_a_guessed_or_altered_token_is_refused(self, lifecycle: Lifecycle) -> None:
        lc = lifecycle
        created = await _create(lc, lc.alice)
        altered = created["token"][:-1] + ("A" if created["token"][-1] != "A" else "B")

        for token in (altered, "sqt_" + "x" * 43, "sqt_", "sqt_a b"):
            response = await _upload(lc, token, lc.alice.workspace.id)
            assert response.status_code == 401, token

    @pytest.mark.asyncio
    async def test_a_password_reset_revokes_personal_tokens_too(self, lifecycle: Lifecycle) -> None:
        """Whoever had the password may have made a token; a reset must end that too.

        The cost is that a CI job needs a new token after a reset. That is the
        safe side of the trade, and the reviewer guide says so.
        """
        lc = lifecycle
        created = await _create(lc, lc.alice)
        assert (await _upload(lc, created["token"], lc.alice.workspace.id)).status_code == 201
        await lc.client.post("/api/v1/auth/forgot-password", json={"email": lc.alice.user.email})

        done = await lc.client.post(
            "/api/v1/auth/reset-password",
            json={"token": lc.reset_token_from_mail(), "new_password": "a-brand-new-passphrase"},
        )

        assert done.status_code == 200
        refused = await _upload(lc, created["token"], lc.alice.workspace.id, "run-b")
        assert refused.status_code == 401
