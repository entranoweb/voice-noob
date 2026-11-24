"""Bulk operations on stored credentials: first encryption, rotation, reversal.

Used by the migration that encrypts existing rows and by
``scripts/rotate_credential_keys.py``. Works on raw rows through lightweight
table definitions, not the ORM models, so it reads exactly what is stored and
is unaffected by the encrypting column types.

Every function is safe to run again: a value already under the active key is
left alone.
"""

from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.core.secret_box import SecretBox, get_box, is_sealed
from app.db.encrypted_types import SEALED_JSON_KEY, is_sealed_json, seal_json, unseal_json

_metadata = sa.MetaData()

# (table, text columns that hold a secret, JSON columns that hold a secret)
SECRET_COLUMNS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "user_settings": (
        (
            "openai_api_key",
            "deepgram_api_key",
            "elevenlabs_api_key",
            "azure_openai_api_key",
            "telnyx_api_key",
            "twilio_auth_token",
        ),
        (),
    ),
    "user_integrations": (("refresh_token",), ("credentials",)),
}


def _table(name: str) -> sa.Table:
    if name in _metadata.tables:
        return _metadata.tables[name]
    text_columns, json_columns = SECRET_COLUMNS[name]
    return sa.Table(
        name,
        _metadata,
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        *(sa.Column(column, sa.Text) for column in text_columns),
        *(sa.Column(column, sa.JSON) for column in json_columns),
    )


def _require_box() -> SecretBox:
    box = get_box()
    if box is None:
        message = "CREDENTIAL_ENCRYPTION_KEYS is not set; there is no key to work with."
        raise RuntimeError(message)
    return box


def _to_active_text(box: SecretBox, value: str | None, context: str) -> str | None:
    """The value under the active key, or None when nothing needs to change."""
    if value is None or value == "":
        return None
    if not is_sealed(value):
        return box.encrypt(value, context)
    if box.key_id_of(value) == box.active_key_id:
        return None
    return box.encrypt(box.decrypt(value, context), context)


def _to_active_json(box: SecretBox, value: Any, context: str) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if not is_sealed_json(value):
        return seal_json(value, context)
    if box.key_id_of(value[SEALED_JSON_KEY]) == box.active_key_id:
        return None
    return seal_json(unseal_json(value, context), context)


def encrypt_to_active_key(connection: Connection) -> dict[str, int]:
    """Put every stored credential under the active key.

    Encrypts values still in plain text and re-encrypts values under an older
    key. Returns how many rows changed per table.
    """
    box = _require_box()
    changed: dict[str, int] = {}
    for name, (text_columns, json_columns) in SECRET_COLUMNS.items():
        table = _table(name)
        count = 0
        for row in connection.execute(sa.select(table)).mappings().all():
            updates: dict[str, Any] = {}
            for column in text_columns:
                new_text = _to_active_text(box, row[column], f"{name}.{column}")
                if new_text is not None:
                    updates[column] = new_text
            for column in json_columns:
                new_json = _to_active_json(box, row[column], f"{name}.{column}")
                if new_json is not None:
                    updates[column] = new_json
            if updates:
                connection.execute(
                    sa.update(table).where(table.c.id == row["id"]).values(**updates)
                )
                count += 1
        changed[name] = count
    return changed


def decrypt_to_plain(connection: Connection) -> dict[str, int]:
    """Write every stored credential back in plain text. For a downgrade only."""
    box = _require_box()
    changed: dict[str, int] = {}
    for name, (text_columns, json_columns) in SECRET_COLUMNS.items():
        table = _table(name)
        count = 0
        for row in connection.execute(sa.select(table)).mappings().all():
            updates: dict[str, Any] = {}
            for column in text_columns:
                value = row[column]
                if is_sealed(value):
                    updates[column] = box.decrypt(value, f"{name}.{column}")
            for column in json_columns:
                value = row[column]
                if is_sealed_json(value):
                    updates[column] = unseal_json(value, f"{name}.{column}")
            if updates:
                connection.execute(
                    sa.update(table).where(table.c.id == row["id"]).values(**updates)
                )
                count += 1
        changed[name] = count
    return changed


def count_by_key(connection: Connection) -> dict[str, int]:
    """How many stored values sit under each key id; ``plain`` for unencrypted."""
    totals: dict[str, int] = {}
    box = get_box()
    for name, (text_columns, json_columns) in SECRET_COLUMNS.items():
        table = _table(name)
        for row in connection.execute(sa.select(table)).mappings().all():
            for column in (*text_columns, *json_columns):
                value = row[column]
                if value is None or value in ("", {}):
                    continue
                sealed = value[SEALED_JSON_KEY] if is_sealed_json(value) else value
                if is_sealed(sealed) and box is not None:
                    label = box.key_id_of(sealed)
                elif is_sealed(sealed):
                    label = "sealed"
                else:
                    label = "plain"
                totals[label] = totals.get(label, 0) + 1
    return totals
