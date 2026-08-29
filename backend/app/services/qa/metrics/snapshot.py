"""Capture CRM state so task completion can be decided, not guessed.

``task_completion`` compares the database after a run against what the scenario
says a successful run should produce. This module takes that snapshot.

Scoped to one user, because a test must not see or assert on another tenant's
rows — and because a shared row changing underneath a run would make the same
scenario pass or fail depending on who else was using the system.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.models.appointment import Appointment
from app.models.contact import Contact

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Fields worth comparing, per table. Deliberately excludes surrogate keys and
# timestamps: an autoincrement id and a created_at differ on every run, so
# including them would make every comparison fail for reasons no scenario cares
# about. A scenario asserts on meaning, not on row identity.
CONTACT_FIELDS = (
    "first_name",
    "last_name",
    "email",
    "phone_number",
    "company_name",
    "status",
)

APPOINTMENT_FIELDS = (
    "contact_id",
    "scheduled_at",
    "duration_minutes",
    "status",
    "service_type",
)


def _record(obj: object, fields: tuple[str, ...]) -> dict[str, Any]:
    """Project one ORM object onto the named fields, stringifying values.

    Values are stringified so a datetime compares equal to the ISO string a
    scenario fixture would naturally declare, rather than failing on type.
    """
    record: dict[str, Any] = {}
    for name in fields:
        value = getattr(obj, name, None)
        record[name] = None if value is None else str(value)
    return record


async def capture_crm_state(db: AsyncSession, user_id: int) -> dict[str, list[dict[str, Any]]]:
    """Snapshot one user's CRM rows in the shape task_completion compares.

    Returns a mapping of table name to a list of projected records. The metric
    reduces this further to just the fields a scenario mentions, so returning a
    little more here than any one scenario needs is intentional.
    """
    contacts = (await db.execute(select(Contact).where(Contact.user_id == user_id))).scalars().all()
    contact_ids = [c.id for c in contacts]

    appointments = (
        (
            await db.execute(
                select(Appointment).where(Appointment.contact_id.in_(contact_ids)),
            )
        )
        .scalars()
        .all()
        if contact_ids
        else []
    )

    return {
        "contacts": [_record(c, CONTACT_FIELDS) for c in contacts],
        "appointments": [_record(a, APPOINTMENT_FIELDS) for a in appointments],
    }
