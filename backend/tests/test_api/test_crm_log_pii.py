"""The CRM API must not write a contact's full number to the log."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from httpx import AsyncClient

    from app.models.user import User

NUMBER = "+15550100042"


@pytest.mark.asyncio
async def test_create_contact_logs_masked_number(
    authenticated_test_client: tuple[AsyncClient, User], caplog: pytest.LogCaptureFixture
) -> None:
    client, _user = authenticated_test_client
    caplog.set_level(logging.DEBUG, logger="app")

    response = await client.post(
        "/api/v1/crm/contacts", json={"first_name": "Pat", "phone_number": NUMBER}
    )

    assert response.status_code == 201
    lines = " ".join(r.getMessage() for r in caplog.records if r.name.startswith("app"))
    assert "Created contact" in lines, "the create path logged nothing, so this proves nothing"
    assert NUMBER not in lines
    assert NUMBER.lstrip("+") not in lines
    assert NUMBER[-2:] in lines
