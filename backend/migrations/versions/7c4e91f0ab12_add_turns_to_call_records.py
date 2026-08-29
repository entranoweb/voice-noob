"""add turns column to call records

Holds the per-turn audio timings recorded off the live media bridge: intended and
transcribed text, time to first audio byte, barge-in and interruption flags.
Nullable on purpose — null means no audio was recorded for the call, which the
audio metrics report as not measurable, and that is a different fact from a call
whose turns were recorded and were bad.

Revision ID: 7c4e91f0ab12
Revises: 53b1ba24a87b
Create Date: 2026-08-29 13:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '7c4e91f0ab12'
down_revision: Union[str, Sequence[str], None] = '53b1ba24a87b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "call_records",
        sa.Column(
            "turns",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Recorded conversational turns with audio timings",
        ),
    )


def downgrade() -> None:
    op.drop_column("call_records", "turns")
