"""Report rows that already hold more than the size caps now allow. Read-only.

    uv run python scripts/report_rows_over_caps.py

Prints, for each capped field, how many rows are over its cap and their ids. It
never prints content and never writes. Rows listed here keep saving: an update that
sends a value unchanged is accepted, and only a changed value is held to the cap.
Nothing here is cut for you.

JSON fields are measured as the text the database holds, which is close to the
serialised size the API checks; encrypted credentials are measured as stored.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import Text, cast, func, select

from app.core import limits
from app.models.agent import Agent
from app.models.appointment import Appointment
from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.phone_number import PhoneNumber
from app.models.test_scenario import TestScenario
from app.models.user_integration import UserIntegration
from app.models.workspace import Workspace

MAX_IDS_PRINTED = 50

# Each entry: label, model, column name, cap, whether the column holds JSON.
FIELDS: list[tuple[str, Any, str, int, bool]] = [
    ("agents.system_prompt", Agent, "system_prompt", limits.PROMPT_MAX_CHARS, False),
    ("agents.description", Agent, "description", limits.DESCRIPTION_MAX_CHARS, False),
    ("agents.initial_greeting", Agent, "initial_greeting", limits.TURN_TEXT_MAX_CHARS, False),
    ("agents.embed_settings", Agent, "embed_settings", limits.JSON_MAX_CHARS, True),
    ("workspaces.settings", Workspace, "settings", limits.JSON_MAX_CHARS, True),
    ("user_integrations.credentials", UserIntegration, "credentials", limits.JSON_MAX_CHARS, True),
    (
        "user_integrations.integration_metadata",
        UserIntegration,
        "integration_metadata",
        limits.JSON_MAX_CHARS,
        True,
    ),
    ("contacts.notes", Contact, "notes", limits.NOTES_MAX_CHARS, False),
    ("phone_numbers.notes", PhoneNumber, "notes", limits.NOTES_MAX_CHARS, False),
    ("appointments.notes", Appointment, "notes", limits.NOTES_MAX_CHARS, False),
    ("campaigns.description", Campaign, "description", limits.DESCRIPTION_MAX_CHARS, False),
    (
        "campaign_contacts.disposition_notes",
        CampaignContact,
        "disposition_notes",
        limits.NOTES_MAX_CHARS,
        False,
    ),
    (
        "test_scenarios.description",
        TestScenario,
        "description",
        limits.DESCRIPTION_MAX_CHARS,
        False,
    ),
]


QA_MODEL_LABEL = "workspaces.settings.qa.evaluation_model"


async def find_over_cap(db: Any) -> dict[str, list[str]]:
    """Map each field's label to the ids (as text) of the rows over its cap."""
    found: dict[str, list[str]] = {}
    for label, model, column, cap, is_json in FIELDS:
        col = getattr(model, column)
        measured = func.char_length(cast(col, Text) if is_json else col)
        rows = await db.execute(select(model.id).where(measured > cap).order_by(model.id))
        found[label] = [str(row_id) for row_id in rows.scalars().all()]
    # Inside a JSON document, so it is read in Python rather than by SQL.
    from app.api.qa import EVALUATION_MODEL_MAX_CHARS

    workspaces = await db.execute(select(Workspace.id, Workspace.settings).order_by(Workspace.id))
    found[QA_MODEL_LABEL] = [
        str(ws_id)
        for ws_id, ws_settings in workspaces.all()
        if len(str(((ws_settings or {}).get("qa") or {}).get("evaluation_model") or ""))
        > EVALUATION_MODEL_MAX_CHARS
    ]
    return found


def render(found: dict[str, list[str]]) -> str:
    lines = []
    from app.api.qa import EVALUATION_MODEL_MAX_CHARS

    rows = [(label, cap) for label, _m, _c, cap, _j in FIELDS]
    rows.append((QA_MODEL_LABEL, EVALUATION_MODEL_MAX_CHARS))
    for label, cap in rows:
        ids = found.get(label, [])
        shown = ", ".join(ids[:MAX_IDS_PRINTED])
        more = f" (+{len(ids) - MAX_IDS_PRINTED} more)" if len(ids) > MAX_IDS_PRINTED else ""
        lines.append(
            f"{label} (cap {cap}): {len(ids)} over" + (f" - ids {shown}{more}" if ids else "")
        )
    return "\n".join(lines)


async def _run() -> int:
    from app.db.session import AsyncSessionLocal, engine

    try:
        async with AsyncSessionLocal() as db:
            found = await find_over_cap(db)
    finally:
        await engine.dispose()
    print(render(found))
    return 0


def main() -> int:
    argparse.ArgumentParser(description=__doc__.split("\n\n")[0]).parse_args()
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
