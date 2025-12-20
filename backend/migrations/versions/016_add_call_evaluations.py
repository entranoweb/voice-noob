"""Add call_evaluations table for QA Testing Framework.

Revision ID: 016_call_evaluations
Revises: 015_azure_openai
Create Date: 2025-12-19

This migration creates the call_evaluations table for storing
post-call QA evaluation results from Claude API analysis.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "016_call_evaluations"
down_revision: str | None = "015_azure_openai"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create call_evaluations table."""
    op.create_table(
        "call_evaluations",
        # Primary key
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        # Foreign keys
        sa.Column(
            "call_id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            comment="Reference to call_records.id",
        ),
        sa.Column(
            "agent_id",
            sa.Uuid(as_uuid=True),
            nullable=True,
            comment="Agent that handled the call",
        ),
        sa.Column(
            "workspace_id",
            sa.Uuid(as_uuid=True),
            nullable=True,
            comment="Workspace for data isolation",
        ),
        # Core scores (0-100)
        sa.Column(
            "overall_score",
            sa.Integer(),
            nullable=False,
            comment="Overall evaluation score (0-100)",
        ),
        sa.Column(
            "intent_completion",
            sa.Integer(),
            nullable=True,
            comment="Intent completion score (0-100)",
        ),
        sa.Column(
            "tool_usage",
            sa.Integer(),
            nullable=True,
            comment="Tool usage score (0-100)",
        ),
        sa.Column(
            "compliance",
            sa.Integer(),
            nullable=True,
            comment="Compliance score (0-100)",
        ),
        sa.Column(
            "response_quality",
            sa.Integer(),
            nullable=True,
            comment="Response quality score (0-100)",
        ),
        # Pass/Fail
        sa.Column(
            "passed",
            sa.Boolean(),
            nullable=False,
            comment="Whether evaluation passed threshold",
        ),
        # Quality metrics (Promptflow pattern)
        sa.Column(
            "coherence",
            sa.Integer(),
            nullable=True,
            comment="Coherence score (0-100)",
        ),
        sa.Column(
            "relevance",
            sa.Integer(),
            nullable=True,
            comment="Relevance score (0-100)",
        ),
        sa.Column(
            "groundedness",
            sa.Integer(),
            nullable=True,
            comment="Groundedness score (0-100)",
        ),
        sa.Column(
            "fluency",
            sa.Integer(),
            nullable=True,
            comment="Fluency score (0-100)",
        ),
        # Sentiment fields
        sa.Column(
            "overall_sentiment",
            sa.String(20),
            nullable=True,
            comment="Overall sentiment: positive, negative, neutral",
        ),
        sa.Column(
            "sentiment_score",
            sa.Float(),
            nullable=True,
            comment="Sentiment score (-1.0 to 1.0)",
        ),
        sa.Column(
            "sentiment_progression",
            postgresql.JSON(),
            nullable=True,
            comment="Sentiment changes throughout call",
        ),
        sa.Column(
            "escalation_risk",
            sa.Float(),
            nullable=True,
            comment="Escalation risk score (0.0 to 1.0)",
        ),
        # Latency tracking (Retell pattern)
        sa.Column(
            "latency_p50_ms",
            sa.Integer(),
            nullable=True,
            comment="50th percentile response latency in ms",
        ),
        sa.Column(
            "latency_p90_ms",
            sa.Integer(),
            nullable=True,
            comment="90th percentile response latency in ms",
        ),
        sa.Column(
            "latency_p95_ms",
            sa.Integer(),
            nullable=True,
            comment="95th percentile response latency in ms",
        ),
        # Audio quality
        sa.Column(
            "audio_quality_score",
            sa.Integer(),
            nullable=True,
            comment="Audio quality score (0-100)",
        ),
        sa.Column(
            "background_noise_detected",
            sa.Boolean(),
            nullable=True,
            comment="Whether significant background noise was detected",
        ),
        sa.Column(
            "vad_metrics",
            postgresql.JSON(),
            nullable=True,
            comment="Voice activity detection metrics",
        ),
        # Analysis JSONB fields
        sa.Column(
            "objectives_detected",
            postgresql.JSON(),
            nullable=True,
            comment="List of detected caller objectives",
        ),
        sa.Column(
            "objectives_completed",
            postgresql.JSON(),
            nullable=True,
            comment="List of completed objectives",
        ),
        sa.Column(
            "failure_reasons",
            postgresql.JSON(),
            nullable=True,
            comment="List of failure reasons if evaluation failed",
        ),
        sa.Column(
            "recommendations",
            postgresql.JSON(),
            nullable=True,
            comment="List of improvement recommendations",
        ),
        sa.Column(
            "turn_analysis",
            postgresql.JSON(),
            nullable=True,
            comment="Per-turn analysis data",
        ),
        sa.Column(
            "criteria_scores",
            postgresql.JSON(),
            nullable=True,
            comment="Detailed scoring by criteria (LlamaIndex pattern)",
        ),
        # Evaluation metadata
        sa.Column(
            "evaluation_model",
            sa.String(100),
            nullable=False,
            comment="Model used for evaluation (e.g., claude-sonnet-4-20250514)",
        ),
        sa.Column(
            "evaluation_latency_ms",
            sa.Integer(),
            nullable=True,
            comment="Time taken for evaluation in ms",
        ),
        sa.Column(
            "evaluation_cost_cents",
            sa.Float(),
            nullable=True,
            comment="Cost of evaluation in cents",
        ),
        sa.Column(
            "evaluation_prompt_version",
            sa.String(50),
            nullable=True,
            comment="Version of evaluation prompt used",
        ),
        # Timestamps
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Constraints
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["call_id"],
            ["call_records.id"],
            name="fk_call_evaluations_call_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name="fk_call_evaluations_agent_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_call_evaluations_workspace_id",
            ondelete="CASCADE",
        ),
    )

    # Create indexes for common queries
    op.create_index("ix_call_evaluations_call_id", "call_evaluations", ["call_id"], unique=True)
    op.create_index("ix_call_evaluations_agent_id", "call_evaluations", ["agent_id"])
    op.create_index("ix_call_evaluations_workspace_id", "call_evaluations", ["workspace_id"])
    op.create_index("ix_call_evaluations_passed", "call_evaluations", ["passed"])
    op.create_index("ix_call_evaluations_created_at", "call_evaluations", ["created_at"])
    op.create_index("ix_call_evaluations_overall_score", "call_evaluations", ["overall_score"])
    # Composite index for dashboard queries
    op.create_index(
        "ix_call_evaluations_workspace_created",
        "call_evaluations",
        ["workspace_id", "created_at"],
    )
    op.create_index(
        "ix_call_evaluations_agent_created",
        "call_evaluations",
        ["agent_id", "created_at"],
    )


def downgrade() -> None:
    """Drop call_evaluations table."""
    op.drop_index("ix_call_evaluations_agent_created", "call_evaluations")
    op.drop_index("ix_call_evaluations_workspace_created", "call_evaluations")
    op.drop_index("ix_call_evaluations_overall_score", "call_evaluations")
    op.drop_index("ix_call_evaluations_created_at", "call_evaluations")
    op.drop_index("ix_call_evaluations_passed", "call_evaluations")
    op.drop_index("ix_call_evaluations_workspace_id", "call_evaluations")
    op.drop_index("ix_call_evaluations_agent_id", "call_evaluations")
    op.drop_index("ix_call_evaluations_call_id", "call_evaluations")
    op.drop_table("call_evaluations")
