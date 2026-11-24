"""Customer credentials are ciphertext in the database and never leave the API."""

from collections.abc import Generator
from typing import Any

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.auth import user_id_to_uuid
from app.core.credential_store import count_by_key, encrypt_to_active_key
from app.core.secret_box import (
    PREFIX,
    CredentialDecryptError,
    SecretBox,
    reset_box,
    use_box,
)
from app.models.user import User
from app.models.user_integration import UserIntegration
from app.models.user_settings import UserSettings

OPENAI_KEY = "sk-live-openai-customer-key-AAAA1111"
TWILIO_TOKEN = "twilio-auth-token-customer-BBBB2222"
HUBSPOT_TOKEN = "hubspot-private-app-token-CCCC3333"
REFRESH_TOKEN = "oauth-refresh-token-customer-DDDD4444"

KEY_ONE = b"1" * 32
KEY_TWO = b"2" * 32


@pytest.fixture(autouse=True)
def _restore_box() -> Generator[None, None, None]:
    yield
    reset_box()


def _sessions(engine: Any) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _raw(engine: Any, statement: str) -> list[Any]:
    async with engine.connect() as connection:
        return list((await connection.execute(sa.text(statement))).all())


@pytest.mark.asyncio
async def test_provider_keys_are_ciphertext_at_rest_and_masked_in_responses(
    authenticated_test_client: tuple[AsyncClient, User], test_engine: Any
) -> None:
    client, user = authenticated_test_client

    saved = await client.post(
        "/api/v1/settings",
        json={
            "openai_api_key": OPENAI_KEY,
            "twilio_auth_token": TWILIO_TOKEN,
            "twilio_account_sid": "AC" + "0" * 32,
        },
    )
    assert saved.status_code == 200
    assert OPENAI_KEY not in saved.text

    rows = await _raw(
        test_engine,
        "SELECT openai_api_key, twilio_auth_token, twilio_account_sid FROM user_settings",
    )
    assert len(rows) == 1
    stored_openai, stored_twilio, stored_sid = rows[0]
    assert stored_openai.startswith(PREFIX)
    assert stored_twilio.startswith(PREFIX)
    assert OPENAI_KEY not in stored_openai
    assert TWILIO_TOKEN not in stored_twilio
    # An identifier, not a secret: stays readable.
    assert stored_sid == "AC" + "0" * 32

    # The application still reads the real value.
    async with _sessions(test_engine)() as session:
        loaded = (
            await session.execute(
                sa.select(UserSettings).where(UserSettings.user_id == user_id_to_uuid(user.id))
            )
        ).scalar_one()
        assert loaded.openai_api_key == OPENAI_KEY
        assert loaded.twilio_auth_token == TWILIO_TOKEN

    shown = await client.get("/api/v1/settings")
    assert shown.status_code == 200
    assert shown.json()["openai_api_key_set"] is True
    assert OPENAI_KEY not in shown.text
    assert TWILIO_TOKEN not in shown.text


@pytest.mark.asyncio
async def test_integration_credentials_are_ciphertext_and_updates_persist(
    authenticated_test_client: tuple[AsyncClient, User], test_engine: Any
) -> None:
    client, user = authenticated_test_client

    connected = await client.post(
        "/api/v1/integrations",
        json={
            "integration_id": "hubspot",
            "integration_name": "HubSpot",
            "credentials": {"access_token": HUBSPOT_TOKEN},
        },
    )
    assert connected.status_code == 201
    assert HUBSPOT_TOKEN not in connected.text
    assert connected.json()["credential_fields"] == ["access_token"]

    stored = (await _raw(test_engine, "SELECT credentials FROM user_integrations"))[0][0]
    assert HUBSPOT_TOKEN not in str(stored)
    assert PREFIX in str(stored)

    # A partial update merges with what is stored and is actually saved.
    updated = await client.put(
        "/api/v1/integrations/hubspot", json={"credentials": {"portal_id": "12345"}}
    )
    assert updated.status_code == 200
    assert HUBSPOT_TOKEN not in updated.text
    assert sorted(updated.json()["credential_fields"]) == ["access_token", "portal_id"]

    async with _sessions(test_engine)() as session:
        loaded = (
            await session.execute(
                sa.select(UserIntegration).where(
                    UserIntegration.user_id == user_id_to_uuid(user.id)
                )
            )
        ).scalar_one()
        assert loaded.credentials == {"access_token": HUBSPOT_TOKEN, "portal_id": "12345"}

    listed = await client.get("/api/v1/integrations")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert HUBSPOT_TOKEN not in listed.text


async def _store_plain(engine: Any) -> None:
    """Rows as an installation older than encryption left them."""
    use_box(None)
    async with _sessions(engine)() as session:
        owner = user_id_to_uuid(7)
        session.add(UserSettings(user_id=owner, openai_api_key=OPENAI_KEY))
        session.add(
            UserIntegration(
                user_id=owner,
                integration_id="hubspot",
                integration_name="HubSpot",
                credentials={"access_token": HUBSPOT_TOKEN},
                refresh_token=REFRESH_TOKEN,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_existing_plain_rows_are_encrypted_in_place_and_rerun_is_a_no_op(
    test_engine: Any,
) -> None:
    await _store_plain(test_engine)
    assert (await _raw(test_engine, "SELECT openai_api_key FROM user_settings"))[0][0] == OPENAI_KEY

    use_box(SecretBox([("k1", KEY_ONE)]))
    async with test_engine.begin() as connection:
        first = await connection.run_sync(encrypt_to_active_key)
    async with test_engine.begin() as connection:
        second = await connection.run_sync(encrypt_to_active_key)
        by_key = await connection.run_sync(count_by_key)

    assert first == {"user_settings": 1, "user_integrations": 1}
    assert second == {"user_settings": 0, "user_integrations": 0}
    assert by_key == {"k1": 3}

    raw_key = (await _raw(test_engine, "SELECT openai_api_key FROM user_settings"))[0][0]
    raw_refresh, raw_credentials = (
        await _raw(test_engine, "SELECT refresh_token, credentials FROM user_integrations")
    )[0]
    assert raw_key.startswith(f"{PREFIX}k1:")
    assert raw_refresh.startswith(f"{PREFIX}k1:")
    assert HUBSPOT_TOKEN not in str(raw_credentials)

    async with _sessions(test_engine)() as session:
        settings_row = (await session.execute(sa.select(UserSettings))).scalar_one()
        integration = (await session.execute(sa.select(UserIntegration))).scalar_one()
        assert settings_row.openai_api_key == OPENAI_KEY
        assert integration.credentials == {"access_token": HUBSPOT_TOKEN}
        assert integration.refresh_token == REFRESH_TOKEN


@pytest.mark.asyncio
async def test_rotation_moves_every_value_to_the_new_key(test_engine: Any) -> None:
    await _store_plain(test_engine)
    use_box(SecretBox([("k1", KEY_ONE)]))
    async with test_engine.begin() as connection:
        await connection.run_sync(encrypt_to_active_key)

    # New key first, old key kept behind it.
    use_box(SecretBox([("k2", KEY_TWO), ("k1", KEY_ONE)]))
    async with _sessions(test_engine)() as session:
        assert (await session.execute(sa.select(UserSettings))).scalar_one().openai_api_key == (
            OPENAI_KEY
        )
    async with test_engine.begin() as connection:
        moved = await connection.run_sync(encrypt_to_active_key)
        by_key = await connection.run_sync(count_by_key)
    assert moved == {"user_settings": 1, "user_integrations": 1}
    assert by_key == {"k2": 3}

    # The old key can now be dropped.
    use_box(SecretBox([("k2", KEY_TWO)]))
    async with _sessions(test_engine)() as session:
        integration = (await session.execute(sa.select(UserIntegration))).scalar_one()
        assert integration.credentials == {"access_token": HUBSPOT_TOKEN}
        assert integration.refresh_token == REFRESH_TOKEN


@pytest.mark.asyncio
async def test_reading_with_the_wrong_key_fails_instead_of_returning_garbage(
    test_engine: Any,
) -> None:
    use_box(SecretBox([("k1", KEY_ONE)]))
    async with _sessions(test_engine)() as session:
        session.add(UserSettings(user_id=user_id_to_uuid(9), openai_api_key=OPENAI_KEY))
        await session.commit()

    use_box(SecretBox([("k1", KEY_TWO)]))
    async with _sessions(test_engine)() as session:
        with pytest.raises(CredentialDecryptError):
            (await session.execute(sa.select(UserSettings))).scalar_one()

    # And with no key at all.
    use_box(None)
    async with _sessions(test_engine)() as session:
        with pytest.raises(CredentialDecryptError):
            (await session.execute(sa.select(UserSettings))).scalar_one()
