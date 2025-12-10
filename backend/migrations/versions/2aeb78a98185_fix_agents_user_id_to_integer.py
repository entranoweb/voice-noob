"""fix_agents_user_id_to_integer

Revision ID: 2aeb78a98185
Revises: fca9f1b81524
Create Date: 2025-12-05 03:33:03.171230

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "2aeb78a98185"
down_revision: Union[str, Sequence[str], None] = "fca9f1b81524"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Fix agents.user_id from UUID to Integer to match users.id."""
    # Drop the existing index on user_id
    op.drop_index("ix_agents_user_id", table_name="agents")

    # Add a new integer column
    op.add_column("agents", sa.Column("user_id_new", sa.Integer(), nullable=True))

    # Set all existing agents to user_id 1 (the default admin user)
    op.execute("UPDATE agents SET user_id_new = 1")

    # Make the new column not nullable
    op.alter_column("agents", "user_id_new", nullable=False)

    # Drop the old UUID column
    op.drop_column("agents", "user_id")

    # Rename the new column to user_id
    op.alter_column("agents", "user_id_new", new_column_name="user_id")

    # Create foreign key constraint
    op.create_foreign_key(
        "fk_agents_user_id",
        "agents",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Recreate the index
    op.create_index("ix_agents_user_id", "agents", ["user_id"])


def downgrade() -> None:
    """Revert agents.user_id back to UUID (data will be lost)."""
    # Drop the foreign key
    op.drop_constraint("fk_agents_user_id", "agents", type_="foreignkey")

    # Drop the index
    op.drop_index("ix_agents_user_id", table_name="agents")

    # Add UUID column
    op.add_column("agents", sa.Column("user_id_new", sa.UUID(), nullable=True))

    # Generate UUIDs for existing rows
    op.execute(
        "UPDATE agents SET user_id_new = gen_random_uuid()"
    )

    # Make not nullable
    op.alter_column("agents", "user_id_new", nullable=False)

    # Drop integer column
    op.drop_column("agents", "user_id")

    # Rename
    op.alter_column("agents", "user_id_new", new_column_name="user_id")

    # Recreate index
    op.create_index("ix_agents_user_id", "agents", ["user_id"])
