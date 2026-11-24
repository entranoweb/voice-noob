"""Make ix_user_settings_user_id a plain index, so a user can hold more than one settings row.

The table was created with a UNIQUE index on user_id (657413e72bb1). Revision
011_ws_user_settings then added workspace_id and a unique constraint on
(user_id, workspace_id), and tried to lift the old rule by dropping a
constraint called user_settings_user_id_key. The old rule was never that
constraint; it was the unique index, which stayed.

So on every migrated database a user could store exactly one settings row:
saving keys for a second workspace, or for a workspace after saving at account
level, violated the index. The models declare the index as plain, and the tests
build their schema from the models, so no test saw it. The migration-drift
check added alongside this revision is what found it.

Revision ID: c7d1e2f3a4b5
Revises: b83d1c4f7a90
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7d1e2f3a4b5"
down_revision: str | None = "b83d1c4f7a90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_user_settings_user_id"
TABLE = "user_settings"


def upgrade() -> None:
    """Replace the unique index on user_id with a plain one."""
    indexes = {index["name"]: index for index in sa.inspect(op.get_bind()).get_indexes(TABLE)}
    existing = indexes.get(INDEX)

    if existing is not None and existing.get("unique"):
        op.drop_index(INDEX, table_name=TABLE)
        existing = None
    if existing is None:
        op.create_index(INDEX, TABLE, ["user_id"], unique=False)


def downgrade() -> None:
    """Nothing to undo.

    Restoring the unique index would fail on any database where a user has
    settings in more than one workspace, which is the state this revision
    exists to allow. The plain index is correct under every earlier revision
    that has a workspace_id column.
    """
