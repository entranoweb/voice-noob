"""Tests for TestScenario and TestRun models (Week 2 - Task 10).

Tests for pre-deployment testing data models.
Note: These tests use SQLite which doesn't support PostgreSQL ARRAY type.
The tags field tests are skipped due to this limitation.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.test_scenario import (
    ScenarioCategory,
    ScenarioDifficulty,
    TestRun,
    TestRunStatus,
    TestScenario,
)


class TestScenarioModel:
    """Test TestScenario model."""

    @pytest.mark.asyncio
    async def test_create_scenario_minimal(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
    ) -> None:
        """Test creating a scenario with minimal required fields."""
        user = await create_test_user()

        scenario = TestScenario(
            user_id=user.id,
            name="Test Scenario",
            category=ScenarioCategory.GREETING.value,
            difficulty=ScenarioDifficulty.EASY.value,
            caller_persona={"name": "Test Caller", "mood": "friendly"},
            conversation_flow=[{"turn": 1, "message": "Hello"}],
            expected_behaviors=["Greet the caller"],
            success_criteria={"min_score": 70},
        )

        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        assert scenario.id is not None
        assert scenario.name == "Test Scenario"
        assert scenario.category == "greeting"
        assert scenario.difficulty == "easy"
        assert scenario.is_active is True
        assert scenario.is_built_in is False

    @pytest.mark.asyncio
    async def test_create_built_in_scenario(
        self,
        test_session: AsyncSession,
    ) -> None:
        """Test creating a built-in scenario (no user_id)."""
        scenario = TestScenario(
            name="Built-in Greeting Test",
            category=ScenarioCategory.GREETING.value,
            difficulty=ScenarioDifficulty.EASY.value,
            caller_persona={"name": "Standard Caller"},
            conversation_flow=[{"turn": 1, "message": "Hi there"}],
            expected_behaviors=["Respond with greeting"],
            success_criteria={"min_score": 70},
            is_built_in=True,
        )

        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        assert scenario.id is not None
        assert scenario.user_id is None
        assert scenario.is_built_in is True

    @pytest.mark.asyncio
    async def test_scenario_categories(self) -> None:
        """Test all scenario categories are defined."""
        expected_categories = [
            "greeting",
            "booking",
            "objection",
            "support",
            "compliance",
            "edge_case",
            "transfer",
            "information",
        ]

        for cat in expected_categories:
            assert cat in [c.value for c in ScenarioCategory]

    @pytest.mark.asyncio
    async def test_scenario_difficulties(self) -> None:
        """Test all scenario difficulties are defined."""
        expected_difficulties = ["easy", "medium", "hard"]

        for diff in expected_difficulties:
            assert diff in [d.value for d in ScenarioDifficulty]

    @pytest.mark.asyncio
    async def test_scenario_with_workspace(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_workspace: Any,
    ) -> None:
        """Test creating a scenario with workspace association."""
        user = await create_test_user()
        workspace = await create_test_workspace(user_id=user.id)

        scenario = TestScenario(
            user_id=user.id,
            workspace_id=workspace.id,
            name="Workspace Scenario",
            category=ScenarioCategory.BOOKING.value,
            difficulty=ScenarioDifficulty.MEDIUM.value,
            caller_persona={"name": "Business Caller"},
            conversation_flow=[{"turn": 1, "message": "I need to book"}],
            expected_behaviors=["Offer booking options"],
            success_criteria={"min_score": 75},
        )

        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        assert scenario.workspace_id == workspace.id

    @pytest.mark.asyncio
    async def test_scenario_with_expected_tool_calls(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
    ) -> None:
        """Test creating a scenario with expected tool calls."""
        user = await create_test_user()

        scenario = TestScenario(
            user_id=user.id,
            name="Tool Usage Scenario",
            category=ScenarioCategory.BOOKING.value,
            difficulty=ScenarioDifficulty.MEDIUM.value,
            caller_persona={"name": "Booking Caller"},
            conversation_flow=[{"turn": 1, "message": "Book for tomorrow"}],
            expected_behaviors=["Check availability", "Confirm booking"],
            expected_tool_calls=[
                {"tool": "check_availability", "params": {"date": "tomorrow"}},
                {"tool": "create_booking", "params": {}},
            ],
            success_criteria={"min_score": 80, "required_tools": ["check_availability"]},
        )

        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        assert scenario.expected_tool_calls is not None
        assert len(scenario.expected_tool_calls) == 2

    @pytest.mark.asyncio
    async def test_scenario_repr(
        self,
        test_session: AsyncSession,
    ) -> None:
        """Test scenario string representation."""
        scenario = TestScenario(
            name="Repr Test",
            category=ScenarioCategory.GREETING.value,
            difficulty=ScenarioDifficulty.EASY.value,
            caller_persona={},
            conversation_flow=[],
            expected_behaviors=[],
            success_criteria={},
        )

        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        repr_str = repr(scenario)
        assert "TestScenario" in repr_str
        assert "Repr Test" in repr_str
        assert "greeting" in repr_str

    @pytest.mark.asyncio
    async def test_scenario_with_complex_persona(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
    ) -> None:
        """Test scenario with complex persona configuration."""
        user = await create_test_user()

        scenario = TestScenario(
            user_id=user.id,
            name="Complex Persona Scenario",
            category=ScenarioCategory.OBJECTION.value,
            difficulty=ScenarioDifficulty.HARD.value,
            caller_persona={
                "name": "Difficult Customer",
                "tone": "aggressive",
                "emotional_state": "frustrated",
                "background": "previous bad experience",
                "objections": ["too expensive", "heard bad reviews"],
                "personality_traits": ["skeptical", "detail-oriented", "impatient"],
            },
            conversation_flow=[
                {"turn": 1, "message": "Your prices are way too high!"},
                {"turn": 2, "message": "I heard terrible things about your service"},
            ],
            expected_behaviors=[
                "Handle objection professionally",
                "Remain calm",
                "Offer solutions",
            ],
            success_criteria={"objection_handled": True, "customer_calm": True, "min_score": 80},
        )

        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        assert scenario.caller_persona["tone"] == "aggressive"
        assert "too expensive" in scenario.caller_persona["objections"]
        assert len(scenario.caller_persona["personality_traits"]) == 3
        assert scenario.difficulty == ScenarioDifficulty.HARD.value

    @pytest.mark.asyncio
    async def test_scenario_with_multi_turn_conversation(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
    ) -> None:
        """Test scenario with multi-turn conversation flow."""
        user = await create_test_user()

        scenario = TestScenario(
            user_id=user.id,
            name="Multi-turn Support Scenario",
            category=ScenarioCategory.SUPPORT.value,
            difficulty=ScenarioDifficulty.MEDIUM.value,
            caller_persona={"name": "Confused Customer", "tone": "uncertain"},
            conversation_flow=[
                {
                    "turn": 1,
                    "message": "I'm having trouble with my account",
                    "expected_intent": "support_request",
                },
                {"turn": 2, "message": "I can't login", "expected_intent": "login_issue"},
                {
                    "turn": 3,
                    "message": "Yes, I tried resetting my password",
                    "expected_intent": "confirm_action",
                },
                {"turn": 4, "message": "Thank you, that worked!", "expected_intent": "gratitude"},
            ],
            expected_behaviors=[
                "Acknowledge the issue",
                "Ask diagnostic questions",
                "Provide solution steps",
                "Confirm resolution",
            ],
            success_criteria={"issue_resolved": True, "customer_satisfied": True, "min_score": 75},
        )

        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        assert len(scenario.conversation_flow) == 4
        assert scenario.conversation_flow[0]["turn"] == 1
        assert scenario.conversation_flow[3]["expected_intent"] == "gratitude"
        assert len(scenario.expected_behaviors) == 4

    @pytest.mark.asyncio
    async def test_scenario_is_active_flag(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
    ) -> None:
        """Test scenario active/inactive status."""
        from sqlalchemy import select

        user = await create_test_user()

        active_scenario = TestScenario(
            user_id=user.id,
            name="Active Scenario",
            category=ScenarioCategory.GREETING.value,
            difficulty=ScenarioDifficulty.EASY.value,
            caller_persona={},
            conversation_flow=[],
            expected_behaviors=[],
            success_criteria={},
            is_active=True,
        )

        inactive_scenario = TestScenario(
            user_id=user.id,
            name="Inactive Scenario",
            category=ScenarioCategory.GREETING.value,
            difficulty=ScenarioDifficulty.EASY.value,
            caller_persona={},
            conversation_flow=[],
            expected_behaviors=[],
            success_criteria={},
            is_active=False,
        )

        test_session.add(active_scenario)
        test_session.add(inactive_scenario)
        await test_session.commit()

        # Query only active scenarios
        result = await test_session.execute(
            select(TestScenario).where(TestScenario.is_active == True)  # noqa: E712
        )
        active_scenarios = result.scalars().all()

        assert len(active_scenarios) == 1
        assert active_scenarios[0].name == "Active Scenario"

    @pytest.mark.asyncio
    async def test_scenario_with_detailed_success_criteria(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
    ) -> None:
        """Test scenario with detailed success criteria."""
        user = await create_test_user()

        scenario = TestScenario(
            user_id=user.id,
            name="Compliance Check Scenario",
            category=ScenarioCategory.COMPLIANCE.value,
            difficulty=ScenarioDifficulty.HARD.value,
            caller_persona={"name": "Privacy-conscious User"},
            conversation_flow=[{"turn": 1, "message": "What data do you collect about me?"}],
            expected_behaviors=[
                "Explain data collection practices",
                "Mention privacy policy",
                "Offer opt-out options",
                "Provide contact for data deletion",
            ],
            success_criteria={
                "privacy_policy_mentioned": True,
                "data_types_explained": True,
                "opt_out_offered": True,
                "compliance_maintained": True,
                "minimum_score": 90,
                "required_keywords": ["privacy policy", "opt-out", "data protection"],
            },
        )

        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        assert scenario.success_criteria["minimum_score"] == 90
        assert scenario.success_criteria["compliance_maintained"] is True
        assert len(scenario.success_criteria["required_keywords"]) == 3

    @pytest.mark.asyncio
    async def test_scenario_timestamps(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
    ) -> None:
        """Test that scenario timestamps are set automatically."""
        user = await create_test_user()

        scenario = TestScenario(
            user_id=user.id,
            name="Timestamp Test",
            category=ScenarioCategory.GREETING.value,
            difficulty=ScenarioDifficulty.EASY.value,
            caller_persona={},
            conversation_flow=[],
            expected_behaviors=[],
            success_criteria={},
        )

        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        assert scenario.created_at is not None
        assert scenario.updated_at is not None
        # Note: SQLite doesn't preserve timezone info, but timestamps are stored correctly
        # In production (PostgreSQL), these would be timezone-aware


class TestTestRunModel:
    """Test TestRun model."""

    @pytest.mark.asyncio
    async def test_create_test_run(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Test creating a test run."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id)

        # Create scenario first
        scenario = TestScenario(
            name="Test Scenario",
            category=ScenarioCategory.GREETING.value,
            difficulty=ScenarioDifficulty.EASY.value,
            caller_persona={},
            conversation_flow=[],
            expected_behaviors=[],
            success_criteria={},
            is_built_in=True,
        )
        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        # Create test run
        test_run = TestRun(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=user.id,
            status=TestRunStatus.PENDING.value,
        )

        test_session.add(test_run)
        await test_session.commit()
        await test_session.refresh(test_run)

        assert test_run.id is not None
        assert test_run.status == "pending"
        assert test_run.passed is None
        assert test_run.overall_score is None

    @pytest.mark.asyncio
    async def test_test_run_statuses(self) -> None:
        """Test all test run statuses are defined."""
        expected_statuses = ["pending", "running", "passed", "failed", "error"]

        for status in expected_statuses:
            assert status in [s.value for s in TestRunStatus]

    @pytest.mark.asyncio
    async def test_test_run_with_results(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Test creating a completed test run with results."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id)

        # Create scenario
        scenario = TestScenario(
            name="Results Test Scenario",
            category=ScenarioCategory.GREETING.value,
            difficulty=ScenarioDifficulty.EASY.value,
            caller_persona={},
            conversation_flow=[],
            expected_behaviors=["Greet caller", "Ask how to help"],
            success_criteria={"min_score": 70},
            is_built_in=True,
        )
        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        # Create completed test run
        now = datetime.now(UTC)
        test_run = TestRun(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=user.id,
            status=TestRunStatus.PASSED.value,
            started_at=now,
            completed_at=now,
            duration_ms=1500,
            overall_score=85,
            passed=True,
            actual_transcript=[
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi! How can I help?"},
            ],
            behavior_matches={"Greet caller": True, "Ask how to help": True},
            criteria_results={"min_score": {"passed": True, "value": 85}},
            issues_found=[],
            recommendations=["Consider adding more personalization"],
        )

        test_session.add(test_run)
        await test_session.commit()
        await test_session.refresh(test_run)

        assert test_run.passed is True
        assert test_run.overall_score == 85
        assert test_run.duration_ms == 1500
        assert len(test_run.actual_transcript) == 2
        assert test_run.behavior_matches["Greet caller"] is True

    @pytest.mark.asyncio
    async def test_test_run_with_error(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Test creating a failed test run with error."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id)

        # Create scenario
        scenario = TestScenario(
            name="Error Test Scenario",
            category=ScenarioCategory.GREETING.value,
            difficulty=ScenarioDifficulty.EASY.value,
            caller_persona={},
            conversation_flow=[],
            expected_behaviors=[],
            success_criteria={},
            is_built_in=True,
        )
        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        # Create error test run
        test_run = TestRun(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=user.id,
            status=TestRunStatus.ERROR.value,
            error_message="API timeout",
            error_details={"code": "TIMEOUT", "retry_count": 3},
        )

        test_session.add(test_run)
        await test_session.commit()
        await test_session.refresh(test_run)

        assert test_run.status == "error"
        assert test_run.error_message == "API timeout"
        assert test_run.error_details["code"] == "TIMEOUT"

    @pytest.mark.asyncio
    async def test_test_run_repr(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Test test run string representation."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id)

        scenario = TestScenario(
            name="Repr Scenario",
            category=ScenarioCategory.GREETING.value,
            difficulty=ScenarioDifficulty.EASY.value,
            caller_persona={},
            conversation_flow=[],
            expected_behaviors=[],
            success_criteria={},
            is_built_in=True,
        )
        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        test_run = TestRun(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=user.id,
            status=TestRunStatus.PENDING.value,
        )
        test_session.add(test_run)
        await test_session.commit()
        await test_session.refresh(test_run)

        repr_str = repr(test_run)
        assert "TestRun" in repr_str
        assert "pending" in repr_str

    @pytest.mark.asyncio
    async def test_test_run_status_transitions(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Test test run status transitions from pending to completed."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id)

        scenario = TestScenario(
            name="Status Transition Test",
            category=ScenarioCategory.GREETING.value,
            difficulty=ScenarioDifficulty.EASY.value,
            caller_persona={},
            conversation_flow=[],
            expected_behaviors=[],
            success_criteria={},
            is_built_in=True,
        )
        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        test_run = TestRun(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=user.id,
            status=TestRunStatus.PENDING.value,
        )
        test_session.add(test_run)
        await test_session.commit()
        await test_session.refresh(test_run)

        # Verify initial status
        assert test_run.status == TestRunStatus.PENDING.value
        assert test_run.started_at is None

        # Transition to RUNNING
        test_run.status = TestRunStatus.RUNNING.value
        test_run.started_at = datetime.now(UTC)
        await test_session.commit()
        await test_session.refresh(test_run)

        assert test_run.status == TestRunStatus.RUNNING.value
        assert test_run.started_at is not None
        assert test_run.completed_at is None

        # Transition to PASSED
        test_run.status = TestRunStatus.PASSED.value
        test_run.completed_at = datetime.now(UTC)
        test_run.passed = True
        test_run.overall_score = 88
        await test_session.commit()
        await test_session.refresh(test_run)

        assert test_run.status == TestRunStatus.PASSED.value
        assert test_run.passed is True
        assert test_run.overall_score == 88
        assert test_run.completed_at is not None

    @pytest.mark.asyncio
    async def test_test_run_with_failed_status(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Test test run with failed status and issues."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id)

        scenario = TestScenario(
            name="Failure Test",
            category=ScenarioCategory.BOOKING.value,
            difficulty=ScenarioDifficulty.MEDIUM.value,
            caller_persona={},
            conversation_flow=[],
            expected_behaviors=["Book appointment", "Confirm details"],
            success_criteria={"appointment_booked": True},
            is_built_in=True,
        )
        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        test_run = TestRun(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=user.id,
            status=TestRunStatus.FAILED.value,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            duration_ms=3500,
            overall_score=42,
            passed=False,
            criteria_results={"appointment_booked": False},
            behavior_matches={"Book appointment": False, "Confirm details": False},
            issues_found=[
                "Failed to invoke booking tool",
                "Did not ask for appointment details",
                "Did not provide confirmation",
            ],
            recommendations=[
                "Review tool calling configuration",
                "Add more specific prompts for booking flow",
                "Include confirmation step in system prompt",
            ],
        )

        test_session.add(test_run)
        await test_session.commit()
        await test_session.refresh(test_run)

        assert test_run.status == TestRunStatus.FAILED.value
        assert test_run.passed is False
        assert test_run.overall_score == 42
        assert len(test_run.issues_found) == 3
        assert len(test_run.recommendations) == 3
        assert test_run.behavior_matches["Book appointment"] is False

    @pytest.mark.asyncio
    async def test_test_run_with_tool_calls(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Test test run with actual tool calls recorded."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id)

        scenario = TestScenario(
            name="Tool Usage Test",
            category=ScenarioCategory.BOOKING.value,
            difficulty=ScenarioDifficulty.MEDIUM.value,
            caller_persona={},
            conversation_flow=[],
            expected_behaviors=[],
            expected_tool_calls=[
                {"tool": "search_availability", "required": True},
                {"tool": "book_appointment", "required": True},
            ],
            success_criteria={},
            is_built_in=True,
        )
        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        test_run = TestRun(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=user.id,
            status=TestRunStatus.PASSED.value,
            passed=True,
            overall_score=92,
            actual_tool_calls=[
                {
                    "timestamp": "2025-01-15T10:00:01Z",
                    "tool": "search_availability",
                    "arguments": {"date": "2025-01-20", "duration": 30},
                    "result": {"available_slots": ["14:00", "15:00"]},
                },
                {
                    "timestamp": "2025-01-15T10:00:05Z",
                    "tool": "book_appointment",
                    "arguments": {"date": "2025-01-20", "time": "14:00", "duration": 30},
                    "result": {"booking_id": "abc123", "confirmed": True},
                },
            ],
        )

        test_session.add(test_run)
        await test_session.commit()
        await test_session.refresh(test_run)

        assert len(test_run.actual_tool_calls) == 2
        assert test_run.actual_tool_calls[0]["tool"] == "search_availability"
        assert test_run.actual_tool_calls[1]["result"]["confirmed"] is True

    @pytest.mark.asyncio
    async def test_test_run_with_workspace(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
        create_test_workspace: Any,
    ) -> None:
        """Test test run with workspace association."""
        user = await create_test_user()
        workspace = await create_test_workspace(user_id=user.id)
        agent = await create_test_agent(user_id=user.id)

        scenario = TestScenario(
            name="Workspace Test Scenario",
            category=ScenarioCategory.GREETING.value,
            difficulty=ScenarioDifficulty.EASY.value,
            caller_persona={},
            conversation_flow=[],
            expected_behaviors=[],
            success_criteria={},
            is_built_in=True,
        )
        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        test_run = TestRun(
            scenario_id=scenario.id,
            agent_id=agent.id,
            workspace_id=workspace.id,
            user_id=user.id,
            status=TestRunStatus.PENDING.value,
        )

        test_session.add(test_run)
        await test_session.commit()
        await test_session.refresh(test_run)

        assert test_run.workspace_id == workspace.id

    @pytest.mark.asyncio
    async def test_test_run_duration_calculation(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Test test run duration calculation."""
        from datetime import timedelta

        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id)

        scenario = TestScenario(
            name="Duration Test",
            category=ScenarioCategory.GREETING.value,
            difficulty=ScenarioDifficulty.EASY.value,
            caller_persona={},
            conversation_flow=[],
            expected_behaviors=[],
            success_criteria={},
            is_built_in=True,
        )
        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        started_at = datetime.now(UTC)
        completed_at = started_at + timedelta(seconds=7, milliseconds=250)

        test_run = TestRun(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=user.id,
            status=TestRunStatus.PASSED.value,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=7250,
        )

        test_session.add(test_run)
        await test_session.commit()
        await test_session.refresh(test_run)

        assert test_run.duration_ms == 7250
        assert test_run.started_at is not None
        assert test_run.completed_at is not None

    @pytest.mark.asyncio
    async def test_test_run_timestamps(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Test that test run timestamps are set automatically."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id)

        scenario = TestScenario(
            name="Timestamp Test",
            category=ScenarioCategory.GREETING.value,
            difficulty=ScenarioDifficulty.EASY.value,
            caller_persona={},
            conversation_flow=[],
            expected_behaviors=[],
            success_criteria={},
            is_built_in=True,
        )
        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        test_run = TestRun(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=user.id,
            status=TestRunStatus.PENDING.value,
        )

        test_session.add(test_run)
        await test_session.commit()
        await test_session.refresh(test_run)

        assert test_run.created_at is not None
        assert test_run.updated_at is not None
        # Note: SQLite doesn't preserve timezone info, but timestamps are stored correctly
        # In production (PostgreSQL), these would be timezone-aware


class TestRelationships:
    """Test model relationships between TestScenario and TestRun."""

    @pytest.mark.asyncio
    async def test_scenario_to_test_runs_relationship(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Test scenario to test runs relationship."""
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id)

        scenario = TestScenario(
            name="Relationship Test",
            category=ScenarioCategory.GREETING.value,
            difficulty=ScenarioDifficulty.EASY.value,
            caller_persona={},
            conversation_flow=[],
            expected_behaviors=[],
            success_criteria={},
            is_built_in=True,
        )
        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        # Create multiple test runs for the scenario
        test_run1 = TestRun(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=user.id,
            status=TestRunStatus.PASSED.value,
        )
        test_run2 = TestRun(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=user.id,
            status=TestRunStatus.FAILED.value,
        )
        test_run3 = TestRun(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=user.id,
            status=TestRunStatus.PENDING.value,
        )

        test_session.add(test_run1)
        test_session.add(test_run2)
        test_session.add(test_run3)
        await test_session.commit()

        # Re-fetch scenario with eager loading of test_runs
        result = await test_session.execute(
            select(TestScenario)
            .where(TestScenario.id == scenario.id)
            .options(selectinload(TestScenario.test_runs))
        )
        scenario_with_runs = result.scalar_one()

        assert len(scenario_with_runs.test_runs) == 3
        test_run_ids = {tr.id for tr in scenario_with_runs.test_runs}
        assert test_run1.id in test_run_ids
        assert test_run2.id in test_run_ids
        assert test_run3.id in test_run_ids

    @pytest.mark.asyncio
    async def test_test_run_to_scenario_relationship(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Test test run to scenario relationship."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id)

        scenario = TestScenario(
            name="Reverse Relationship Test",
            category=ScenarioCategory.BOOKING.value,
            difficulty=ScenarioDifficulty.MEDIUM.value,
            caller_persona={},
            conversation_flow=[],
            expected_behaviors=[],
            success_criteria={},
            is_built_in=True,
        )
        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        test_run = TestRun(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=user.id,
            status=TestRunStatus.PENDING.value,
        )
        test_session.add(test_run)
        await test_session.commit()
        await test_session.refresh(test_run)

        # Access scenario through test run
        assert test_run.scenario is not None
        assert test_run.scenario.id == scenario.id
        assert test_run.scenario.name == "Reverse Relationship Test"
        assert test_run.scenario.category == ScenarioCategory.BOOKING.value

    @pytest.mark.asyncio
    async def test_test_run_to_agent_relationship(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Test test run to agent relationship."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id, name="Test Agent XYZ")

        scenario = TestScenario(
            name="Agent Relationship Test",
            category=ScenarioCategory.GREETING.value,
            difficulty=ScenarioDifficulty.EASY.value,
            caller_persona={},
            conversation_flow=[],
            expected_behaviors=[],
            success_criteria={},
            is_built_in=True,
        )
        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        test_run = TestRun(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=user.id,
            status=TestRunStatus.PENDING.value,
        )
        test_session.add(test_run)
        await test_session.commit()
        await test_session.refresh(test_run)

        # Access agent through test run
        assert test_run.agent is not None
        assert test_run.agent.id == agent.id
        assert test_run.agent.name == "Test Agent XYZ"

    @pytest.mark.asyncio
    async def test_scenario_cascade_delete(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Test that deleting a scenario cascades to test runs."""
        from sqlalchemy import select

        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id)

        scenario = TestScenario(
            name="Cascade Test",
            category=ScenarioCategory.GREETING.value,
            difficulty=ScenarioDifficulty.EASY.value,
            caller_persona={},
            conversation_flow=[],
            expected_behaviors=[],
            success_criteria={},
            is_built_in=True,
        )
        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        test_run = TestRun(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=user.id,
            status=TestRunStatus.PENDING.value,
        )
        test_session.add(test_run)
        await test_session.commit()

        scenario_id = scenario.id

        # Delete scenario
        await test_session.delete(scenario)
        await test_session.commit()

        # Verify test run was deleted
        result = await test_session.execute(
            select(TestRun).where(TestRun.scenario_id == scenario_id)
        )
        test_runs = result.scalars().all()

        assert len(test_runs) == 0

    @pytest.mark.asyncio
    async def test_query_test_runs_by_status(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Test querying test runs by status."""
        from sqlalchemy import select

        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id)

        scenario = TestScenario(
            name="Status Query Test",
            category=ScenarioCategory.GREETING.value,
            difficulty=ScenarioDifficulty.EASY.value,
            caller_persona={},
            conversation_flow=[],
            expected_behaviors=[],
            success_criteria={},
            is_built_in=True,
        )
        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        # Create test runs with different statuses
        for status in [
            TestRunStatus.PENDING,
            TestRunStatus.RUNNING,
            TestRunStatus.PASSED,
            TestRunStatus.FAILED,
            TestRunStatus.ERROR,
        ]:
            test_run = TestRun(
                scenario_id=scenario.id,
                agent_id=agent.id,
                user_id=user.id,
                status=status.value,
            )
            test_session.add(test_run)

        await test_session.commit()

        # Query passed test runs
        result = await test_session.execute(
            select(TestRun).where(TestRun.status == TestRunStatus.PASSED.value)
        )
        passed_runs = result.scalars().all()

        assert len(passed_runs) == 1
        assert passed_runs[0].status == TestRunStatus.PASSED.value

        # Query failed test runs
        result = await test_session.execute(
            select(TestRun).where(TestRun.status == TestRunStatus.FAILED.value)
        )
        failed_runs = result.scalars().all()

        assert len(failed_runs) == 1
        assert failed_runs[0].status == TestRunStatus.FAILED.value
