"""merge test-scenario and campaign heads

Revision ID: f594242aaae4
Revises: 017_test_scenarios, 2aeb78a98185
Create Date: 2026-08-26 16:07:36.204825

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f594242aaae4'
down_revision: Union[str, Sequence[str], None] = ('017_test_scenarios', '2aeb78a98185')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
