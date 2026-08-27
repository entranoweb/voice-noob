"""Tests for the fixture lifecycle: seed, isolate, roll back, prove.

The claim these defend is "we test against your real database and put it back."
That is only worth making if rollback survives the cases where cleanup usually
fails: a run that raised, and a tool that committed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appointment import Appointment
from app.models.contact import Contact
from app.services.qa.fixtures import (
    FixtureError,
    FixtureLedger,
    fixture_scope,
    verify_restored,
)
from app.services.qa.metrics.base import MetricContext
from app.services.qa.metrics.validation.state_restored import StateRestored

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

SOON = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0)


async def _count(engine: AsyncEngine, model: Any) -> int:
    async with AsyncSession(bind=engine, expire_on_commit=False) as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


@pytest.mark.asyncio
class TestFixtureScope:
    async def test_seeds_the_declared_rows(
        self,
        test_engine: AsyncEngine,
        create_test_user: Any,
    ) -> None:
        user = await create_test_user()
        spec = {"contacts": [{"first_name": "Jane", "phone_number": "5551234567"}]}

        async with fixture_scope(test_engine, user_id=user.id, spec=spec) as run:
            found = (await run.session.execute(select(Contact))).scalars().all()
            assert [c.first_name for c in found] == ["Jane"]

    async def test_seeded_rows_are_gone_afterwards(
        self,
        test_engine: AsyncEngine,
        create_test_user: Any,
    ) -> None:
        user = await create_test_user()
        spec = {"contacts": [{"first_name": "Jane", "phone_number": "5551234567"}]}

        async with fixture_scope(test_engine, user_id=user.id, spec=spec):
            pass

        assert await _count(test_engine, Contact) == 0

    async def test_writes_made_during_the_run_are_rolled_back(
        self,
        test_engine: AsyncEngine,
        create_test_user: Any,
    ) -> None:
        """Not just the fixtures - anything the agent's tools did."""
        user = await create_test_user()

        async with fixture_scope(test_engine, user_id=user.id) as run:
            run.session.add(Contact(user_id=user.id, first_name="Walk-in", phone_number="5550000"))
            await run.session.flush()

        assert await _count(test_engine, Contact) == 0

    async def test_a_committing_tool_still_gets_rolled_back(
        self,
        test_engine: AsyncEngine,
        create_test_user: Any,
    ) -> None:
        """The CRM tools call commit(). Savepoint mode is what makes that safe;
        without it every simulated booking would be a real one."""
        user = await create_test_user()

        async with fixture_scope(test_engine, user_id=user.id) as run:
            run.session.add(Contact(user_id=user.id, first_name="Booked", phone_number="5551111"))
            await run.session.commit()
            assert (await _count_in(run.session, Contact)) == 1

        assert await _count(test_engine, Contact) == 0

    async def test_rollback_happens_even_when_the_run_raises(
        self,
        test_engine: AsyncEngine,
        create_test_user: Any,
    ) -> None:
        """The case best-effort cleanup gets wrong, and the case that matters:
        a crashed run is exactly when residue is left behind."""
        user = await create_test_user()
        spec = {"contacts": [{"first_name": "Jane", "phone_number": "5551234567"}]}

        # PT012: the multi-statement body is the point - the raise has to happen
        # *inside* the scope for this to test anything.
        with pytest.raises(RuntimeError, match="agent exploded"):  # noqa: PT012
            async with fixture_scope(test_engine, user_id=user.id, spec=spec) as run:
                run.session.add(
                    Contact(user_id=user.id, first_name="Extra", phone_number="5552222"),
                )
                await run.session.commit()
                raise RuntimeError("agent exploded")

        assert await _count(test_engine, Contact) == 0

    async def test_pre_existing_rows_are_untouched(
        self,
        test_engine: AsyncEngine,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
    ) -> None:
        """Rollback must undo the run, not the database."""
        user = await create_test_user()
        await create_test_contact(user_id=user.id, first_name="Existing")

        async with fixture_scope(test_engine, user_id=user.id) as run:
            run.session.add(Contact(user_id=user.id, first_name="Temp", phone_number="5553333"))
            await run.session.commit()

        async with AsyncSession(bind=test_engine, expire_on_commit=False) as reader:
            names = [c.first_name for c in (await reader.execute(select(Contact))).scalars().all()]
        assert names == ["Existing"]

    async def test_seeds_an_appointment_against_its_contact(
        self,
        test_engine: AsyncEngine,
        create_test_user: Any,
    ) -> None:
        user = await create_test_user()
        spec = {
            "contacts": [{"first_name": "Jane", "phone_number": "5551234567"}],
            "appointments": [{"contact_phone": "5551234567", "scheduled_at": SOON}],
        }

        async with fixture_scope(test_engine, user_id=user.id, spec=spec) as run:
            appointments = (await run.session.execute(select(Appointment))).scalars().all()
            assert len(appointments) == 1
            assert run.ledger.seeded[-1].table == "appointments"

    async def test_an_appointment_pointing_at_nothing_is_an_error(
        self,
        test_engine: AsyncEngine,
        create_test_user: Any,
    ) -> None:
        """Fail loudly at seed time. A fixture that silently skipped a row would
        make the scenario assert against a world it never set up."""
        user = await create_test_user()
        spec = {"appointments": [{"contact_phone": "5559999", "scheduled_at": SOON}]}

        with pytest.raises(FixtureError, match="5559999"):
            async with fixture_scope(test_engine, user_id=user.id, spec=spec):
                pass

        assert await _count(test_engine, Appointment) == 0


async def _count_in(session: AsyncSession, model: Any) -> int:
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


@pytest.mark.asyncio
class TestLedgerAndVerification:
    async def test_a_clean_run_is_recorded_as_clean(
        self,
        test_engine: AsyncEngine,
        create_test_user: Any,
    ) -> None:
        user = await create_test_user()
        spec = {"contacts": [{"first_name": "Jane", "phone_number": "5551234567"}]}

        async with fixture_scope(test_engine, user_id=user.id, spec=spec) as run:
            ledger = run.ledger

        assert ledger.rolled_back is True
        assert ledger.restored is True
        assert ledger.residue is None
        assert ledger.clean is True
        assert ledger.as_dict()["seeded_count"] == 1

    async def test_verification_can_be_skipped(
        self,
        test_engine: AsyncEngine,
        create_test_user: Any,
    ) -> None:
        """Unverified must stay distinct from verified-clean."""
        user = await create_test_user()

        async with fixture_scope(test_engine, user_id=user.id, verify=False) as run:
            ledger = run.ledger

        assert ledger.rolled_back is True
        assert ledger.restored is None
        assert ledger.clean is False

    async def test_verification_notices_residue(
        self,
        test_engine: AsyncEngine,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_contact: Any,
    ) -> None:
        """Simulates a row that outlived the run, by comparing against a
        baseline taken before it existed."""
        user = await create_test_user()
        baseline: dict[str, list[dict[str, Any]]] = {"contacts": [], "appointments": []}
        await create_test_contact(user_id=user.id, first_name="Leftover")

        ledger = await verify_restored(
            test_engine,
            user_id=user.id,
            ledger=FixtureLedger(rolled_back=True),
            baseline=baseline,
        )

        assert ledger.restored is False
        assert ledger.residue is not None
        assert ledger.residue["contacts"]["added"][0]["first_name"] == "Leftover"
        assert ledger.clean is False


class TestStateRestoredMetric:
    """The ledger has to become a verdict, or it is just a log line."""

    def test_an_unscoped_run_is_not_measurable(self) -> None:
        """Not every run uses fixtures. Failing those would be nonsense."""
        score = StateRestored().compute(MetricContext(run_id="r"))
        assert score.value is None
        assert score.passed is None

    def test_a_clean_ledger_passes(self) -> None:
        score = StateRestored().compute(
            MetricContext(
                run_id="r",
                fixture_ledger={"rolled_back": True, "restored": True, "seeded_count": 2},
            ),
        )
        assert score.passed is True
        assert score.value == 1.0

    def test_residue_fails_the_run(self) -> None:
        score = StateRestored().compute(
            MetricContext(
                run_id="r",
                fixture_ledger={
                    "rolled_back": True,
                    "restored": False,
                    "residue": {"contacts": {"added": [{"first_name": "Leftover"}]}},
                },
            ),
        )
        assert score.passed is False
        assert score.detail["residue"]["contacts"]["added"]

    def test_a_failed_rollback_fails_the_run(self) -> None:
        score = StateRestored().compute(
            MetricContext(run_id="r", fixture_ledger={"rolled_back": False}),
        )
        assert score.passed is False

    def test_unverified_rollback_is_not_measurable(self) -> None:
        """Rollback ran but nothing checked it. That is not evidence of clean."""
        score = StateRestored().compute(
            MetricContext(run_id="r", fixture_ledger={"rolled_back": True, "restored": None}),
        )
        assert score.value is None
