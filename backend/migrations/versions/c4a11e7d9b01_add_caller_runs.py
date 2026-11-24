"""add caller runs and caller calls

Results uploaded from the caller rig: one row per batch, one row per call. Both
carry the upload as it arrived (`raw`), because the caller's result format is
still changing and a column added later must be fillable from what was stored.

Both tables cascade from `workspaces`, so deleting a workspace removes its
results, and `caller_calls` cascades from `caller_runs`.

Revision ID: c4a11e7d9b01
Revises: d2f6a81c5e37
Create Date: 2026-11-25 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4a11e7d9b01'
down_revision: Union[str, Sequence[str], None] = 'd2f6a81c5e37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "caller_runs",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
            comment="Workspace that owns this run",
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            comment="User who uploaded the run",
        ),
        sa.Column(
            "run_id",
            sa.String(length=64),
            nullable=False,
            comment="Run id chosen by the caller; uploads are idempotent",
        ),
        sa.Column(
            "content_hash",
            sa.String(length=64),
            nullable=False,
            comment="SHA-256 of the uploaded bundle",
        ),
        sa.Column(
            "payload_version",
            sa.Integer(),
            nullable=False,
            comment="Version of the bundle format as uploaded",
        ),
        sa.Column("rig_version", sa.String(length=64), nullable=False),
        sa.Column("target_label", sa.String(length=200), nullable=False),
        sa.Column("scenario_set_hash", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempted", sa.Integer(), nullable=False),
        sa.Column("graded", sa.Integer(), nullable=False),
        sa.Column("passed", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("not_run", sa.Integer(), nullable=False),
        sa.Column("outcome_counts", sa.JSON(), nullable=False),
        sa.Column("excluded", sa.JSON(), nullable=False),
        sa.Column("chains", sa.JSON(), nullable=False),
        sa.Column("raw", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("workspace_id", "run_id", name="uq_caller_runs_workspace_run_id"),
    )
    op.create_index("ix_caller_runs_workspace_id", "caller_runs", ["workspace_id"])
    op.create_index("ix_caller_runs_user_id", "caller_runs", ["user_id"])

    op.create_table(
        "caller_calls",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "run_pk",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("caller_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "position", sa.Integer(), nullable=False, comment="Order within the run, from 1"
        ),
        sa.Column("file", sa.String(length=200), nullable=False),
        sa.Column("scenario", sa.String(length=300), nullable=False),
        sa.Column("scenario_file", sa.String(length=300), nullable=False),
        sa.Column("mutates", sa.String(length=40), nullable=False),
        sa.Column("repeat", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(length=60), nullable=False),
        sa.Column("fault_class", sa.String(length=60), nullable=True),
        sa.Column("last_step", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.String(length=200), nullable=True),
        sa.Column("ended", sa.String(length=200), nullable=False),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("synthetic", sa.Boolean(), nullable=False),
        sa.Column("labels_unverified", sa.Boolean(), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column("checks", sa.JSON(), nullable=False),
        sa.Column("timeline", sa.JSON(), nullable=False),
        sa.Column("tool_calls", sa.JSON(), nullable=False),
        sa.Column("raw", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_caller_calls_run_pk", "caller_calls", ["run_pk"])
    op.create_index("ix_caller_calls_workspace_id", "caller_calls", ["workspace_id"])
    op.create_index("ix_caller_calls_outcome", "caller_calls", ["outcome"])


def downgrade() -> None:
    op.drop_index("ix_caller_calls_outcome", table_name="caller_calls")
    op.drop_index("ix_caller_calls_workspace_id", table_name="caller_calls")
    op.drop_index("ix_caller_calls_run_pk", table_name="caller_calls")
    op.drop_table("caller_calls")
    op.drop_index("ix_caller_runs_user_id", table_name="caller_runs")
    op.drop_index("ix_caller_runs_workspace_id", table_name="caller_runs")
    op.drop_table("caller_runs")
