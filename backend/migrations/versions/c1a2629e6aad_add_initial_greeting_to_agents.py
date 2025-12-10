"""Add initial_greeting to agents

Revision ID: c1a2629e6aad
Revises: 014_add_embed_settings
Create Date: 2025-11-29 22:08:57.733701

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1a2629e6aad'
down_revision: Union[str, Sequence[str], None] = '014_embed_settings'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add initial_greeting column to agents table."""
    op.add_column(
        'agents',
        sa.Column(
            'initial_greeting',
            sa.Text(),
            nullable=True,
            comment='Optional initial greeting the agent speaks when call starts'
        )
    )


def downgrade() -> None:
    """Remove initial_greeting column from agents table."""
    op.drop_column('agents', 'initial_greeting')
