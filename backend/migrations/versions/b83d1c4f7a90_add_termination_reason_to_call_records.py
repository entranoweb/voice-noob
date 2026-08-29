"""add termination reason to call records

Why the conversation ended, as the media bridge observed it — the vocabulary is
`app.monitoring.call_trace.TerminationReason`. Nullable, and the null means
something: nothing recorded a reason. It must not be inferred from the call's
terminal status, because a call that reached "completed" may have been ended by
the caller, by the agent, or by a duration cap, and picking one would put a
fabrication into every dashboard that groups by it.

Separate from `7c4e91f0ab12` rather than folded into it. That revision had
already been pushed, so anywhere it had been applied would never have run the
added statement and would have been left without the column — a migration that
has left your machine is history, not a draft.

Revision ID: b83d1c4f7a90
Revises: 7c4e91f0ab12
Create Date: 2026-08-29 14:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b83d1c4f7a90'
down_revision: Union[str, Sequence[str], None] = '7c4e91f0ab12'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "call_records",
        sa.Column(
            "termination_reason",
            sa.String(length=32),
            nullable=True,
            comment="Why the conversation ended, if observed",
        ),
    )


def downgrade() -> None:
    op.drop_column("call_records", "termination_reason")
