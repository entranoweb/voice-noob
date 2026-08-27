"""Validation gate: did the run put the database back?

Testing against a real database is only defensible if the run provably undoes
itself. This metric reads the fixture ledger and refuses to let a run that left
residue be read as a clean result.

It sits in the validation layer, not the diagnostic one, and that placement is
the argument. Residue means some later run starts from state this one left
behind, so every result after it is suspect. Reporting the agent's score from
a run that did not clean up would be reporting a number nobody should act on.
"""

from __future__ import annotations

from app.services.qa.metrics.base import (
    BaseMetric,
    MetricCategory,
    MetricContext,
    MetricKind,
    MetricScore,
)
from app.services.qa.metrics.registry import register


@register
class StateRestored(BaseMetric):
    """Whether the run's writes were rolled back and the rollback verified."""

    name = "state_restored"
    version = "v1"
    category = MetricCategory.VALIDATION
    kind = MetricKind.DETERMINISTIC

    def compute(self, context: MetricContext) -> MetricScore:
        ledger = context.fixture_ledger
        if not ledger:
            # An unscoped run is not a dirty run. It is a run where this
            # question was never asked, and saying otherwise would fail every
            # scenario that does not use fixtures.
            return self.not_measurable("run was not fixture-scoped")

        rolled_back = bool(ledger.get("rolled_back"))
        restored = ledger.get("restored")

        if not rolled_back:
            return self.score(
                0.0,
                passed=False,
                reason="rollback did not complete",
                seeded_count=ledger.get("seeded_count", 0),
            )

        if restored is None:
            # Rollback ran but nothing checked it. Unverified is not the same as
            # verified-clean, and only one of them belongs in an audit record.
            return self.not_measurable("rollback was not verified")

        residue = ledger.get("residue")
        return self.score(
            1.0 if restored else 0.0,
            passed=bool(restored),
            seeded_count=ledger.get("seeded_count", 0),
            residue=residue,
        )
