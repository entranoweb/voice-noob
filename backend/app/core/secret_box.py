"""Encryption of customer credentials at rest.

Model, carrier and CRM keys that customers hand us are stored encrypted, so a
database dump, a backup or a read-only SQL hole does not hand out every
customer's billing-bearing keys.

Stored form
-----------
``enc:v1:<key id>:<base64url(nonce || ciphertext || tag)>``

* AES-256-GCM (authenticated: a changed value fails, it does not decrypt to
  something else).
* The key id travels with the value, so several keys can be live at once.
  Writes always use the first key in ``CREDENTIAL_ENCRYPTION_KEYS``; reads use
  whichever key the value names. That asymmetry is what makes rotation possible
  without downtime.
* The key id and the column the value belongs to are bound in as associated
  data, so a value copied into a different column, or relabelled with another
  key id, does not decrypt.

What this does not do
---------------------
* It does not bind a value to its row. The column type sees one value and not
  the primary key, so a ciphertext copied to another row of the same column
  still decrypts. Row ownership is the query layer's job.
* It keeps the key in the process environment, which is not next to the data
  but is not a key-management service either.

A value with no ``enc:v1:`` prefix is read back as it is. That is what lets an
upgrade start before the migration has encrypted the existing rows; the
migration and ``scripts/rotate_credential_keys.py`` leave none behind.
"""

import base64
import binascii
import os
import re
from dataclasses import dataclass
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "enc:v1:"
_NONCE_BYTES = 12
_KEY_BYTES = 32
_TAG_BYTES = 16
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_MASK_VISIBLE = 4
_MASK_MIN_LENGTH = 12


class CredentialKeyError(RuntimeError):
    """The credential key ring is missing or malformed."""


class CredentialDecryptError(RuntimeError):
    """A stored credential could not be decrypted. Never carries the value."""


@dataclass(frozen=True)
class _Key:
    key_id: str
    cipher: AESGCM


def parse_key_ring(raw: str | None) -> list[tuple[str, bytes]]:
    """Parse ``id:base64key,id:base64key`` into ``[(id, key bytes)]``.

    The first entry is the active key. Raises :class:`CredentialKeyError` with a
    message that names the problem and never echoes key material.
    """
    if raw is None or not raw.strip():
        return []
    entries: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for position, part in enumerate(raw.split(","), start=1):
        item = part.strip()
        if not item:
            continue
        key_id, sep, encoded = item.partition(":")
        if not sep or not _KEY_ID_RE.match(key_id):
            message = (
                f"CREDENTIAL_ENCRYPTION_KEYS entry {position} is not of the form "
                "<id>:<base64 key>; the id may use letters, digits, '-' and '_'."
            )
            raise CredentialKeyError(message)
        if key_id in seen:
            message = f"CREDENTIAL_ENCRYPTION_KEYS names the key id '{key_id}' twice."
            raise CredentialKeyError(message)
        try:
            padded = encoded + "=" * (-len(encoded) % 4)
            key = base64.urlsafe_b64decode(padded.encode("ascii"))
        except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
            message = f"CREDENTIAL_ENCRYPTION_KEYS key '{key_id}' is not valid base64."
            raise CredentialKeyError(message) from exc
        if len(key) != _KEY_BYTES:
            message = (
                f"CREDENTIAL_ENCRYPTION_KEYS key '{key_id}' must be {_KEY_BYTES} bytes "
                '(for example `python -c "import secrets,base64;'
                'print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"`).'
            )
            raise CredentialKeyError(message)
        seen.add(key_id)
        entries.append((key_id, key))
    return entries


class SecretBox:
    """Encrypts with the newest key, decrypts with any key in the ring."""

    def __init__(self, keys: list[tuple[str, bytes]]) -> None:
        if not keys:
            message = "A SecretBox needs at least one key."
            raise CredentialKeyError(message)
        self._keys = [_Key(key_id, AESGCM(key)) for key_id, key in keys]
        self._by_id = {key.key_id: key for key in self._keys}

    @property
    def active_key_id(self) -> str:
        return self._keys[0].key_id

    @property
    def key_ids(self) -> list[str]:
        return [key.key_id for key in self._keys]

    def encrypt(self, plaintext: str, context: str) -> str:
        key = self._keys[0]
        nonce = os.urandom(_NONCE_BYTES)
        sealed = key.cipher.encrypt(nonce, plaintext.encode("utf-8"), _aad(key.key_id, context))
        body = base64.urlsafe_b64encode(nonce + sealed).decode("ascii")
        return f"{PREFIX}{key.key_id}:{body}"

    def decrypt(self, token: str, context: str) -> str:
        key_id, body = _split(token)
        key = self._by_id.get(key_id)
        if key is None:
            message = (
                f"A stored credential names key id '{key_id}', which is not in "
                "CREDENTIAL_ENCRYPTION_KEYS. Put that key back in the ring."
            )
            raise CredentialDecryptError(message)
        try:
            raw = base64.urlsafe_b64decode(body.encode("ascii"))
        except (binascii.Error, ValueError) as exc:
            message = "A stored credential is not valid base64."
            raise CredentialDecryptError(message) from exc
        if len(raw) < _NONCE_BYTES + _TAG_BYTES:
            message = "A stored credential is too short to be a sealed value."
            raise CredentialDecryptError(message)
        nonce, sealed = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        try:
            return key.cipher.decrypt(nonce, sealed, _aad(key_id, context)).decode("utf-8")
        except InvalidTag as exc:
            message = (
                f"A stored credential failed authentication under key id '{key_id}': "
                "the key is wrong, or the value was changed or moved."
            )
            raise CredentialDecryptError(message) from exc

    def key_id_of(self, token: str) -> str:
        return _split(token)[0]


def _aad(key_id: str, context: str) -> bytes:
    return f"{key_id}|{context}".encode()


def _split(token: str) -> tuple[str, str]:
    if not token.startswith(PREFIX):
        message = "Not a sealed credential."
        raise CredentialDecryptError(message)
    key_id, sep, body = token[len(PREFIX) :].partition(":")
    if not sep or not key_id or not body:
        message = "A stored credential is malformed."
        raise CredentialDecryptError(message)
    return key_id, body


def is_sealed(value: object) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


@lru_cache(maxsize=1)
def _configured_box() -> SecretBox | None:
    from app.core.config import settings

    keys = parse_key_ring(settings.CREDENTIAL_ENCRYPTION_KEYS)
    return SecretBox(keys) if keys else None


_override: list[SecretBox | None] = []


def get_box() -> SecretBox | None:
    """The process's box, or None when no key is configured (DEBUG only)."""
    if _override:
        return _override[-1]
    return _configured_box()


def use_box(box: SecretBox | None) -> None:
    """Replace the process's box. For tests and the rotation script."""
    _override.clear()
    _override.append(box)


def reset_box() -> None:
    """Go back to the box built from settings."""
    _override.clear()
    _configured_box.cache_clear()


def seal(plaintext: str, context: str) -> str:
    """Encrypt for storage. With no key configured the value is stored as given."""
    box = get_box()
    if box is None:
        return plaintext
    return box.encrypt(plaintext, context)


def unseal(stored: str, context: str) -> str:
    """Decrypt a stored value. A value that was never sealed comes back as it is."""
    if not is_sealed(stored):
        return stored
    box = get_box()
    if box is None:
        message = "A stored credential is encrypted but CREDENTIAL_ENCRYPTION_KEYS is not set."
        raise CredentialDecryptError(message)
    return box.decrypt(stored, context)


def mask(value: str | None) -> str | None:
    """A display form that cannot be used as the credential: ``****abcd``."""
    if not value:
        return None
    if len(value) < _MASK_MIN_LENGTH:
        return "****"
    return f"****{value[-_MASK_VISIBLE:]}"
