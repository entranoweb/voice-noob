"""Encryption of customer credentials: the cipher, the key ring, the start-up check."""

import base64

import pytest

from app.core.config import (
    MissingCredentialKeyError,
    Settings,
    refuse_missing_credential_keys,
)
from app.core.secret_box import (
    PREFIX,
    CredentialDecryptError,
    CredentialKeyError,
    SecretBox,
    mask,
    parse_key_ring,
)

KEY_A = b"a" * 32
KEY_B = b"b" * 32
CONTEXT = "user_settings.openai_api_key"
SECRET = "sk-live-this-is-a-customer-key-0001"


def _encoded(key: bytes) -> str:
    return base64.urlsafe_b64encode(key).decode()


def test_round_trip_and_stored_form() -> None:
    box = SecretBox([("k1", KEY_A)])

    token = box.encrypt(SECRET, CONTEXT)

    assert token.startswith(f"{PREFIX}k1:")
    assert SECRET not in token
    assert box.decrypt(token, CONTEXT) == SECRET
    # Random nonce: the same value never encrypts to the same text twice.
    assert box.encrypt(SECRET, CONTEXT) != token


def test_rotation_reads_old_key_and_writes_new_key() -> None:
    old = SecretBox([("k1", KEY_A)])
    token_under_old = old.encrypt(SECRET, CONTEXT)

    rotated = SecretBox([("k2", KEY_B), ("k1", KEY_A)])

    assert rotated.decrypt(token_under_old, CONTEXT) == SECRET
    token_under_new = rotated.encrypt(SECRET, CONTEXT)
    assert rotated.key_id_of(token_under_new) == "k2"
    # Once the old key is dropped, only values moved to the new key still read.
    new_only = SecretBox([("k2", KEY_B)])
    assert new_only.decrypt(token_under_new, CONTEXT) == SECRET
    with pytest.raises(CredentialDecryptError, match="k1"):
        new_only.decrypt(token_under_old, CONTEXT)


def test_wrong_key_under_the_same_id_fails_closed() -> None:
    token = SecretBox([("k1", KEY_A)]).encrypt(SECRET, CONTEXT)

    with pytest.raises(CredentialDecryptError) as excinfo:
        SecretBox([("k1", KEY_B)]).decrypt(token, CONTEXT)

    assert SECRET not in str(excinfo.value)


def test_changed_or_moved_value_fails() -> None:
    box = SecretBox([("k1", KEY_A)])
    token = box.encrypt(SECRET, CONTEXT)

    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(CredentialDecryptError):
        box.decrypt(tampered, CONTEXT)
    # The same ciphertext in another column does not decrypt.
    with pytest.raises(CredentialDecryptError):
        box.decrypt(token, "user_settings.telnyx_api_key")


def test_key_ring_parsing() -> None:
    ring = parse_key_ring(f"k2:{_encoded(KEY_B)}, k1:{_encoded(KEY_A)}")

    assert [key_id for key_id, _ in ring] == ["k2", "k1"]
    assert parse_key_ring(None) == []
    assert parse_key_ring("  ") == []


@pytest.mark.parametrize(
    "raw",
    [
        "no-colon-here",
        "k1:not base64 at all!!",
        f"k1:{base64.urlsafe_b64encode(b'too-short').decode()}",
        f"k1:{_encoded(KEY_A)},k1:{_encoded(KEY_B)}",
        f"bad id:{_encoded(KEY_A)}",
    ],
)
def test_malformed_key_ring_is_refused_without_echoing_keys(raw: str) -> None:
    with pytest.raises(CredentialKeyError) as excinfo:
        parse_key_ring(raw)

    assert _encoded(KEY_A) not in str(excinfo.value)


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "SECRET_KEY": "x" * 40,
        "DEBUG": False,
        "CREDENTIAL_ENCRYPTION_KEYS": None,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_start_is_refused_in_production_without_a_key() -> None:
    with pytest.raises(MissingCredentialKeyError, match="CREDENTIAL_ENCRYPTION_KEYS"):
        refuse_missing_credential_keys(_settings())


def test_start_is_allowed_with_a_key_or_in_debug() -> None:
    refuse_missing_credential_keys(_settings(CREDENTIAL_ENCRYPTION_KEYS=f"k1:{_encoded(KEY_A)}"))
    refuse_missing_credential_keys(_settings(DEBUG=True))


def test_malformed_key_is_refused_even_in_debug() -> None:
    with pytest.raises(MissingCredentialKeyError) as excinfo:
        refuse_missing_credential_keys(
            _settings(DEBUG=True, CREDENTIAL_ENCRYPTION_KEYS="k1:c2hvcnQ=")
        )

    assert "c2hvcnQ=" not in str(excinfo.value)


def test_mask_never_returns_a_usable_value() -> None:
    assert mask(SECRET) == "****0001"
    assert mask("short") == "****"
    assert mask(None) is None
    assert mask("") is None
