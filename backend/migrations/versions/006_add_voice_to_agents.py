"""Add voice column to agents table.

Revision ID: 006_add_voice_to_agents
Revises: 005_add_workspaces
Create Date: 2024-11-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "006_add_voice_to_agents"
down_revision: str | None = "005_add_workspaces"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add voice column to agents table."""
    op.add_column(
        "agents",
        sa.Column(
            "voice",
            sa.String(50),
            nullable=False,
            server_default="shimmer",
            comment="Voice for TTS (e.g., alloy, shimmer, coral)",
        ),
    )


def downgrade() -> None:
    """Remove voice column from agents table."""
    op.drop_column("agents", "voice")
