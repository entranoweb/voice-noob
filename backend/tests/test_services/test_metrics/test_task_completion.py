"""Tests for deterministic task completion via database state."""

from __future__ import annotations

from typing import Any

from app.services.qa.metrics.accuracy.task_completion import (
    TaskCompletion,
    canonicalise,
    diff_state,
    project,
    state_hash,
)
from app.services.qa.metrics.base import MetricCategory, MetricContext, MetricKind


def context(**kwargs: Any) -> MetricContext:
    return MetricContext(run_id="run-1", **kwargs)


class TestCanonicalisation:
    """State comparison must not depend on incidental ordering."""

    def test_record_order_does_not_change_the_hash(self) -> None:
        """Databases return rows in whatever order they like."""
        a = {"appointments": [{"id": 1}, {"id": 2}]}
        b = {"appointments": [{"id": 2}, {"id": 1}]}
        assert state_hash(a) == state_hash(b)

    def test_key_order_does_not_change_the_hash(self) -> None:
        a = {"appointments": [{"status": "scheduled", "contact_id": 7}]}
        b = {"appointments": [{"contact_id": 7, "status": "scheduled"}]}
        assert state_hash(a) == state_hash(b)

    def test_table_order_does_not_change_the_hash(self) -> None:
        a = {"contacts": [{"id": 1}], "appointments": [{"id": 2}]}
        b = {"appointments": [{"id": 2}], "contacts": [{"id": 1}]}
        assert state_hash(a) == state_hash(b)

    def test_different_content_changes_the_hash(self) -> None:
        a = {"appointments": [{"status": "scheduled"}]}
        b = {"appointments": [{"status": "cancelled"}]}
        assert state_hash(a) != state_hash(b)

    def test_non_serialisable_values_do_not_raise(self) -> None:
        """Datetimes and UUIDs are ordinary in a database snapshot."""
        from datetime import UTC, datetime

        state = {"appointments": [{"scheduled_at": datetime(2026, 3, 1, tzinfo=UTC)}]}
        assert canonicalise(state)  # does not raise


class TestProjection:
    """Only the fields a scenario asserts on should be compared."""

    def test_ignores_fields_the_scenario_does_not_mention(self) -> None:
        expected = {"appointments": [{"status": "scheduled"}]}
        actual = {
            "appointments": [
                {"id": 91, "status": "scheduled", "created_at": "2026-03-01T10:00:00Z"},
            ],
        }
        assert project(actual, expected) == {"appointments": [{"status": "scheduled"}]}

    def test_ignores_tables_the_scenario_does_not_mention(self) -> None:
        expected = {"appointments": [{"status": "scheduled"}]}
        actual = {"appointments": [{"status": "scheduled"}], "contacts": [{"id": 1}]}
        assert "contacts" not in project(actual, expected)

    def test_missing_table_projects_to_empty(self) -> None:
        expected = {"appointments": [{"status": "scheduled"}]}
        assert project({}, expected) == {"appointments": []}


class TestDiff:
    """A failure has to say what went wrong, not just that it did."""

    def test_reports_a_missing_record(self) -> None:
        diff = diff_state({"appointments": [{"status": "scheduled"}]}, {"appointments": []})
        assert diff["appointments"]["missing"] == [{"status": "scheduled"}]
        assert diff["appointments"]["actual_count"] == 0

    def test_reports_an_unexpected_record(self) -> None:
        diff = diff_state({"appointments": []}, {"appointments": [{"status": "scheduled"}]})
        assert diff["appointments"]["unexpected"] == [{"status": "scheduled"}]

    def test_reports_a_wrong_value_as_both_sides(self) -> None:
        """The useful signal is 'expected this, got that'."""
        diff = diff_state(
            {"appointments": [{"status": "scheduled"}]},
            {"appointments": [{"status": "cancelled"}]},
        )
        assert diff["appointments"]["missing"] == [{"status": "scheduled"}]
        assert diff["appointments"]["unexpected"] == [{"status": "cancelled"}]

    def test_matching_tables_are_omitted(self) -> None:
        assert diff_state({"contacts": [{"id": 1}]}, {"contacts": [{"id": 1}]}) == {}


class TestTaskCompletionMetric:
    """The metric itself."""

    metric = TaskCompletion()

    def test_identity(self) -> None:
        """Category and kind are part of the contract consumers rely on."""
        assert self.metric.category is MetricCategory.ACCURACY
        assert self.metric.kind is MetricKind.DETERMINISTIC

    def test_passes_when_the_expected_state_was_produced(self) -> None:
        score = self.metric.compute(
            context(
                expected_db_state={"appointments": [{"status": "scheduled"}]},
                final_db_state={
                    "appointments": [{"id": 4, "status": "scheduled"}],
                    "contacts": [{"id": 1}],
                },
            ),
        )
        assert score.value == 1.0
        assert score.passed is True
        assert "diff" not in score.detail

    def test_fails_when_the_appointment_was_never_created(self) -> None:
        """The flagship failure: agent talked well, booked nothing."""
        score = self.metric.compute(
            context(
                expected_db_state={"appointments": [{"status": "scheduled"}]},
                final_db_state={"appointments": []},
            ),
        )
        assert score.value == 0.0
        assert score.passed is False
        assert score.detail["diff"]["appointments"]["missing"] == [{"status": "scheduled"}]

    def test_fails_when_booked_with_the_wrong_details(self) -> None:
        score = self.metric.compute(
            context(
                expected_db_state={"appointments": [{"scheduled_at": "2026-03-25T14:00:00Z"}]},
                final_db_state={"appointments": [{"scheduled_at": "2026-03-20T14:00:00Z"}]},
            ),
        )
        assert score.passed is False
        assert score.detail["diff"]["appointments"]["unexpected"]

    def test_unmeasurable_without_a_scenario_expectation(self) -> None:
        """Not every scenario asserts on the database; that is not a failure."""
        score = self.metric.compute(context(final_db_state={"appointments": []}))
        assert score.value is None
        assert score.passed is None
        assert score.measured is False

    def test_unmeasurable_without_a_snapshot(self) -> None:
        """A missing snapshot is a harness gap, not an agent failure."""
        score = self.metric.compute(
            context(expected_db_state={"appointments": [{"status": "scheduled"}]}),
        )
        assert score.value is None
        assert "snapshot" in score.detail["reason"]

    def test_extra_unrelated_rows_do_not_fail_the_run(self) -> None:
        """A scenario asserts what it names and nothing more."""
        score = self.metric.compute(
            context(
                expected_db_state={"appointments": [{"status": "scheduled"}]},
                final_db_state={
                    "appointments": [{"status": "scheduled"}],
                    "call_records": [{"id": 99}],
                },
            ),
        )
        assert score.passed is True
