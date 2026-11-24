"""Authentication API routes."""

from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditAction, record_audit
from app.core.auth import CurrentUser, SuperUser
from app.core.config import settings
from app.core.limiter import limiter
from app.db.session import get_db
from app.models.user import User

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
logger = structlog.get_logger()

# bcrypt only reads the first 72 bytes of a password. passlib, which this module
# used until November 2026, cut longer passwords to that length without saying so;
# bcrypt 5 raises instead. The cut is made here, explicitly, so every hash passlib
# wrote still verifies and a long password behaves exactly as it did before.
BCRYPT_MAX_PASSWORD_BYTES = 72


# =============================================================================
# Pydantic Models
# =============================================================================


class RegisterRequest(BaseModel):
    """User registration request."""

    email: EmailStr
    username: str  # Will be used as full_name
    password: str


class TokenResponse(BaseModel):
    """Token response."""

    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    """User response."""

    id: int
    email: str
    username: str | None = None  # Maps to full_name

    model_config = {"from_attributes": True}

    @classmethod
    def from_user(cls, user: "User") -> "UserResponse":
        """Create response from User model."""
        return cls(id=user.id, email=user.email, username=user.full_name)


# =============================================================================
# Helper Functions
# =============================================================================


def _password_bytes(password: str) -> bytes:
    """UTF-8 bytes of a password, cut to the length bcrypt actually reads."""
    return password.encode("utf-8")[:BCRYPT_MAX_PASSWORD_BYTES]


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its bcrypt hash.

    A stored value that is not a bcrypt hash, or a password bcrypt cannot take
    (one containing a NUL byte), is a failed match rather than an error: a login
    attempt must answer "wrong password", never a 500.
    """
    try:
        return bcrypt.checkpw(_password_bytes(plain_password), hashed_password.encode("utf-8"))
    except ValueError:
        return False


def get_password_hash(password: str) -> str:
    """Hash a password with bcrypt (``$2b$``, the cost passlib used: 12)."""
    return bcrypt.hashpw(_password_bytes(password), bcrypt.gensalt(rounds=12)).decode("utf-8")


def create_access_token(subject: str | int, expires_delta: timedelta | None = None) -> str:
    """Create a JWT access token.

    Args:
        subject: The subject of the token (user ID or email)
        expires_delta: Optional custom expiration time. Defaults to ACCESS_TOKEN_EXPIRE_MINUTES.

    Returns:
        Encoded JWT token string
    """
    if expires_delta:
        expire = datetime.now(UTC) + expires_delta
    else:
        expire = datetime.now(UTC) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode = {"sub": str(subject), "exp": expire}
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


# =============================================================================
# Auth Endpoints
# =============================================================================


@router.post("/register", response_model=UserResponse)
@limiter.limit("5/minute")  # Strict rate limit to prevent account spam
async def register(
    request: Request,  # Must be named `request` and be a Starlette Request: slowapi
    # resolves the rate-limit key from this parameter by name, then asserts its type.
    payload: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Register a new user.

    Args:
        request: Incoming HTTP request (used by the rate limiter)
        payload: Registration payload with email, username, password
        db: Database session

    Returns:
        Created user
    """
    # Open sign-up is a development convenience. In production an account is a
    # tenant with access to carrier and model credentials, and the administrator
    # creates it (POST /api/v1/auth/users).
    if not settings.public_signup_allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sign-up is closed. Ask the administrator for an account.",
        )

    user = await _create_user(payload, db)
    return UserResponse.from_user(user)


async def _create_user(payload: RegisterRequest, db: AsyncSession) -> User:
    """Create an account from a registration payload; 400 when the email is taken."""
    log = logger.bind(username=payload.username)
    log.info("registering_user")

    # Check if email already exists
    result = await db.execute(select(User).where(User.email == payload.email))
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    # Create user (username is stored as full_name)
    user = User(
        email=payload.email,
        full_name=payload.username,
        hashed_password=get_password_hash(payload.password),
    )
    db.add(user)
    await db.flush()
    await record_audit(
        db,
        AuditAction.REGISTER,
        actor_user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        request=request,
    )
    await db.commit()
    await db.refresh(user)

    log.info("user_registered", user_id=user.id)
    return user


@router.post("/login", response_model=TokenResponse)
@limiter.limit("10/minute")  # Strict rate limit to prevent brute force attacks
async def login(
    request: Request,  # Required for rate limiter
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Login and get an access token.

    Args:
        form_data: OAuth2 form with username (email) and password
        db: Database session

    Returns:
        Access token
    """
    log = logger.bind(username=form_data.username)
    log.info("login_attempt")

    # Find user by email
    result = await db.execute(select(User).where(User.email == form_data.username))
    user = result.scalar_one_or_none()

    # A disabled account gets the same answer as a wrong password: no token,
    # and nothing that tells the caller the account exists.
    if (
        not user
        or not verify_password(form_data.password, user.hashed_password)
        or not user.is_active
    ):
        log.warning("login_failed")
        # The account the attempt was aimed at, when there is one, so its owner
        # can see failed attempts in their own trail. Never the password.
        await record_audit(
            db,
            AuditAction.LOGIN_FAILED,
            actor_user_id=user.id if user else None,
            target_type="user",
            target_id=str(user.id) if user else None,
            success=False,
            request=request,
            commit=True,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = create_access_token(user.id)
    await record_audit(
        db,
        AuditAction.LOGIN_SUCCESS,
        actor_user_id=user.id,
        target_type="user",
        target_id=str(user.id),
        request=request,
        commit=True,
    )
    log.info("login_success", user_id=user.id)

    return TokenResponse(access_token=access_token)


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(current_user: CurrentUser) -> UserResponse:
    """Get current user information.

    Args:
        current_user: Authenticated user

    Returns:
        User information
    """
    return UserResponse.from_user(current_user)


# =============================================================================
# Account administration (platform administrator only)
# =============================================================================


class AccountStatusResponse(BaseModel):
    """An account and whether it may sign in."""

    id: int
    email: str
    is_active: bool


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_account(
    payload: RegisterRequest,
    admin: SuperUser,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Create an account. This is how accounts are made while sign-up is closed."""
    user = await _create_user(payload, db)
    logger.warning("account_created_by_admin", admin_id=admin.id, user_id=user.id)
    return UserResponse.from_user(user)


async def _set_account_active(
    user_id: int, active: bool, admin: User, db: AsyncSession
) -> AccountStatusResponse:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.id == admin.id and not active:
        # The last way in must not lock itself out by accident.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An administrator cannot disable their own account",
        )
    user.is_active = active
    await db.commit()
    logger.warning(
        "account_enabled" if active else "account_disabled",
        admin_id=admin.id,
        user_id=user.id,
    )
    return AccountStatusResponse(id=user.id, email=user.email, is_active=user.is_active)


@router.post("/users/{user_id}/disable", response_model=AccountStatusResponse)
async def disable_account(
    user_id: int,
    admin: SuperUser,
    db: AsyncSession = Depends(get_db),
) -> AccountStatusResponse:
    """Switch an account off. Takes effect on that account's next request:
    every authenticated request re-reads the account, so an unexpired token
    stops working at once."""
    return await _set_account_active(user_id, False, admin, db)


@router.post("/users/{user_id}/enable", response_model=AccountStatusResponse)
async def enable_account(
    user_id: int,
    admin: SuperUser,
    db: AsyncSession = Depends(get_db),
) -> AccountStatusResponse:
    """Switch a disabled account back on."""
    return await _set_account_active(user_id, True, admin, db)
