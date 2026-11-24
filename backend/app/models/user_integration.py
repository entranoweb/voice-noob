"""User integration credentials model."""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.encrypted_types import EncryptedJSON, EncryptedText

if TYPE_CHECKING:
    from app.models.workspace import Workspace


class UserIntegration(Base):
    """User's connected integration credentials.

    Credentials for external integrations (OAuth tokens, API keys, etc.), encrypted
    at rest by the column types.
    """

    __tablename__ = "user_integrations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)

    # Workspace reference (for data isolation between clients/workspaces)
    # Different workspaces can have different integration credentials
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="Workspace this integration belongs to (null = user-level)",
    )

    integration_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
        comment="Integration slug (e.g., 'hubspot', 'slack')",
    )
    integration_name: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="Display name (e.g., 'HubSpot', 'Slack')"
    )

    # Credentials storage (encrypted at rest)
    credentials: Mapped[dict[str, Any]] = mapped_column(
        EncryptedJSON("user_integrations.credentials"),
        nullable=False,
        comment="Encrypted credentials (access_token, api_key, etc.)",
    )

    # Connection metadata
    is_active: Mapped[bool] = mapped_column(
        default=True, comment="Whether integration is currently active"
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="Last time integration was used"
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="OAuth token expiration (if applicable)",
    )

    # OAuth refresh (if applicable)
    refresh_token: Mapped[str | None] = mapped_column(
        EncryptedText("user_integrations.refresh_token"),
        nullable=True,
        comment="OAuth refresh token (encrypted)",
    )

    # Additional metadata
    integration_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="Additional integration-specific metadata"
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    # Relationships
    workspace: Mapped["Workspace | None"] = relationship("Workspace", lazy="selectin")

    def __repr__(self) -> str:
        return (
            f"<UserIntegration(id={self.id}, user_id={self.user_id}, "
            f"integration={self.integration_id}, active={self.is_active})>"
        )
