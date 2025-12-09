"""Add error tracking fields to campaigns

Revision ID: fca9f1b81524
Revises: e9a148e30e35
Create Date: 2025-12-04 15:28:12.932421

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'fca9f1b81524'
down_revision: Union[str, Sequence[str], None] = 'e9a148e30e35'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add error tracking fields to campaigns table."""
    op.add_column('campaigns', sa.Column('last_error', sa.Text(), nullable=True, comment='Most recent error message'))
    op.add_column('campaigns', sa.Column('error_count', sa.Integer(), nullable=False, server_default='0', comment='Total number of errors encountered'))
    op.add_column('campaigns', sa.Column('last_error_at', sa.DateTime(timezone=True), nullable=True, comment='When the last error occurred'))


def downgrade() -> None:
    """Remove error tracking fields from campaigns table."""
    op.drop_column('campaigns', 'last_error_at')
    op.drop_column('campaigns', 'error_count')
    op.drop_column('campaigns', 'last_error')
