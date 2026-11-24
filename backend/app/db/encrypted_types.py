"""Column types that encrypt on the way in and decrypt on the way out.

The rest of the application reads and writes plain values; only the database
sees ciphertext. See ``app.core.secret_box`` for the stored form.

Two things follow from random encryption and are worth knowing before using
these types on a new column:

* An equality filter on the column (``WHERE col = :value``) finds nothing, and
  says nothing. Filter on another column.
* ``context`` names the column (``"table.column"``). It is bound into the
  ciphertext, so it must never change for a column that already holds data.
"""

import json
from typing import Any

from sqlalchemy import JSON, Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from app.core.secret_box import get_box, is_sealed, seal, unseal

# The one key of the JSON object that holds a sealed credentials document.
SEALED_JSON_KEY = "_enc"


class EncryptedText(TypeDecorator[str]):
    """A TEXT column whose value is encrypted at rest."""

    impl = Text
    cache_ok = True

    # Positional on purpose: SQLAlchemy builds a type's cache key from its
    # constructor's positional-or-keyword parameters.
    def __init__(self, context: str) -> None:
        super().__init__()
        self.context = context

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:  # noqa: ARG002
        if value is None:
            return None
        return seal(value, self.context)

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:  # noqa: ARG002
        if value is None:
            return None
        return unseal(value, self.context)


class EncryptedJSON(TypeDecorator[dict[str, Any]]):
    """A JSON column whose whole document is encrypted at rest.

    Stored as ``{"_enc": "enc:v1:..."}`` so the column stays JSON and no schema
    change is needed. A document without that key was written before encryption
    and is read back as it is.
    """

    impl = JSON
    cache_ok = True

    def __init__(self, context: str) -> None:
        super().__init__()
        self.context = context

    def process_bind_param(
        self,
        value: dict[str, Any] | None,
        dialect: Dialect,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        if get_box() is None:
            return value
        return seal_json(value, self.context)

    def process_result_value(
        self,
        value: dict[str, Any] | None,
        dialect: Dialect,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        return unseal_json(value, self.context)


def is_sealed_json(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value.keys()) == {SEALED_JSON_KEY}
        and is_sealed(value[SEALED_JSON_KEY])
    )


def seal_json(value: dict[str, Any], context: str) -> dict[str, Any]:
    return {SEALED_JSON_KEY: seal(json.dumps(value, sort_keys=True), context)}


def unseal_json(value: dict[str, Any], context: str) -> dict[str, Any]:
    if not is_sealed_json(value):
        return value
    loaded = json.loads(unseal(value[SEALED_JSON_KEY], context))
    if not isinstance(loaded, dict):
        message = "A sealed credentials document did not contain an object."
        raise TypeError(message)
    return loaded
