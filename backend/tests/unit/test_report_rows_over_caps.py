"""The over-cap report lists ids and counts, never content, and writes nothing."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.core.limits import PROMPT_MAX_CHARS
from app.models.agent import Agent

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import report_rows_over_caps as report

pytestmark = pytest.mark.asyncio


async def test_it_lists_only_rows_over_a_cap_by_id_without_content(
    authenticated_test_client: Any, test_session: Any
) -> None:
    _, user = authenticated_test_client
    secret = "SECRET-PROMPT-TEXT"
    over = Agent(
        id=uuid.uuid4(),
        user_id=user.id,
        name="over",
        system_prompt=secret + "p" * PROMPT_MAX_CHARS,
        pricing_tier="balanced",
        enabled_tools=[],
    )
    within = Agent(
        id=uuid.uuid4(),
        user_id=user.id,
        name="within",
        system_prompt="p" * PROMPT_MAX_CHARS,
        pricing_tier="balanced",
        enabled_tools=[],
    )
    test_session.add_all([over, within])
    await test_session.commit()

    found = await report.find_over_cap(test_session)
    text = report.render(found)

    assert found["agents.system_prompt"] == [str(over.id)]
    assert str(within.id) not in text
    assert secret not in text
    assert "agents.system_prompt (cap 50000): 1 over" in text


def test_every_capped_free_text_field_is_in_the_report() -> None:
    labels = {label for label, *_ in report.FIELDS}
    assert {"phone_numbers.notes", "contacts.notes", "agents.system_prompt"} <= labels


async def test_a_workspace_with_an_over_long_qa_model_name_is_listed_by_id(
    authenticated_test_client: Any, test_session: Any, test_workspace: Any
) -> None:
    from app.models.workspace import Workspace

    workspace = await test_session.get(Workspace, test_workspace.id)
    workspace.settings = {"qa": {"evaluation_model": "SECRET-MODEL" + "m" * 150}}
    await test_session.commit()

    found = await report.find_over_cap(test_session)
    text = report.render(found)

    assert found[report.QA_MODEL_LABEL] == [str(test_workspace.id)]
    assert "SECRET-MODEL" not in text
