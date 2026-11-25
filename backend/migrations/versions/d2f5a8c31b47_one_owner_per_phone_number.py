"""one owner per phone number

Inbound routing resolves a dialled number without knowing whose account it
belongs to, so the same number registered under two accounts made the answer
depend on row order: one customer's calls could reach another's agent, or fail
that customer's signature check and go unanswered.

This adds a unique index over live (not released) numbers, compared without the
leading plus so "+44…" and "44…" count as the same number.

It does not clean up for you. If two live records already hold one number, the
upgrade stops and prints which record ids collide; nothing is changed, deleted
or released. Which account really holds a number is a fact about the carrier
account, not something a migration can decide. Release or delete the wrong
record, then run the upgrade again. The check prints record ids and owner ids
only, never the phone numbers themselves.

Revision ID: d2f5a8c31b47
Revises: c7d1e2f3a4b5
Create Date: 2026-11-25 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd2f5a8c31b47'
down_revision: Union[str, Sequence[str], None] = 'c7d1e2f3a4b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "uq_phone_numbers_live_number"


class DuplicatePhoneNumbersError(RuntimeError):
    """Two live records hold one number; a person has to say which is right."""


def find_duplicates(bind: sa.engine.Connection) -> list[list[tuple[str, str]]]:
    """Groups of (record id, owner id) that hold the same live number."""
    rows = bind.execute(
        sa.text(
            "SELECT id, user_id, ltrim(phone_number, '+') AS bare "
            "FROM phone_numbers WHERE status <> 'released' ORDER BY created_at, id"
        )
    ).fetchall()
    groups: dict[str, list[tuple[str, str]]] = {}
    for record_id, owner_id, bare in rows:
        groups.setdefault(bare, []).append((str(record_id), str(owner_id)))
    return [members for members in groups.values() if len(members) > 1]


def upgrade() -> None:
    bind = op.get_bind()
    duplicates = find_duplicates(bind)
    if duplicates:
        lines = [
            f"{len(duplicates)} phone number(s) are registered by more than one live record.",
            "Nothing was changed. Release or delete the wrong record in each group, "
            "then run the upgrade again:",
        ]
        for number, members in enumerate(duplicates, start=1):
            described = ", ".join(f"record {rid} (owner {oid})" for rid, oid in members)
            lines.append(f"  group {number}: {described}")
        raise DuplicatePhoneNumbersError("\n".join(lines))

    op.create_index(
        INDEX_NAME,
        "phone_numbers",
        [sa.text("ltrim(phone_number, '+')")],
        unique=True,
        postgresql_where=sa.text("status <> 'released'"),
        sqlite_where=sa.text("status <> 'released'"),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="phone_numbers")
