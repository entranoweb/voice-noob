"""The migration-drift gate: how differences are named and what fails it.

The comparison against a real migrated database runs in CI
(`scripts/check_migration_drift.py`). These tests pin the parts that decide
pass or fail, on differences built by hand in the shapes Alembic returns.
"""

from sqlalchemy import (
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON

from app.db.migration_drift import compare_with_baseline, drift_keys, parse_baseline


def _tables() -> tuple[Table, Table]:
    metadata = MetaData()
    users = Table("users", metadata, Column("id", Integer, primary_key=True))
    settings = Table(
        "user_settings",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("user_id", Integer),
        Column("tags", JSON),
    )
    return users, settings


def test_each_kind_of_difference_gets_a_readable_stable_key() -> None:
    _users, settings = _tables()
    unique_index = Index("ix_user_settings_user_id", settings.c.user_id, unique=True)
    constraint = UniqueConstraint(settings.c.user_id, name="uq_settings_user")
    settings.append_constraint(constraint)
    foreign_key = ForeignKeyConstraint([settings.c.user_id], ["users.id"], ondelete="CASCADE")
    settings.append_constraint(foreign_key)

    keys = drift_keys(
        [
            ("remove_index", unique_index),
            ("add_constraint", constraint),
            ("add_fk", foreign_key),
            ("add_column", None, "user_settings", Column("region", String(20))),
            ("remove_table", Table("legacy", MetaData())),
            [("modify_type", None, "user_settings", "tags", {}, JSONB(), JSON())],
        ]
    )

    assert keys == sorted(
        [
            "remove_index ix_user_settings_user_id on user_settings(user_id) unique",
            "add_constraint uq_settings_user on user_settings(user_id)",
            "add_fk user_settings(user_id) -> users.id ondelete=CASCADE",
            "add_column user_settings.region",
            "remove_table legacy",
            "modify_type user_settings.tags: JSONB(astext_type=Text()) -> JSON()",
        ]
    )


def test_a_unique_index_and_a_plain_one_are_different_keys() -> None:
    # The gate exists because of exactly this: an index the migrations made
    # unique and the model declares plain, which nothing else noticed.
    _users, settings = _tables()
    plain = drift_keys([("add_index", Index("ix_a", settings.c.user_id))])
    unique = drift_keys([("add_index", Index("ix_a", settings.c.user_id, unique=True))])

    assert plain != unique


def test_column_comments_are_not_compared() -> None:
    assert drift_keys([[("modify_comment", None, "agents", "name", {}, None, "Agent name")]]) == []


def test_an_operation_it_has_not_met_is_still_reported() -> None:
    # Dropping an unknown difference would let new drift through the gate.
    assert drift_keys([("add_sequence", "public", "orders_seq")]) == [
        "add_sequence public orders_seq"
    ]


def test_baseline_ignores_blank_lines_and_comments() -> None:
    text = "# known drift\n\nremove_index ix_a on t(c)\n  add_column t.d  \n"

    assert parse_baseline(text) == ["remove_index ix_a on t(c)", "add_column t.d"]


def test_only_known_differences_pass() -> None:
    report = compare_with_baseline(["a", "b"], ["a", "b"])

    assert report.ok
    assert report.known == ["a", "b"]


def test_a_difference_missing_from_the_baseline_fails() -> None:
    report = compare_with_baseline(["a", "new one"], ["a"])

    assert not report.ok
    assert report.new == ["new one"]


def test_a_baseline_line_that_matches_nothing_fails() -> None:
    # Otherwise a fixed difference could return and be waved through as "known".
    report = compare_with_baseline(["a"], ["a", "already fixed"])

    assert not report.ok
    assert report.fixed == ["already fixed"]
