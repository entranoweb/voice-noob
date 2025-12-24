"""Tests for TestRunner service (Week 2 - Task 13).

Comprehensive tests for the test execution engine.
All external dependencies (Anthropic API, database) are mocked.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.test_scenario import (
    ScenarioCategory,
    ScenarioDifficulty,
    TestRun,
    TestRunStatus,
    TestScenario,
)
from app.services.qa.test_runner import TestRunner


class TestTestRunnerInit:
    """Test TestRunner initialization."""

    def test_init_with_session(self, test_session: AsyncSession) -> None:
        """Test TestRunner initializes with database session."""
        runner = TestRunner(test_session)

        assert runner.db is test_session
        assert runner._client is None
        assert runner.logger is not None

    def test_init_creates_logger(self, test_session: AsyncSession) -> None:
        """Test that initialization creates a structured logger."""
        runner = TestRunner(test_session)

        # Logger should be bound with component name
        assert hasattr(runner.logger, "_context")


class TestGetClient:
    """Test _get_client method."""

    @pytest.mark.asyncio
    async def test_get_client_creates_anthropic_client(
        self,
        test_session: AsyncSession,
    ) -> None:
        """Test _get_client creates Anthropic client on first call."""
        runner = TestRunner(test_session)

        with patch("anthropic.AsyncAnthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_anthropic.return_value = mock_client

            with patch("app.services.qa.test_runner.settings") as mock_settings:
                mock_settings.ANTHROPIC_API_KEY = "test-key"

                client = await runner._get_client()

                assert client == mock_client
                mock_anthropic.assert_called_once_with(api_key="test-key")

    @pytest.mark.asyncio
    async def test_get_client_caches_client(
        self,
        test_session: AsyncSession,
    ) -> None:
        """Test _get_client caches client after first call."""
        runner = TestRunner(test_session)

        with patch("anthropic.AsyncAnthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_anthropic.return_value = mock_client

            with patch("app.services.qa.test_runner.settings") as mock_settings:
                mock_settings.ANTHROPIC_API_KEY = "test-key"

                # First call
                client1 = await runner._get_client()
                # Second call
                client2 = await runner._get_client()

                assert client1 == client2
                # Should only create client once
                assert mock_anthropic.call_count == 1

    @pytest.mark.asyncio
    async def test_get_client_raises_error_without_api_key(
        self,
        test_session: AsyncSession,
    ) -> None:
        """Test _get_client raises error when API key not configured."""
        runner = TestRunner(test_session)

        with patch("app.services.qa.test_runner.settings") as mock_settings:
            mock_settings.ANTHROPIC_API_KEY = None

            with pytest.raises(ValueError, match="ANTHROPIC_API_KEY not configured"):
                await runner._get_client()

    @pytest.mark.asyncio
    async def test_get_client_raises_error_without_anthropic_package(
        self,
        test_session: AsyncSession,
    ) -> None:
        """Test _get_client raises error when anthropic package not installed."""
        runner = TestRunner(test_session)

        with patch("builtins.__import__", side_effect=ImportError("No module named 'anthropic'")):
            with pytest.raises(ImportError, match="anthropic package not installed"):
                await runner._get_client()


class TestSeedBuiltInScenarios:
    """Test built-in scenario seeding."""

    @pytest.mark.asyncio
    async def test_seed_creates_scenarios(
        self,
        test_session: AsyncSession,
    ) -> None:
        """Test seed_built_in_scenarios creates scenarios."""
        runner = TestRunner(test_session)

        count = await runner.seed_built_in_scenarios()

        # Should create 12 built-in scenarios
        assert count == 12

    @pytest.mark.asyncio
    async def test_seed_is_idempotent(
        self,
        test_session: AsyncSession,
    ) -> None:
        """Test seed_built_in_scenarios is idempotent."""
        runner = TestRunner(test_session)

        # First seed
        count1 = await runner.seed_built_in_scenarios()
        assert count1 == 12

        # Second seed should create 0
        count2 = await runner.seed_built_in_scenarios()
        assert count2 == 0

    @pytest.mark.asyncio
    async def test_seed_creates_active_scenarios(
        self,
        test_session: AsyncSession,
    ) -> None:
        """Test seed_built_in_scenarios creates active scenarios."""
        runner = TestRunner(test_session)

        await runner.seed_built_in_scenarios()

        # Query scenarios
        from sqlalchemy import select

        result = await test_session.execute(
            select(TestScenario).where(TestScenario.is_built_in == True)  # noqa: E712
        )
        scenarios = result.scalars().all()

        # All should be active
        assert all(s.is_active for s in scenarios)
        assert all(s.is_built_in for s in scenarios)

    @pytest.mark.asyncio
    async def test_seed_creates_scenarios_with_categories(
        self,
        test_session: AsyncSession,
    ) -> None:
        """Test seed_built_in_scenarios creates scenarios across categories."""
        runner = TestRunner(test_session)

        await runner.seed_built_in_scenarios()

        from sqlalchemy import select

        result = await test_session.execute(
            select(TestScenario).where(TestScenario.is_built_in == True)  # noqa: E712
        )
        scenarios = result.scalars().all()

        # Should have multiple categories
        categories = {s.category for s in scenarios}
        assert len(categories) > 1
        assert "greeting" in categories
        assert "booking" in categories


class TestRunScenario:
    """Test running individual scenarios."""

    @pytest.mark.asyncio
    async def test_run_scenario_not_found(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Test run_scenario raises error for non-existent scenario."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id)

        runner = TestRunner(test_session)

        with pytest.raises(ValueError, match="Scenario .* not found"):
            await runner.run_scenario(
                scenario_id=uuid.uuid4(),
                agent_id=agent.id,
                user_id=user.id,
            )

    @pytest.mark.asyncio
    async def test_run_scenario_agent_not_found(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
    ) -> None:
        """Test run_scenario raises error for non-existent agent."""
        user = await create_test_user()

        # Create a scenario
        scenario = TestScenario(
            name="Test Scenario",
            category=ScenarioCategory.GREETING.value,
            difficulty=ScenarioDifficulty.EASY.value,
            caller_persona={"name": "Test"},
            conversation_flow=[{"speaker": "user", "message": "Hello"}],
            expected_behaviors=["Greet"],
            success_criteria={"min_score": 70},
            is_built_in=True,
        )
        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        runner = TestRunner(test_session)

        with pytest.raises(ValueError, match="Agent .* not found"):
            await runner.run_scenario(
                scenario_id=scenario.id,
                agent_id=uuid.uuid4(),
                user_id=user.id,
            )

    @pytest.mark.asyncio
    async def test_run_scenario_creates_test_run(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Test run_scenario creates a TestRun record."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id)

        # Create a scenario
        scenario = TestScenario(
            name="Test Scenario",
            category=ScenarioCategory.GREETING.value,
            difficulty=ScenarioDifficulty.EASY.value,
            caller_persona={"name": "Test Caller", "mood": "friendly"},
            conversation_flow=[{"speaker": "user", "message": "Hello there!"}],
            expected_behaviors=["Greet the caller warmly"],
            success_criteria={"min_score": 70},
            is_built_in=True,
        )
        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        runner = TestRunner(test_session)

        # Mock the conversation simulation and evaluation
        mock_conversation = [
            {
                "speaker": "user",
                "message": "Hello there!",
                "timestamp": datetime.now(UTC).isoformat(),
            },
            {
                "speaker": "agent",
                "message": "Hi! How can I help?",
                "timestamp": datetime.now(UTC).isoformat(),
            },
        ]
        mock_evaluation = {
            "overall_score": 85,
            "passed": True,
            "behavior_matches": {"Greet the caller warmly": True},
            "criteria_results": {"min_score": {"met": True, "reason": "Score 85 > 70"}},
            "issues_found": [],
            "recommendations": ["Great job!"],
        }

        with patch.object(
            runner, "_simulate_conversation", new_callable=AsyncMock
        ) as mock_simulate, patch.object(
            runner, "_evaluate_conversation", new_callable=AsyncMock
        ) as mock_evaluate:
            mock_simulate.return_value = mock_conversation
            mock_evaluate.return_value = mock_evaluation

            test_run = await runner.run_scenario(
                scenario_id=scenario.id,
                agent_id=agent.id,
                user_id=user.id,
            )

            assert test_run is not None
            assert test_run.scenario_id == scenario.id
            assert test_run.agent_id == agent.id
            assert test_run.user_id == user.id
            assert test_run.status == TestRunStatus.PASSED.value
            assert test_run.passed is True
            assert test_run.overall_score == 85

    @pytest.mark.asyncio
    async def test_run_scenario_with_workspace(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Test run_scenario includes workspace_id when provided."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id)
        workspace_id = uuid.uuid4()

        scenario = TestScenario(
            name="Test Scenario",
            category=ScenarioCategory.GREETING.value,
            difficulty=ScenarioDifficulty.EASY.value,
            caller_persona={"name": "Test"},
            conversation_flow=[{"speaker": "user", "message": "Hello"}],
            expected_behaviors=["Greet"],
            success_criteria={"min_score": 70},
            is_built_in=True,
        )
        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        runner = TestRunner(test_session)

        with patch.object(runner, "_simulate_conversation", new_callable=AsyncMock):
            with patch.object(
                runner, "_evaluate_conversation", new_callable=AsyncMock
            ) as mock_eval:
                mock_eval.return_value = {
                    "overall_score": 80,
                    "passed": True,
                    "behavior_matches": {},
                    "criteria_results": {},
                    "issues_found": [],
                    "recommendations": [],
                }

                test_run = await runner.run_scenario(
                    scenario_id=scenario.id,
                    agent_id=agent.id,
                    user_id=user.id,
                    workspace_id=workspace_id,
                )

                assert test_run.workspace_id == workspace_id

    @pytest.mark.asyncio
    async def test_run_scenario_handles_failure(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Test run_scenario marks test as failed when evaluation fails."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id)

        scenario = TestScenario(
            name="Test Scenario",
            category=ScenarioCategory.GREETING.value,
            difficulty=ScenarioDifficulty.EASY.value,
            caller_persona={"name": "Test"},
            conversation_flow=[{"speaker": "user", "message": "Hello"}],
            expected_behaviors=["Greet"],
            success_criteria={"min_score": 70},
            is_built_in=True,
        )
        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        runner = TestRunner(test_session)

        mock_evaluation = {
            "overall_score": 50,
            "passed": False,
            "behavior_matches": {"Greet": False},
            "criteria_results": {},
            "issues_found": ["Did not greet properly"],
            "recommendations": ["Improve greeting"],
        }

        with patch.object(runner, "_simulate_conversation", new_callable=AsyncMock):
            with patch.object(
                runner, "_evaluate_conversation", new_callable=AsyncMock
            ) as mock_eval:
                mock_eval.return_value = mock_evaluation

                test_run = await runner.run_scenario(
                    scenario_id=scenario.id,
                    agent_id=agent.id,
                    user_id=user.id,
                )

                assert test_run.status == TestRunStatus.FAILED.value
                assert test_run.passed is False
                assert test_run.overall_score == 50

    @pytest.mark.asyncio
    async def test_run_scenario_handles_exception(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Test run_scenario marks test as error when exception occurs."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id)

        scenario = TestScenario(
            name="Test Scenario",
            category=ScenarioCategory.GREETING.value,
            difficulty=ScenarioDifficulty.EASY.value,
            caller_persona={"name": "Test"},
            conversation_flow=[{"speaker": "user", "message": "Hello"}],
            expected_behaviors=["Greet"],
            success_criteria={"min_score": 70},
            is_built_in=True,
        )
        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)

        runner = TestRunner(test_session)

        with patch.object(
            runner, "_simulate_conversation", new_callable=AsyncMock
        ) as mock_simulate:
            mock_simulate.side_effect = Exception("API Error")

            test_run = await runner.run_scenario(
                scenario_id=scenario.id,
                agent_id=agent.id,
                user_id=user.id,
            )

            assert test_run.status == TestRunStatus.ERROR.value
            assert test_run.error_message == "API Error"
            assert test_run.completed_at is not None


class TestSimulateConversation:
    """Test _simulate_conversation method."""

    @pytest.mark.asyncio
    async def test_simulate_conversation_generates_responses(
        self,
        test_session: AsyncSession,
    ) -> None:
        """Test _simulate_conversation generates agent responses."""
        runner = TestRunner(test_session)

        agent = Agent(
            id=uuid.uuid4(),
            name="Test Agent",
            system_prompt="You are a helpful assistant.",
            user_id=1,
        )

        scenario = TestScenario(
            id=uuid.uuid4(),
            name="Test",
            category="greeting",
            difficulty="easy",
            caller_persona={},
            conversation_flow=[
                {"speaker": "user", "message": "Hello"},
                {"speaker": "user", "message": "How are you?"},
            ],
            expected_behaviors=[],
            success_criteria={},
        )

        # Mock Anthropic client
        mock_response = Mock()
        mock_response.content = [Mock(text="I'm doing great, thanks!")]

        mock_client = Mock()
        mock_client.messages = Mock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch.object(runner, "_get_client", return_value=mock_client):
            conversation = await runner._simulate_conversation(agent, scenario)

            # Should have user + agent responses for each turn
            assert len(conversation) == 4
            assert conversation[0]["speaker"] == "user"
            assert conversation[0]["message"] == "Hello"
            assert conversation[1]["speaker"] == "agent"
            assert conversation[2]["speaker"] == "user"
            assert conversation[3]["speaker"] == "agent"

    @pytest.mark.asyncio
    async def test_simulate_conversation_uses_agent_system_prompt(
        self,
        test_session: AsyncSession,
    ) -> None:
        """Test _simulate_conversation uses agent's system prompt."""
        runner = TestRunner(test_session)

        system_prompt = "You are a specialized customer service agent."
        agent = Agent(
            id=uuid.uuid4(),
            name="Test Agent",
            system_prompt=system_prompt,
            user_id=1,
        )

        scenario = TestScenario(
            id=uuid.uuid4(),
            name="Test",
            category="greeting",
            difficulty="easy",
            caller_persona={},
            conversation_flow=[{"speaker": "user", "message": "Hello"}],
            expected_behaviors=[],
            success_criteria={},
        )

        mock_response = Mock()
        mock_response.content = [Mock(text="Hi there!")]

        mock_client = Mock()
        mock_client.messages = Mock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch.object(runner, "_get_client", return_value=mock_client):
            await runner._simulate_conversation(agent, scenario)

            # Verify system prompt was used
            call_args = mock_client.messages.create.call_args
            assert call_args.kwargs["system"] == system_prompt


class TestEvaluateConversation:
    """Test _evaluate_conversation method."""

    @pytest.mark.asyncio
    async def test_evaluate_conversation_returns_evaluation(
        self,
        test_session: AsyncSession,
    ) -> None:
        """Test _evaluate_conversation returns evaluation results."""
        runner = TestRunner(test_session)

        agent = Agent(
            id=uuid.uuid4(),
            name="Test Agent",
            system_prompt="You are helpful.",
            user_id=1,
        )

        scenario = TestScenario(
            id=uuid.uuid4(),
            name="Test",
            category="greeting",
            difficulty="easy",
            caller_persona={"name": "Test Caller"},
            conversation_flow=[],
            expected_behaviors=["Greet warmly", "Be professional"],
            success_criteria={"min_score": 70},
        )

        conversation = [
            {"speaker": "user", "message": "Hello"},
            {"speaker": "agent", "message": "Hi! How can I help?"},
        ]

        evaluation_json = {
            "overall_score": 85,
            "passed": True,
            "behavior_matches": {"Greet warmly": True, "Be professional": True},
            "criteria_results": {"min_score": {"met": True, "reason": "Score is 85"}},
            "issues_found": [],
            "recommendations": ["Great job!"],
        }

        mock_response = Mock()
        mock_response.content = [Mock(text=json.dumps(evaluation_json))]

        mock_client = Mock()
        mock_client.messages = Mock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch.object(runner, "_get_client", return_value=mock_client):
            result = await runner._evaluate_conversation(agent, scenario, conversation)

            assert result["overall_score"] == 85
            assert result["passed"] is True
            assert "behavior_matches" in result
            assert "criteria_results" in result

    @pytest.mark.asyncio
    async def test_evaluate_conversation_handles_markdown_json(
        self,
        test_session: AsyncSession,
    ) -> None:
        """Test _evaluate_conversation parses JSON from markdown blocks."""
        runner = TestRunner(test_session)

        agent = Agent(id=uuid.uuid4(), name="Test", system_prompt="Test", user_id=1)
        scenario = TestScenario(
            id=uuid.uuid4(),
            name="Test",
            category="greeting",
            difficulty="easy",
            caller_persona={},
            conversation_flow=[],
            expected_behaviors=["Greet"],
            success_criteria={},
        )
        conversation = [{"speaker": "user", "message": "Hi"}]

        evaluation_json = {"overall_score": 75, "passed": True}
        markdown_response = f"```json\n{json.dumps(evaluation_json)}\n```"

        mock_response = Mock()
        mock_response.content = [Mock(text=markdown_response)]

        mock_client = Mock()
        mock_client.messages = Mock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch.object(runner, "_get_client", return_value=mock_client):
            result = await runner._evaluate_conversation(agent, scenario, conversation)

            assert result["overall_score"] == 75
            assert result["passed"] is True

    @pytest.mark.asyncio
    async def test_evaluate_conversation_handles_parse_failure(
        self,
        test_session: AsyncSession,
    ) -> None:
        """Test _evaluate_conversation returns default on parse failure."""
        runner = TestRunner(test_session)

        agent = Agent(id=uuid.uuid4(), name="Test", system_prompt="Test", user_id=1)
        scenario = TestScenario(
            id=uuid.uuid4(),
            name="Test",
            category="greeting",
            difficulty="easy",
            caller_persona={},
            conversation_flow=[],
            expected_behaviors=["Greet"],
            success_criteria={},
        )
        conversation = [{"speaker": "user", "message": "Hi"}]

        mock_response = Mock()
        mock_response.content = [Mock(text="This is not valid JSON")]

        mock_client = Mock()
        mock_client.messages = Mock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch.object(runner, "_get_client", return_value=mock_client):
            result = await runner._evaluate_conversation(agent, scenario, conversation)

            # Should return default values
            assert result["overall_score"] == 50
            assert result["passed"] is False
            assert "Failed to parse evaluation response" in result["issues_found"]


class TestRunAllScenarios:
    """Test running all scenarios."""

    @pytest.mark.asyncio
    async def test_run_all_scenarios_empty(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Test run_all_scenarios with no scenarios returns empty list."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id)

        runner = TestRunner(test_session)

        results = await runner.run_all_scenarios(
            agent_id=agent.id,
            user_id=user.id,
        )

        assert results == []

    @pytest.mark.asyncio
    async def test_run_all_scenarios_runs_all_active(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Test run_all_scenarios runs all active scenarios."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id)

        # Create active and inactive scenarios
        active1 = TestScenario(
            name="Active 1",
            category="greeting",
            difficulty="easy",
            caller_persona={},
            conversation_flow=[{"speaker": "user", "message": "Hi"}],
            expected_behaviors=[],
            success_criteria={},
            is_active=True,
        )
        active2 = TestScenario(
            name="Active 2",
            category="booking",
            difficulty="medium",
            caller_persona={},
            conversation_flow=[{"speaker": "user", "message": "Hi"}],
            expected_behaviors=[],
            success_criteria={},
            is_active=True,
        )
        inactive = TestScenario(
            name="Inactive",
            category="greeting",
            difficulty="easy",
            caller_persona={},
            conversation_flow=[{"speaker": "user", "message": "Hi"}],
            expected_behaviors=[],
            success_criteria={},
            is_active=False,
        )
        test_session.add_all([active1, active2, inactive])
        await test_session.commit()

        runner = TestRunner(test_session)

        with patch.object(runner, "run_scenario", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = TestRun(
                scenario_id=uuid.uuid4(),
                agent_id=agent.id,
                user_id=user.id,
                status=TestRunStatus.PASSED.value,
            )

            results = await runner.run_all_scenarios(
                agent_id=agent.id,
                user_id=user.id,
            )

            # Should run both active scenarios
            assert mock_run.call_count == 2

    @pytest.mark.asyncio
    async def test_run_all_scenarios_with_category_filter(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Test run_all_scenarios filters by category."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id)

        # Create scenarios in different categories
        greeting_scenario = TestScenario(
            name="Greeting Test",
            category=ScenarioCategory.GREETING.value,
            difficulty=ScenarioDifficulty.EASY.value,
            caller_persona={},
            conversation_flow=[{"speaker": "user", "message": "Hi"}],
            expected_behaviors=[],
            success_criteria={},
            is_active=True,
        )
        booking_scenario = TestScenario(
            name="Booking Test",
            category=ScenarioCategory.BOOKING.value,
            difficulty=ScenarioDifficulty.MEDIUM.value,
            caller_persona={},
            conversation_flow=[{"speaker": "user", "message": "Hi"}],
            expected_behaviors=[],
            success_criteria={},
            is_active=True,
        )
        test_session.add_all([greeting_scenario, booking_scenario])
        await test_session.commit()

        runner = TestRunner(test_session)

        with patch.object(runner, "run_scenario", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = TestRun(
                scenario_id=uuid.uuid4(),
                agent_id=agent.id,
                user_id=user.id,
                status=TestRunStatus.PASSED.value,
            )

            # Run only greeting scenarios
            await runner.run_all_scenarios(
                agent_id=agent.id,
                user_id=user.id,
                category="greeting",
            )

            # Should only run greeting scenario
            assert mock_run.call_count == 1

    @pytest.mark.asyncio
    async def test_run_all_scenarios_continues_on_error(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Test run_all_scenarios continues even if one scenario fails."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id)

        scenario1 = TestScenario(
            name="Scenario 1",
            category="greeting",
            difficulty="easy",
            caller_persona={},
            conversation_flow=[{"speaker": "user", "message": "Hi"}],
            expected_behaviors=[],
            success_criteria={},
            is_active=True,
        )
        scenario2 = TestScenario(
            name="Scenario 2",
            category="greeting",
            difficulty="easy",
            caller_persona={},
            conversation_flow=[{"speaker": "user", "message": "Hi"}],
            expected_behaviors=[],
            success_criteria={},
            is_active=True,
        )
        test_session.add_all([scenario1, scenario2])
        await test_session.commit()

        runner = TestRunner(test_session)

        call_count = 0

        async def mock_run_scenario(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("First scenario failed")  # noqa: TRY002
            return TestRun(
                scenario_id=uuid.uuid4(),
                agent_id=agent.id,
                user_id=user.id,
                status=TestRunStatus.PASSED.value,
            )

        with patch.object(runner, "run_scenario", side_effect=mock_run_scenario):
            results = await runner.run_all_scenarios(
                agent_id=agent.id,
                user_id=user.id,
            )

            # Should have run both scenarios but only returned successful one
            assert call_count == 2
            assert len(results) == 1


class TestSeedScenariosBackground:
    """Test background scenario seeding function."""

    @pytest.mark.asyncio
    async def test_seed_scenarios_background_creates_session(self) -> None:
        """Test seed_scenarios_background creates its own session."""
        from app.services.qa.test_runner import seed_scenarios_background

        with patch("app.services.qa.test_runner.AsyncSessionLocal") as mock_session_factory:
            mock_session = AsyncMock()
            mock_session_factory.return_value.__aenter__.return_value = mock_session

            with patch.object(
                TestRunner, "seed_built_in_scenarios", new_callable=AsyncMock
            ) as mock_seed:
                mock_seed.return_value = 12

                await seed_scenarios_background()

                mock_seed.assert_called_once()

    @pytest.mark.asyncio
    async def test_seed_scenarios_background_handles_errors(self) -> None:
        """Test seed_scenarios_background handles errors gracefully."""
        from app.services.qa.test_runner import seed_scenarios_background

        with patch("app.services.qa.test_runner.AsyncSessionLocal") as mock_session_factory:
            mock_session_factory.side_effect = Exception("Database error")

            # Should not raise
            await seed_scenarios_background()


class TestTestRunnerIntegration:
    """Integration tests for TestRunner (with mocked AI)."""

    @pytest.mark.asyncio
    async def test_full_test_run_flow(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_agent: Any,
    ) -> None:
        """Test complete flow: seed -> run -> verify results."""
        user = await create_test_user()
        agent = await create_test_agent(user_id=user.id)

        runner = TestRunner(test_session)

        # Seed scenarios
        count = await runner.seed_built_in_scenarios()
        assert count == 12

        # Mock conversation and evaluation
        mock_conversation = [
            {"speaker": "user", "message": "Hello"},
            {"speaker": "agent", "message": "Hi! How can I help?"},
        ]
        mock_evaluation = {
            "overall_score": 85,
            "passed": True,
            "behavior_matches": {},
            "criteria_results": {},
            "issues_found": [],
            "recommendations": ["Great job!"],
        }

        with patch.object(runner, "_simulate_conversation", new_callable=AsyncMock) as mock_sim:
            with patch.object(
                runner, "_evaluate_conversation", new_callable=AsyncMock
            ) as mock_eval:
                mock_sim.return_value = mock_conversation
                mock_eval.return_value = mock_evaluation

                # Run all scenarios
                results = await runner.run_all_scenarios(
                    agent_id=agent.id,
                    user_id=user.id,
                )

                # Verify results
                assert len(results) == 12
                for result in results:
                    assert result.status in [
                        TestRunStatus.PASSED.value,
                        TestRunStatus.FAILED.value,
                        TestRunStatus.ERROR.value,
                    ]
                    assert result.agent_id == agent.id
                    assert result.user_id == user.id
                    assert result.duration_ms is not None
