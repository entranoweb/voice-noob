"""Append-only audit trail."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Uuid, event
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuditEvent(Base):
    """One security-relevant action: who did what to which thing, and when.

    Rows are written once and never changed. There is deliberately no foreign
    key to the user or the workspace: the record of an erasure has to survive
    the erasure.
    """

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False, index=True
    )
    actor_user_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True, comment="User who acted; null for the system"
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    def __repr__(self) -> str:
        return f"<AuditEvent(action={self.action}, actor={self.actor_user_id})>"


class AuditEventImmutableError(RuntimeError):
    """Something tried to change or remove an audit row through the ORM."""


def _refuse_change(*_args: Any) -> None:
    message = "Audit events are append-only: they are never updated or deleted."
    raise AuditEventImmutableError(message)


event.listen(AuditEvent, "before_update", _refuse_change)
event.listen(AuditEvent, "before_delete", _refuse_change)
