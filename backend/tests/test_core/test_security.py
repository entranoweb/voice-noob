"""Tests for security and authentication utilities."""

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.api.auth import create_access_token, get_password_hash, verify_password
from app.core.auth import get_current_user
from app.core.config import settings


class TestPasswordHashing:
    """Test password hashing functionality."""

    def test_hash_password(self) -> None:
        """Test password hashing produces different hash from plaintext."""
        password = "supersecretpassword123"
        hashed = get_password_hash(password)

        assert hashed != password
        assert len(hashed) > 0
        assert hashed.startswith("$2b$")  # bcrypt hash prefix

    def test_hash_password_deterministic(self) -> None:
        """Test that hashing same password twice produces different hashes (due to salt)."""
        password = "supersecretpassword123"
        hash1 = get_password_hash(password)
        hash2 = get_password_hash(password)

        # Hashes should be different due to different salts
        assert hash1 != hash2

    def test_hash_empty_password(self) -> None:
        """Test hashing empty password."""
        password = ""
        hashed = get_password_hash(password)

        assert hashed != password
        assert len(hashed) > 0

    def test_hash_long_password(self) -> None:
        """Test hashing long password (bcrypt has 72 byte limit)."""
        # Bcrypt truncates passwords to 72 bytes, so we test within that limit
        password = "a" * 70
        hashed = get_password_hash(password)

        assert hashed != password
        assert len(hashed) > 0


class TestPasswordVerification:
    """Test password verification functionality."""

    def test_verify_password_success(self) -> None:
        """Test successful password verification."""
        password = "correctpassword123"
        hashed = get_password_hash(password)

        assert verify_password(password, hashed) is True

    def test_verify_password_failure(self) -> None:
        """Test failed password verification with wrong password."""
        password = "correctpassword123"
        wrong_password = "wrongpassword456"
        hashed = get_password_hash(password)

        assert verify_password(wrong_password, hashed) is False

    def test_verify_password_empty(self) -> None:
        """Test verifying empty password."""
        password = ""
        hashed = get_password_hash(password)

        assert verify_password(password, hashed) is True
        assert verify_password("nonempty", hashed) is False

    def test_verify_password_case_sensitive(self) -> None:
        """Test that password verification is case-sensitive."""
        password = "Password123"
        hashed = get_password_hash(password)

        assert verify_password("password123", hashed) is False
        assert verify_password("PASSWORD123", hashed) is False
        assert verify_password("Password123", hashed) is True

    def test_verify_password_with_special_characters(self) -> None:
        """Test password verification with special characters."""
        password = "P@ssw0rd!#$%^&*()"
        hashed = get_password_hash(password)

        assert verify_password(password, hashed) is True
        assert verify_password("P@ssw0rd!#$%^&*", hashed) is False


class TestJWTTokenCreation:
    """Test JWT token creation."""

    def test_create_access_token_default_expiry(self) -> None:
        """Test creating access token with default expiration."""
        subject = "user@example.com"
        token = create_access_token(subject)

        assert token is not None
        assert isinstance(token, str)
        assert len(token) > 0

        # Decode and verify
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        assert payload["sub"] == subject
        assert "exp" in payload

    def test_create_access_token_custom_expiry(self) -> None:
        """Test creating access token with custom expiration."""
        subject = "user@example.com"
        expires_delta = timedelta(minutes=15)
        token = create_access_token(subject, expires_delta)

        assert token is not None
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        assert payload["sub"] == subject

        # Verify expiration time is approximately correct (within 1 minute tolerance)
        exp_time = datetime.fromtimestamp(payload["exp"], tz=UTC)
        expected_exp = datetime.now(UTC) + expires_delta
        time_diff = abs((exp_time - expected_exp).total_seconds())
        assert time_diff < 60  # Within 1 minute

    def test_create_access_token_integer_subject(self) -> None:
        """Test creating access token with integer subject (user ID)."""
        subject = 12345
        token = create_access_token(subject)

        assert token is not None
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        assert payload["sub"] == str(subject)  # Should be converted to string

    def test_create_access_token_different_subjects(self) -> None:
        """Test that different subjects produce different tokens."""
        token1 = create_access_token("user1@example.com")
        token2 = create_access_token("user2@example.com")

        assert token1 != token2

        payload1 = jwt.decode(token1, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        payload2 = jwt.decode(token2, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])

        assert payload1["sub"] != payload2["sub"]


class TestJWTTokenValidation:
    """Test JWT token validation."""

    def test_decode_valid_token(self) -> None:
        """Test decoding a valid token."""
        subject = "user@example.com"
        token = create_access_token(subject)

        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])

        assert payload["sub"] == subject
        assert "exp" in payload

    def test_decode_token_with_wrong_secret(self) -> None:
        """Test decoding token with wrong secret fails."""
        subject = "user@example.com"
        token = create_access_token(subject)

        with pytest.raises(jwt.PyJWTError):
            jwt.decode(token, "wrong-secret-key", algorithms=[settings.ALGORITHM])

    def test_decode_token_with_wrong_algorithm(self) -> None:
        """Test decoding token with wrong algorithm fails."""
        subject = "user@example.com"
        token = create_access_token(subject)

        with pytest.raises(jwt.PyJWTError):
            jwt.decode(token, settings.SECRET_KEY, algorithms=["HS512"])

    def test_decode_expired_token(self) -> None:
        """Test decoding expired token fails."""
        subject = "user@example.com"
        # Create token that expired 1 hour ago
        expires_delta = timedelta(hours=-1)
        token = create_access_token(subject, expires_delta)

        with pytest.raises(jwt.ExpiredSignatureError):
            jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])

    def test_decode_malformed_token(self) -> None:
        """Test decoding malformed token fails."""
        malformed_token = "not.a.valid.jwt.token"

        with pytest.raises(jwt.PyJWTError):
            jwt.decode(malformed_token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])

    def test_token_expiration_time(self) -> None:
        """Test that token expiration is set correctly."""
        subject = "user@example.com"
        token = create_access_token(subject)

        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        exp_time = datetime.fromtimestamp(payload["exp"], tz=UTC)
        expected_exp = datetime.now(UTC) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

        # Should be within 1 minute of expected expiration
        time_diff = abs((exp_time - expected_exp).total_seconds())
        assert time_diff < 60


class TestSecurityIntegration:
    """Integration tests for security features."""

    def test_password_hash_and_verify_workflow(self) -> None:
        """Test complete workflow of hashing and verifying password."""
        original_password = "MySecurePassword123!"

        # User registration: hash password
        hashed_password = get_password_hash(original_password)

        # User login: verify password
        is_valid = verify_password(original_password, hashed_password)
        assert is_valid is True

        # Failed login: wrong password
        is_invalid = verify_password("WrongPassword", hashed_password)
        assert is_invalid is False

    def test_create_and_decode_token_workflow(self) -> None:
        """Test complete workflow of creating and decoding token."""
        user_id = 42

        # Create token for user
        token = create_access_token(user_id)

        # Decode and verify token
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        assert payload["sub"] == str(user_id)

        # Verify token hasn't expired
        exp_time = datetime.fromtimestamp(payload["exp"], tz=UTC)
        assert exp_time > datetime.now(UTC)

    def test_multiple_users_unique_tokens(self) -> None:
        """Test that multiple users get unique tokens."""
        users = ["user1@example.com", "user2@example.com", "user3@example.com"]
        tokens = [create_access_token(user) for user in users]

        # All tokens should be unique
        assert len(tokens) == len(set(tokens))

        # Each token should decode to correct user
        for user, token in zip(users, tokens, strict=False):
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
            assert payload["sub"] == user


# A hash and its password produced by the passlib code path this module used
# until November 2026 (CryptContext(schemes=["bcrypt"]) on bcrypt 4.3.0). Stored
# users carry hashes like these, so they must keep verifying after the swap.
PASSLIB_ERA_PASSWORD = "correct horse battery staple"
PASSLIB_ERA_HASH = "$2b$12$qCLJSaYtUUWLpJW7eb5nXuBkW2j.lerQ5elQw3WaBIVuzDtqBVeLi"
# passlib hashed this 80-character password; bcrypt read only its first 72 bytes.
PASSLIB_ERA_LONG_PASSWORD = "x" * 80
PASSLIB_ERA_LONG_HASH = "$2b$12$wtcEKDw8U7wh.n7ahKDeEOw3afjUAWOMsCqQ9/ac2YWCFhfzUu/mW"


class TestPasslibReplacement:
    """The move from passlib to bcrypt must not lock out an existing user."""

    def test_hash_written_by_passlib_still_verifies(self) -> None:
        assert verify_password(PASSLIB_ERA_PASSWORD, PASSLIB_ERA_HASH) is True
        assert verify_password("correct horse battery stapler", PASSLIB_ERA_HASH) is False

    def test_new_hash_is_bcrypt_2b_cost_12(self) -> None:
        assert get_password_hash("anything").startswith("$2b$12$")

    def test_long_password_is_cut_at_72_bytes_as_passlib_did(self) -> None:
        # The old hash of an 80-character password verifies with that password...
        assert verify_password(PASSLIB_ERA_LONG_PASSWORD, PASSLIB_ERA_LONG_HASH) is True
        # ...and, because only 72 bytes were ever read, with its 72-byte prefix too.
        assert verify_password("x" * 72, PASSLIB_ERA_LONG_HASH) is True
        assert verify_password("x" * 71, PASSLIB_ERA_LONG_HASH) is False

    def test_long_password_hashes_without_raising(self) -> None:
        # bcrypt 5 raises ValueError past 72 bytes; the explicit cut prevents a 500.
        long_password = "p" * 200
        hashed = get_password_hash(long_password)
        assert verify_password(long_password, hashed) is True

    def test_multibyte_password_is_cut_on_bytes_not_characters(self) -> None:
        password = "é" * 40  # 80 bytes in UTF-8
        hashed = get_password_hash(password)
        assert verify_password(password, hashed) is True
        assert verify_password("é" * 36, hashed) is True  # the same first 72 bytes
        assert verify_password("é" * 35, hashed) is False

    def test_stored_value_that_is_not_a_hash_is_a_failed_match(self) -> None:
        assert verify_password("anything", "not-a-bcrypt-hash") is False
        assert verify_password("anything", "") is False

    def test_password_with_nul_byte_is_a_failed_match(self) -> None:
        assert verify_password("a\x00b", PASSLIB_ERA_HASH) is False


class TestTokenRejection:
    """Every bad token is the same 401 from the dependency the routes use."""

    @staticmethod
    async def _status_for(token: str) -> int:
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        with pytest.raises(HTTPException) as raised:
            # A rejected token never reaches the database, so none is supplied.
            await get_current_user(credentials, None)  # type: ignore[arg-type]
        assert raised.value.headers == {"WWW-Authenticate": "Bearer"}
        return raised.value.status_code

    def test_round_trip_carries_subject_and_expiry(self) -> None:
        token = create_access_token(42)
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        assert payload["sub"] == "42"
        assert set(payload) == {"sub", "exp"}
        assert jwt.get_unverified_header(token)["alg"] == "HS256"

    async def test_expired_token_is_401(self) -> None:
        assert await self._status_for(create_access_token(42, timedelta(minutes=-5))) == 401

    async def test_tampered_token_is_401(self) -> None:
        header, payload, signature = create_access_token(42).split(".")
        forged_payload = (
            jwt.encode({"sub": "1", "exp": datetime.now(UTC) + timedelta(hours=1)}, "x" * 32)
        ).split(".")[1]
        assert payload != forged_payload
        assert await self._status_for(f"{header}.{forged_payload}.{signature}") == 401

    async def test_token_signed_with_another_secret_is_401(self) -> None:
        token = jwt.encode(
            {"sub": "42", "exp": datetime.now(UTC) + timedelta(hours=1)},
            "another-secret-of-sufficient-length-000",
            algorithm="HS256",
        )
        assert await self._status_for(token) == 401

    async def test_token_with_another_algorithm_is_401(self) -> None:
        token = jwt.encode(
            {"sub": "42", "exp": datetime.now(UTC) + timedelta(hours=1)},
            settings.SECRET_KEY.ljust(64, "0"),
            algorithm="HS512",
        )
        assert await self._status_for(token) == 401

    async def test_unsigned_token_is_401(self) -> None:
        token = jwt.encode(
            {"sub": "42", "exp": datetime.now(UTC) + timedelta(hours=1)}, None, algorithm="none"
        )
        assert await self._status_for(token) == 401

    async def test_malformed_token_is_401(self) -> None:
        assert await self._status_for("not.a.valid.jwt.token") == 401

    async def test_token_without_subject_is_401(self) -> None:
        token = jwt.encode(
            {"exp": datetime.now(UTC) + timedelta(hours=1)},
            settings.SECRET_KEY,
            algorithm=settings.ALGORITHM,
        )
        assert await self._status_for(token) == 401
