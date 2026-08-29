"""add fixture column to test scenarios

Revision ID: 53b1ba24a87b
Revises: f594242aaae4
Create Date: 2026-08-27 07:21:20.166031

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '53b1ba24a87b'
down_revision: Union[str, Sequence[str], None] = 'f594242aaae4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "test_scenarios",
        sa.Column("fixture", sa.JSON(), nullable=True),
    )




def downgrade() -> None:
    op.drop_column("test_scenarios", "fixture")
