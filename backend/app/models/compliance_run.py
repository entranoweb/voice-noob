"""Record of each data-protection operation that was actually run."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

KIND_EXPORT = "export"
KIND_ERASURE = "erasure"
KIND_RETENTION = "retention"

STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

TRIGGER_USER = "user"
TRIGGER_SCHEDULE = "schedule"


class ComplianceRun(Base):
    """One export, erasure or retention sweep, with how it ended.

    The compliance status page reads these, so what it reports is what
    happened and not what the code is meant to do. No foreign key to the user:
    the record of an erasure outlives the data it erased.
    """

    __tablename__ = "compliance_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False, default=TRIGGER_USER)
    user_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True, comment="Account covered; null means every account"
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_RUNNING)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    counts: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="Exception type only; never data"
    )

    def __repr__(self) -> str:
        return f"<ComplianceRun(kind={self.kind}, status={self.status})>"
