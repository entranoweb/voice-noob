"""encrypt stored credentials; add audit_events and compliance_runs

Three things, in one revision because they ship together:

1. Encrypts every provider key and integration credential already in the
   database, in place, under the active key in CREDENTIAL_ENCRYPTION_KEYS. No
   column changes type: a sealed value is text in a text column, and a sealed
   credentials document is a one-key JSON object in the JSON column.
2. Creates ``audit_events``, the append-only audit trail.
3. Creates ``compliance_runs``, the record of each export, erasure and
   retention sweep that the compliance status page reports from.

Safe to run again: a value already under the active key is left alone, and a
value under an older key in the ring is moved to the active one.

With no key configured (possible only with DEBUG on) the encryption step is
skipped and says so; the application reads plain values back unchanged.

Revision ID: d2f6a81c5e37
Revises: b83d1c4f7a90
Create Date: 2026-11-25 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd2f6a81c5e37'
down_revision: Union[str, Sequence[str], None] = 'b83d1c4f7a90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "actor_user_id",
            sa.Integer(),
            nullable=True,
            comment="User who acted; null for the system",
        ),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=True),
        sa.Column("target_id", sa.String(length=128), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
    )
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])
    op.create_index("ix_audit_events_actor_user_id", "audit_events", ["actor_user_id"])
    op.create_index("ix_audit_events_workspace_id", "audit_events", ["workspace_id"])
    op.create_index("ix_audit_events_action", "audit_events", ["action"])

    op.create_table(
        "compliance_runs",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column(
            "user_id",
            sa.Integer(),
            nullable=True,
            comment="Account covered; null means every account",
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("counts", sa.JSON(), nullable=True),
        sa.Column(
            "error",
            sa.String(length=255),
            nullable=True,
            comment="Exception type only; never data",
        ),
    )
    op.create_index("ix_compliance_runs_kind", "compliance_runs", ["kind"])
    op.create_index("ix_compliance_runs_user_id", "compliance_runs", ["user_id"])
    op.create_index("ix_compliance_runs_started_at", "compliance_runs", ["started_at"])

    from app.core.credential_store import encrypt_to_active_key
    from app.core.secret_box import get_box

    if get_box() is None:
        print(
            "CREDENTIAL_ENCRYPTION_KEYS is not set: stored credentials were NOT "
            "encrypted. Set the key and run scripts/rotate_credential_keys.py."
        )
        return
    changed = encrypt_to_active_key(op.get_bind())
    print(f"Encrypted stored credentials: {changed}")


def downgrade() -> None:
    from app.core.credential_store import decrypt_to_plain
    from app.core.secret_box import get_box

    # The previous revision's code reads these columns as plain text, so they
    # must go back to plain text or every stored credential stops working.
    if get_box() is not None:
        decrypt_to_plain(op.get_bind())

    op.drop_index("ix_compliance_runs_started_at", table_name="compliance_runs")
    op.drop_index("ix_compliance_runs_user_id", table_name="compliance_runs")
    op.drop_index("ix_compliance_runs_kind", table_name="compliance_runs")
    op.drop_table("compliance_runs")
    op.drop_index("ix_audit_events_action", table_name="audit_events")
    op.drop_index("ix_audit_events_workspace_id", table_name="audit_events")
    op.drop_index("ix_audit_events_actor_user_id", table_name="audit_events")
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
    op.drop_table("audit_events")
