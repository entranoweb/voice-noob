"""TestScenario and TestRun models for QA Testing Framework.

Pre-deployment testing infrastructure for voice agents.
"""

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.agent import Agent
    from app.models.call_evaluation import CallEvaluation
    from app.models.user import User
    from app.models.workspace import Workspace


class ScenarioCategory(str, Enum):
    """Test scenario categories."""

    GREETING = "greeting"
    BOOKING = "booking"
    OBJECTION = "objection"
    SUPPORT = "support"
    COMPLIANCE = "compliance"
    EDGE_CASE = "edge_case"
    TRANSFER = "transfer"
    INFORMATION = "information"


class ScenarioDifficulty(str, Enum):
    """Test scenario difficulty levels."""

    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class TestRunStatus(str, Enum):
    """Test run status."""

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class TestScenario(Base):
    """Pre-built test scenario for voice agents.

    Stores test cases with expected behaviors and success criteria.
    Can be built-in (shipped with the system) or user-created.
    """

    __tablename__ = "test_scenarios"

    # Primary key
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Ownership (null = built-in scenario)
    user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="Owner user ID (null for built-in scenarios)",
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="Workspace for custom scenarios",
    )

    # Scenario metadata
    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="Scenario name")
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Detailed description of what this scenario tests"
    )
    category: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        comment="Category: greeting, booking, objection, support, compliance, edge_case",
    )
    difficulty: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=ScenarioDifficulty.MEDIUM.value,
        comment="Difficulty: easy, medium, hard",
    )

    # Test configuration
    caller_persona: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="Simulated caller personality and context"
    )
    conversation_flow: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, comment="Array of conversation turns with user messages"
    )
    expected_behaviors: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, comment="Expected agent behaviors and responses"
    )
    expected_tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON, nullable=True, comment="Expected tool invocations (if any)"
    )
    success_criteria: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="Criteria for pass/fail determination"
    )

    # Scenario flags
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True, comment="Whether scenario is active"
    )
    is_built_in: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
        comment="Whether this is a built-in scenario",
    )
    tags: Mapped[list[str] | None] = mapped_column(
        ARRAY(String(50)), nullable=True, comment="Tags for filtering scenarios"
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    # Relationships
    user: Mapped["User | None"] = relationship("User", lazy="selectin")
    workspace: Mapped["Workspace | None"] = relationship("Workspace", lazy="selectin")
    test_runs: Mapped[list["TestRun"]] = relationship(
        "TestRun", back_populates="scenario", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<TestScenario(id={self.id}, name={self.name}, category={self.category})>"


class TestRun(Base):
    """Individual test execution result.

    Tracks a single run of a test scenario against an agent.
    """

    __tablename__ = "test_runs"

    # Primary key
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # References
    scenario_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("test_scenarios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Reference to test_scenarios.id",
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Agent being tested",
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="Workspace for data isolation",
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="User who initiated the test",
    )

    # Test execution
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=TestRunStatus.PENDING.value,
        index=True,
        comment="Status: pending, running, passed, failed, error",
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When test execution started"
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When test execution completed"
    )
    duration_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Test duration in milliseconds"
    )

    # Results
    overall_score: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Overall test score (0-100)"
    )
    passed: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, index=True, comment="Whether the test passed"
    )

    # Conversation data
    actual_transcript: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON, nullable=True, comment="Actual conversation transcript"
    )
    actual_tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON, nullable=True, comment="Tools actually invoked during test"
    )

    # Detailed results
    criteria_results: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="Pass/fail for each success criterion"
    )
    behavior_matches: Mapped[dict[str, bool] | None] = mapped_column(
        JSON, nullable=True, comment="Which expected behaviors were observed"
    )
    issues_found: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, comment="List of issues identified during test"
    )
    recommendations: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, comment="Recommendations for improvement"
    )

    # Error tracking
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Error message if test failed to execute"
    )
    error_details: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="Detailed error information"
    )

    # Evaluation reference
    evaluation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("call_evaluations.id", ondelete="SET NULL"),
        nullable=True,
        comment="Reference to call_evaluations.id if evaluated",
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
    scenario: Mapped["TestScenario"] = relationship(
        "TestScenario", back_populates="test_runs", lazy="selectin"
    )
    agent: Mapped["Agent"] = relationship("Agent", lazy="selectin")
    workspace: Mapped["Workspace | None"] = relationship("Workspace", lazy="selectin")
    user: Mapped["User"] = relationship("User", lazy="selectin")
    evaluation: Mapped["CallEvaluation | None"] = relationship("CallEvaluation", lazy="selectin")

    def __repr__(self) -> str:
        return f"<TestRun(id={self.id}, scenario={self.scenario_id}, status={self.status})>"
