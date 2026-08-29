"""Task completion, decided by database state rather than opinion.

The bottom-line accuracy question — *did the agent actually do the thing?* — is
answered by comparing the state of the CRM after the run against the state the
scenario says a successful run should produce. No model is involved, so there is
no judge variance, no calibration burden, and nothing for a customer to argue
with when the answer is "no".

This is the single most valuable metric in the suite, because "did it book the
appointment" is the question the product exists to answer, and until now it was
being guessed at by a language model reading a transcript that could not contain
a tool call in the first place.

Comparison is by hash so the check is exact and cheap; on mismatch the metric
computes a field-level diff, because a bare "0.0" tells nobody what went wrong.

Approach adapted from ServiceNow's EVA (MIT).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.services.qa.metrics.base import (
    BaseMetric,
    MetricCategory,
    MetricContext,
    MetricKind,
    MetricScore,
)
from app.services.qa.metrics.registry import register

# A scenario's expected state is keyed by table, each holding a list of records:
#
#   {"appointments": [{"contact_id": 1, "status": "scheduled", ...}], ...}
#
# Only the tables and fields the scenario names are compared. Anything the
# scenario is silent about is ignored, so a scenario asserting "an appointment
# exists with this status" does not also accidentally assert that no contact was
# updated. Tests should say what they mean and nothing more.
DbState = dict[str, list[dict[str, Any]]]


def canonicalise(state: DbState) -> str:
    """Serialise state so equal states always produce an identical string.

    Keys are sorted at every level and records are sorted by their serialised
    form, because row order out of a database is not meaningful and would
    otherwise make an identical outcome hash differently on a rerun.
    """
    normalised = {
        table: sorted(
            (json.dumps(record, sort_keys=True, default=str) for record in records),
        )
        for table, records in sorted(state.items())
    }
    return json.dumps(normalised, sort_keys=True)


def state_hash(state: DbState) -> str:
    """Stable SHA-256 of a database state."""
    return hashlib.sha256(canonicalise(state).encode()).hexdigest()


def diff_state(expected: DbState, actual: DbState) -> dict[str, Any]:
    """Explain how two states differ, in terms a person can act on.

    Reports, per table, the records that were expected but absent and the ones
    present but unexpected. Deliberately shallow: the useful signal is almost
    always "the appointment was never created" or "it was created with the wrong
    time", and both are visible at record level.
    """
    diff: dict[str, Any] = {}

    for table in sorted(set(expected) | set(actual)):
        expected_records = expected.get(table, [])
        actual_records = actual.get(table, [])

        expected_keys = {json.dumps(r, sort_keys=True, default=str) for r in expected_records}
        actual_keys = {json.dumps(r, sort_keys=True, default=str) for r in actual_records}

        missing = sorted(expected_keys - actual_keys)
        unexpected = sorted(actual_keys - expected_keys)

        if missing or unexpected:
            diff[table] = {
                "missing": [json.loads(r) for r in missing],
                "unexpected": [json.loads(r) for r in unexpected],
                "expected_count": len(expected_records),
                "actual_count": len(actual_records),
            }

    return diff


def project(state: DbState, template: DbState) -> DbState:
    """Reduce ``state`` to the tables and fields ``template`` mentions.

    Lets a scenario assert on a handful of fields without having to enumerate
    every column the row happens to carry — ids, timestamps and defaults are
    noise for this purpose and would make every comparison fail.
    """
    projected: DbState = {}
    for table, expected_records in template.items():
        fields: set[str] = set()
        for record in expected_records:
            fields.update(record)
        projected[table] = [
            {k: v for k, v in record.items() if k in fields} for record in state.get(table, [])
        ]
    return projected


@register
class TaskCompletion(BaseMetric):
    """Binary: did the run leave the database in the expected state?"""

    name = "task_completion"
    version = "v1"
    category = MetricCategory.ACCURACY
    kind = MetricKind.DETERMINISTIC

    def compute(self, context: MetricContext) -> MetricScore:
        expected = context.expected_db_state
        actual = context.final_db_state

        if expected is None:
            return self.not_measurable(
                "scenario declares no expected_db_state",
            )
        if actual is None:
            return self.not_measurable(
                "no database snapshot was captured for this run",
            )

        # Compare only what the scenario asserts.
        projected = project(actual, expected)

        expected_digest = state_hash(expected)
        actual_digest = state_hash(projected)
        matched = expected_digest == actual_digest

        detail: dict[str, Any] = {
            "expected_hash": expected_digest,
            "actual_hash": actual_digest,
        }
        if not matched:
            detail["diff"] = diff_state(expected, projected)

        return self.score(1.0 if matched else 0.0, passed=matched, **detail)
