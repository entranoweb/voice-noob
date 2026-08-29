"""Seed, isolate and roll back the data a scenario runs against.

Testing an agent against a real database is only sellable if you can prove you
put it back. This module provides that lifecycle:

    baseline snapshot -> seed fixtures -> run -> assert -> roll back -> prove

Isolation is a real database transaction, not a best-effort cleanup pass. The
session handed to the agent's tools joins an outer transaction in savepoint
mode, so the ``commit()`` calls inside those tools release a savepoint instead
of writing anything durably. Rolling the outer transaction back at the end
removes everything the run did, including rows we never knew about.

Deleting what we think we created is the alternative, and it is worse: it
misses cascades, it cannot undo an *update* to a row that already existed, and
it leaves residue precisely when a run failed halfway, which is when residue
matters most.

Rollback is then verified rather than assumed. After the transaction is gone we
re-read the database on a fresh connection and compare it to the baseline. That
comparison is what turns "we clean up after ourselves" from a promise into a
record, and it is the artifact a change-advisory board asks for.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appointment import Appointment
from app.models.contact import Contact
from app.services.qa.metrics.snapshot import capture_crm_state

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine

logger = structlog.get_logger()

# Tables a fixture may seed, in dependency order: an appointment needs its
# contact to exist first.
SEEDABLE = ("contacts", "appointments")


class FixtureError(RuntimeError):
    """Raised when a fixture cannot be seeded as declared."""


@dataclass(frozen=True)
class SeededRow:
    """One row a fixture created, recorded for the audit trail."""

    table: str
    pk: Any
    label: str | None = None


@dataclass
class FixtureLedger:
    """The record of what a run touched and whether it was undone.

    ``restored is None`` means verification never ran — distinct from False,
    which means it ran and found residue. The same distinction the metric layer
    makes between *not measured* and *measured and bad*, for the same reason.
    """

    seeded: list[SeededRow] = field(default_factory=list)
    rolled_back: bool = False
    restored: bool | None = None
    residue: dict[str, Any] | None = None

    @property
    def clean(self) -> bool:
        """True only when rollback ran and verification confirmed it."""
        return self.rolled_back and self.restored is True

    def as_dict(self) -> dict[str, Any]:
        """Serialisable form, for storing alongside the run."""
        return {
            "seeded": [{"table": r.table, "pk": str(r.pk), "label": r.label} for r in self.seeded],
            "seeded_count": len(self.seeded),
            "rolled_back": self.rolled_back,
            "restored": self.restored,
            "residue": self.residue,
            "clean": self.clean,
        }


@dataclass
class FixtureRun:
    """What a scoped run gets: a session to work in, and its ledger."""

    session: AsyncSession
    ledger: FixtureLedger
    baseline: dict[str, list[dict[str, Any]]]


def _contact_key(record: dict[str, Any]) -> str | None:
    """The field an appointment uses to point at its contact."""
    phone = record.get("phone_number")
    return str(phone) if phone else None


async def _seed(
    session: AsyncSession,
    spec: dict[str, Any],
    user_id: int,
    ledger: FixtureLedger,
) -> None:
    """Insert the declared rows, resolving appointment references to contacts."""
    contacts_by_phone: dict[str, Contact] = {}

    for record in spec.get("contacts") or []:
        if not isinstance(record, dict):
            raise FixtureError(f"contact fixture must be an object, got {type(record).__name__}")
        contact = Contact(user_id=user_id, **record)
        session.add(contact)
        await session.flush()
        key = _contact_key(record)
        if key:
            contacts_by_phone[key] = contact
        ledger.seeded.append(SeededRow("contacts", contact.id, key))

    for record in spec.get("appointments") or []:
        if not isinstance(record, dict):
            raise FixtureError(
                f"appointment fixture must be an object, got {type(record).__name__}",
            )
        data = dict(record)
        # An appointment names its contact by phone number, because a fixture is
        # written before any ids exist.
        phone = data.pop("contact_phone", None)
        contact_id = data.pop("contact_id", None)
        if contact_id is None:
            if phone is None:
                raise FixtureError(
                    "appointment fixture needs contact_phone or contact_id",
                )
            target = contacts_by_phone.get(str(phone))
            if target is None:
                raise FixtureError(
                    f"appointment references contact_phone {phone!r}, which no seeded contact has",
                )
            contact_id = target.id

        appointment = Appointment(contact_id=contact_id, **data)
        session.add(appointment)
        await session.flush()
        ledger.seeded.append(SeededRow("appointments", appointment.id, str(phone or contact_id)))


@asynccontextmanager
async def fixture_scope(
    engine: AsyncEngine,
    *,
    user_id: int,
    spec: dict[str, Any] | None = None,
    verify: bool = True,
) -> AsyncIterator[FixtureRun]:
    """Run a block against seeded data, then undo everything it did.

    The yielded session is the one to hand to the agent's tools. Anything they
    write — including through their own ``commit()`` — is inside the outer
    transaction this opens, and is gone when the block exits.

    Rollback happens whether the block succeeds or raises. A run that crashed
    halfway is exactly the case that leaves residue if cleanup is best-effort.
    """
    baseline: dict[str, list[dict[str, Any]]] = {}
    ledger = FixtureLedger()

    # Read the baseline outside the scoped transaction, so it reflects what the
    # database looked like before this run and can be compared against after.
    async with AsyncSession(bind=engine, expire_on_commit=False) as reader:
        baseline = await capture_crm_state(reader, user_id)

    connection = await engine.connect()
    transaction = await connection.begin()
    # join_transaction_mode="create_savepoint" is what makes the tools' own
    # commits non-durable: each becomes a savepoint release inside this
    # transaction rather than a write.
    session = AsyncSession(
        bind=connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    )

    try:
        if spec:
            await _seed(session, spec, user_id, ledger)
            await session.flush()
        yield FixtureRun(session=session, ledger=ledger, baseline=baseline)
    finally:
        await session.close()
        try:
            await transaction.rollback()
            ledger.rolled_back = True
        except Exception:
            logger.exception("fixture_rollback_failed", user_id=user_id)
            ledger.rolled_back = False
        finally:
            await connection.close()

        if verify and ledger.rolled_back:
            await verify_restored(
                engine,
                user_id=user_id,
                ledger=ledger,
                baseline=baseline,
            )


async def verify_restored(
    engine: AsyncEngine,
    *,
    user_id: int,
    ledger: FixtureLedger,
    baseline: dict[str, list[dict[str, Any]]],
) -> FixtureLedger:
    """Re-read the database and record whether it matches the baseline.

    Uses a fresh connection deliberately: reading through the same session that
    did the work could be answered from its identity map, which would prove
    nothing about what is actually on disk.
    """
    async with AsyncSession(bind=engine, expire_on_commit=False) as reader:
        after = await capture_crm_state(reader, user_id)

    residue = _residue(baseline, after)
    ledger.restored = not residue
    ledger.residue = residue or None
    return ledger


def _residue(baseline: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Rows present afterwards that the baseline did not have, per table."""
    leftovers: dict[str, Any] = {}
    for table in set(baseline) | set(after):
        before_rows = list(baseline.get(table) or [])
        after_rows = list(after.get(table) or [])
        extra = []
        remaining = list(before_rows)
        for row in after_rows:
            if row in remaining:
                remaining.remove(row)
            else:
                extra.append(row)
        if extra or remaining:
            leftovers[table] = {"added": extra, "missing": remaining}
    return leftovers


__all__ = [
    "SEEDABLE",
    "FixtureError",
    "FixtureLedger",
    "FixtureRun",
    "SeededRow",
    "fixture_scope",
    "verify_restored",
]
