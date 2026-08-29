"""Call record model for telephony call history."""

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.agent import Agent
    from app.models.call_evaluation import CallEvaluation
    from app.models.contact import Contact
    from app.models.workspace import Workspace


class CallDirection(str, Enum):
    """Call direction."""

    INBOUND = "inbound"
    OUTBOUND = "outbound"


class CallStatus(str, Enum):
    """Call status."""

    INITIATED = "initiated"
    RINGING = "ringing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BUSY = "busy"
    NO_ANSWER = "no_answer"
    CANCELED = "canceled"


class CallRecord(Base):
    """Telephony call record.

    Stores details of each phone call made or received via Twilio/Telnyx.
    """

    __tablename__ = "call_records"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True, comment="Owner user ID"
    )

    # Provider call identifiers
    provider: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="Telephony provider: twilio or telnyx"
    )
    provider_call_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
        comment="Provider call ID (CallSid for Twilio, call_control_id for Telnyx)",
    )

    # Agent reference
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Agent that handled the call",
    )

    # Contact reference (if call was to/from a CRM contact)
    contact_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="CRM contact if applicable",
    )

    # Workspace reference (for data isolation between clients/workspaces)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="Workspace this call belongs to",
    )

    # Call details
    direction: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="Call direction: inbound or outbound"
    )
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=CallStatus.INITIATED.value,
        comment="Call status",
    )
    from_number: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="Caller phone number"
    )
    to_number: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="Recipient phone number"
    )

    # Call metrics
    duration_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="Call duration in seconds"
    )

    # Recording and transcript
    recording_url: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="URL to call recording"
    )
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True, comment="Call transcript")

    # Per-turn timings recorded off the live audio bridge: what each side said,
    # what was heard, time to first audio byte, and whether the agent was talked
    # over. This is what the three audio metrics read. Null means no audio was
    # recorded for this call, which those metrics report as not measurable —
    # distinct from an empty list, which would mean a call with no turns.
    # JSONB on Postgres, which is what this deploys against and what the
    # migration creates; plain JSON elsewhere, so the property tests that build
    # their own SQLite database can still compile the table.
    turns: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=True,
        comment="Recorded conversational turns with audio timings",
    )

    # Timestamps
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
        comment="When the call was initiated",
    )
    answered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When the call was answered"
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When the call ended"
    )
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
    agent: Mapped["Agent | None"] = relationship("Agent", lazy="selectin")
    contact: Mapped["Contact | None"] = relationship("Contact", lazy="selectin")
    workspace: Mapped["Workspace | None"] = relationship("Workspace", lazy="selectin")
    evaluation: Mapped["CallEvaluation | None"] = relationship(
        "CallEvaluation", back_populates="call_record", uselist=False, lazy="selectin"
    )

    def __repr__(self) -> str:
        return (
            f"<CallRecord(id={self.id}, direction={self.direction}, "
            f"status={self.status}, from={self.from_number}, to={self.to_number})>"
        )
