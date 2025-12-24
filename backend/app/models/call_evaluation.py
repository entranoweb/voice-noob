"""CallEvaluation model for QA Testing Framework.

Stores post-call evaluation results from Claude API analysis.
"""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.agent import Agent
    from app.models.call_record import CallRecord
    from app.models.workspace import Workspace


class CallEvaluation(Base):
    """Post-call QA evaluation results.

    Stores Claude API evaluation of call quality including:
    - Core scores (overall, intent completion, tool usage, compliance)
    - Quality metrics (coherence, relevance, groundedness, fluency)
    - Sentiment analysis (overall, progression, escalation risk)
    - Latency tracking (p50, p90, p95 response times)
    - Turn-by-turn analysis and recommendations
    """

    __tablename__ = "call_evaluations"

    # Primary key
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Foreign keys
    call_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("call_records.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
        comment="Reference to call_records.id",
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Agent that handled the call",
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="Workspace for data isolation",
    )

    # Core scores (0-100)
    overall_score: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True, comment="Overall evaluation score (0-100)"
    )
    intent_completion: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Intent completion score (0-100)"
    )
    tool_usage: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Tool usage score (0-100)"
    )
    compliance: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Compliance score (0-100)"
    )
    response_quality: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Response quality score (0-100)"
    )

    # Pass/Fail
    passed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, index=True, comment="Whether evaluation passed threshold"
    )

    # Quality metrics (Promptflow pattern)
    coherence: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Coherence score (0-100)"
    )
    relevance: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Relevance score (0-100)"
    )
    groundedness: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Groundedness score (0-100)"
    )
    fluency: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Fluency score (0-100)"
    )

    # Sentiment fields
    overall_sentiment: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="Overall sentiment: positive, negative, neutral"
    )
    sentiment_score: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="Sentiment score (-1.0 to 1.0)"
    )
    sentiment_progression: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="Sentiment changes throughout call"
    )
    escalation_risk: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="Escalation risk score (0.0 to 1.0)"
    )

    # Latency tracking (Retell pattern)
    latency_p50_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="50th percentile response latency in ms"
    )
    latency_p90_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="90th percentile response latency in ms"
    )
    latency_p95_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="95th percentile response latency in ms"
    )

    # Audio quality
    audio_quality_score: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Audio quality score (0-100)"
    )
    background_noise_detected: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, comment="Whether significant background noise was detected"
    )
    vad_metrics: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="Voice activity detection metrics"
    )

    # Analysis JSONB fields
    objectives_detected: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, comment="List of detected caller objectives"
    )
    objectives_completed: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, comment="List of completed objectives"
    )
    failure_reasons: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, comment="List of failure reasons if evaluation failed"
    )
    recommendations: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, comment="List of improvement recommendations"
    )
    turn_analysis: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON, nullable=True, comment="Per-turn analysis data"
    )
    criteria_scores: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="Detailed scoring by criteria (LlamaIndex pattern)"
    )

    # Evaluation metadata
    evaluation_model: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Model used for evaluation (e.g., claude-sonnet-4-20250514)",
    )
    evaluation_latency_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Time taken for evaluation in ms"
    )
    evaluation_cost_cents: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="Cost of evaluation in cents"
    )
    evaluation_prompt_version: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="Version of evaluation prompt used"
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    # Relationships
    call_record: Mapped["CallRecord"] = relationship("CallRecord", back_populates="evaluation")
    agent: Mapped["Agent | None"] = relationship("Agent", lazy="selectin")
    workspace: Mapped["Workspace | None"] = relationship("Workspace", lazy="selectin")

    def __repr__(self) -> str:
        return (
            f"<CallEvaluation(id={self.id}, call_id={self.call_id}, "
            f"score={self.overall_score}, passed={self.passed})>"
        )
