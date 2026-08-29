"""Add test_scenarios and test_runs tables for QA Testing Framework.

Revision ID: 017_test_scenarios
Revises: 016_call_evaluations
Create Date: 2025-12-19

This migration creates tables for pre-deployment testing:
- test_scenarios: Pre-built test cases for voice agents
- test_runs: Track individual test execution results
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "017_test_scenarios"
down_revision: str | None = "016_call_evaluations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create test_scenarios and test_runs tables."""
    # Create test_scenarios table
    op.create_table(
        "test_scenarios",
        # Primary key
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        # Ownership (null = built-in scenario)
        sa.Column(
            "user_id",
            sa.Integer(),
            nullable=True,
            comment="Owner user ID (null for built-in scenarios)",
        ),
        sa.Column(
            "workspace_id",
            sa.Uuid(as_uuid=True),
            nullable=True,
            comment="Workspace for custom scenarios",
        ),
        # Scenario metadata
        sa.Column(
            "name",
            sa.String(200),
            nullable=False,
            comment="Scenario name",
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=True,
            comment="Detailed description of what this scenario tests",
        ),
        sa.Column(
            "category",
            sa.String(50),
            nullable=False,
            comment="Category: greeting, booking, objection, support, compliance, edge_case",
        ),
        sa.Column(
            "difficulty",
            sa.String(20),
            nullable=False,
            default="medium",
            comment="Difficulty: easy, medium, hard",
        ),
        # Test configuration (using JSONB for better indexing/operations)
        sa.Column(
            "caller_persona",
            postgresql.JSONB(),
            nullable=False,
            comment="Simulated caller personality and context",
        ),
        sa.Column(
            "conversation_flow",
            postgresql.JSONB(),
            nullable=False,
            comment="Array of conversation turns with user messages",
        ),
        sa.Column(
            "expected_behaviors",
            postgresql.JSONB(),
            nullable=False,
            comment="Expected agent behaviors and responses",
        ),
        sa.Column(
            "expected_tool_calls",
            postgresql.JSONB(),
            nullable=True,
            comment="Expected tool invocations (if any)",
        ),
        sa.Column(
            "success_criteria",
            postgresql.JSONB(),
            nullable=False,
            comment="Criteria for pass/fail determination",
        ),
        # Scenario flags
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            default=True,
            comment="Whether scenario is active",
        ),
        sa.Column(
            "is_built_in",
            sa.Boolean(),
            nullable=False,
            default=False,
            comment="Whether this is a built-in scenario",
        ),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.String(50)),
            nullable=True,
            comment="Tags for filtering scenarios",
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
            ["user_id"],
            ["users.id"],
            name="fk_test_scenarios_user_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_test_scenarios_workspace_id",
            ondelete="CASCADE",
        ),
    )

    # Create indexes for test_scenarios
    op.create_index("ix_test_scenarios_user_id", "test_scenarios", ["user_id"])
    op.create_index("ix_test_scenarios_workspace_id", "test_scenarios", ["workspace_id"])
    op.create_index("ix_test_scenarios_category", "test_scenarios", ["category"])
    op.create_index("ix_test_scenarios_is_built_in", "test_scenarios", ["is_built_in"])
    op.create_index("ix_test_scenarios_is_active", "test_scenarios", ["is_active"])

    # Create test_runs table
    op.create_table(
        "test_runs",
        # Primary key
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        # References
        sa.Column(
            "scenario_id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            comment="Reference to test_scenarios.id",
        ),
        sa.Column(
            "agent_id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            comment="Agent being tested",
        ),
        sa.Column(
            "workspace_id",
            sa.Uuid(as_uuid=True),
            nullable=True,
            comment="Workspace for data isolation",
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            nullable=False,
            comment="User who initiated the test",
        ),
        # Test execution
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            default="pending",
            comment="Status: pending, running, passed, failed, error",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When test execution started",
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When test execution completed",
        ),
        sa.Column(
            "duration_ms",
            sa.Integer(),
            nullable=True,
            comment="Test duration in milliseconds",
        ),
        # Results
        sa.Column(
            "overall_score",
            sa.Integer(),
            nullable=True,
            comment="Overall test score (0-100)",
        ),
        sa.Column(
            "passed",
            sa.Boolean(),
            nullable=True,
            comment="Whether the test passed",
        ),
        # Conversation data (using JSONB for better indexing/operations)
        sa.Column(
            "actual_transcript",
            postgresql.JSONB(),
            nullable=True,
            comment="Actual conversation transcript",
        ),
        sa.Column(
            "actual_tool_calls",
            postgresql.JSONB(),
            nullable=True,
            comment="Tools actually invoked during test",
        ),
        # Detailed results
        sa.Column(
            "criteria_results",
            postgresql.JSONB(),
            nullable=True,
            comment="Pass/fail for each success criterion",
        ),
        sa.Column(
            "behavior_matches",
            postgresql.JSONB(),
            nullable=True,
            comment="Which expected behaviors were observed",
        ),
        sa.Column(
            "issues_found",
            postgresql.JSONB(),
            nullable=True,
            comment="List of issues identified during test",
        ),
        sa.Column(
            "recommendations",
            postgresql.JSONB(),
            nullable=True,
            comment="Recommendations for improvement",
        ),
        # Error tracking
        sa.Column(
            "error_message",
            sa.Text(),
            nullable=True,
            comment="Error message if test failed to execute",
        ),
        sa.Column(
            "error_details",
            postgresql.JSONB(),
            nullable=True,
            comment="Detailed error information",
        ),
        # Evaluation reference (if QA evaluation was performed)
        sa.Column(
            "evaluation_id",
            sa.Uuid(as_uuid=True),
            nullable=True,
            comment="Reference to call_evaluations.id if evaluated",
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
            ["scenario_id"],
            ["test_scenarios.id"],
            name="fk_test_runs_scenario_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name="fk_test_runs_agent_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_test_runs_workspace_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_test_runs_user_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["call_evaluations.id"],
            name="fk_test_runs_evaluation_id",
            ondelete="SET NULL",
        ),
    )

    # Create indexes for test_runs
    op.create_index("ix_test_runs_scenario_id", "test_runs", ["scenario_id"])
    op.create_index("ix_test_runs_agent_id", "test_runs", ["agent_id"])
    op.create_index("ix_test_runs_workspace_id", "test_runs", ["workspace_id"])
    op.create_index("ix_test_runs_user_id", "test_runs", ["user_id"])
    op.create_index("ix_test_runs_status", "test_runs", ["status"])
    op.create_index("ix_test_runs_passed", "test_runs", ["passed"])
    op.create_index("ix_test_runs_created_at", "test_runs", ["created_at"])
    # Composite index for dashboard queries
    op.create_index(
        "ix_test_runs_agent_created",
        "test_runs",
        ["agent_id", "created_at"],
    )


def downgrade() -> None:
    """Drop test_runs and test_scenarios tables."""
    # Drop test_runs indexes
    op.drop_index("ix_test_runs_agent_created", "test_runs")
    op.drop_index("ix_test_runs_created_at", "test_runs")
    op.drop_index("ix_test_runs_passed", "test_runs")
    op.drop_index("ix_test_runs_status", "test_runs")
    op.drop_index("ix_test_runs_user_id", "test_runs")
    op.drop_index("ix_test_runs_workspace_id", "test_runs")
    op.drop_index("ix_test_runs_agent_id", "test_runs")
    op.drop_index("ix_test_runs_scenario_id", "test_runs")
    op.drop_table("test_runs")

    # Drop test_scenarios indexes
    op.drop_index("ix_test_scenarios_is_active", "test_scenarios")
    op.drop_index("ix_test_scenarios_is_built_in", "test_scenarios")
    op.drop_index("ix_test_scenarios_category", "test_scenarios")
    op.drop_index("ix_test_scenarios_workspace_id", "test_scenarios")
    op.drop_index("ix_test_scenarios_user_id", "test_scenarios")
    op.drop_table("test_scenarios")
