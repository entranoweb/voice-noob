"""Tests for Testing API endpoints (Week 2 - Task 14).

Tests for pre-deployment testing scenarios and test runs.

Comprehensive test coverage including:
- Scenario listing, filtering, and retrieval
- Category and difficulty enumeration
- Scenario seeding (idempotent)
- Test execution (single and batch)
- Test run listing and retrieval
- Testing summary statistics
- Multi-tenant isolation
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.test_scenario import (
    ScenarioCategory,
    ScenarioDifficulty,
    TestRun,
    TestRunStatus,
    TestScenario,
)
from app.models.user import User

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest_asyncio.fixture
async def create_test_scenario(test_session: AsyncSession) -> Any:
    """Factory fixture to create test scenarios."""

    async def _create_scenario(
        user_id: int | None = None,
        is_built_in: bool = False,
        **kwargs: Any,
    ) -> TestScenario:
        """Create a test scenario.

        Args:
            user_id: Owner user ID (None for built-in)
            is_built_in: Whether this is a built-in scenario
            **kwargs: Additional scenario fields

        Returns:
            Created TestScenario
        """
        scenario_data = {
            "name": "Test Scenario",
            "description": "Test scenario description",
            "category": ScenarioCategory.GREETING.value,
            "difficulty": ScenarioDifficulty.EASY.value,
            "caller_persona": {"name": "Test Caller", "mood": "friendly"},
            "conversation_flow": [
                {"speaker": "user", "message": "Hello"},
                {"speaker": "agent", "message": "Hi there!"},
            ],
            "expected_behaviors": ["Greet the caller", "Be friendly"],
            "success_criteria": {"min_score": 70, "required_behaviors": ["Greet the caller"]},
            "is_active": True,
            "is_built_in": is_built_in,
            "user_id": user_id,
        }
        scenario_data.update(kwargs)
        scenario = TestScenario(**scenario_data)
        test_session.add(scenario)
        await test_session.commit()
        await test_session.refresh(scenario)
        return scenario

    return _create_scenario


@pytest_asyncio.fixture
async def create_test_run_record(test_session: AsyncSession) -> Any:
    """Factory fixture to create test run records."""

    async def _create_run(
        scenario_id: uuid.UUID,
        agent_id: uuid.UUID,
        user_id: int,
        **kwargs: Any,
    ) -> TestRun:
        """Create a test run record.

        Args:
            scenario_id: Scenario ID
            agent_id: Agent ID
            user_id: User ID
            **kwargs: Additional test run fields

        Returns:
            Created TestRun
        """
        from datetime import UTC, datetime

        run_data = {
            "scenario_id": scenario_id,
            "agent_id": agent_id,
            "user_id": user_id,
            "status": TestRunStatus.PASSED.value,
            "started_at": datetime.now(UTC),
            "completed_at": datetime.now(UTC),
            "duration_ms": 1500,
            "overall_score": 85,
            "passed": True,
            "actual_transcript": [
                {"speaker": "user", "message": "Hello"},
                {"speaker": "agent", "message": "Hi there!"},
            ],
            "behavior_matches": {"Greet the caller": True},
            "criteria_results": {"min_score": {"met": True}},
            "issues_found": [],
            "recommendations": ["Consider adding more personalization"],
        }
        run_data.update(kwargs)
        test_run = TestRun(**run_data)
        test_session.add(test_run)
        await test_session.commit()
        await test_session.refresh(test_run)
        return test_run

    return _create_run


# =============================================================================
# Scenario Endpoints Tests
# =============================================================================


class TestScenarioEndpoints:
    """Test scenario management endpoints."""

    @pytest.mark.asyncio
    async def test_list_scenarios_empty(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test GET /testing/scenarios returns empty list when no scenarios."""
        client, _user = authenticated_test_client

        response = await client.get("/api/v1/testing/scenarios")

        assert response.status_code == 200
        data = response.json()
        assert "scenarios" in data
        assert "total" in data
        assert "page" in data
        assert "page_size" in data
        assert "total_pages" in data
        assert data["total"] >= 0
        assert isinstance(data["scenarios"], list)

    @pytest.mark.asyncio
    async def test_list_scenarios_with_data(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: AsyncSession,
        create_test_scenario: Any,
    ) -> None:
        """Test GET /testing/scenarios returns scenarios with data."""
        client, user = authenticated_test_client

        # Create built-in scenario (visible to all users)
        await create_test_scenario(is_built_in=True, name="Built-in Scenario")

        # Create user's own scenario
        await create_test_scenario(user_id=user.id, name="User Scenario")

        response = await client.get("/api/v1/testing/scenarios")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] >= 2  # At least 2 scenarios
        assert len(data["scenarios"]) >= 2

        # Check scenario structure
        scenario = data["scenarios"][0]
        assert "id" in scenario
        assert "name" in scenario
        assert "category" in scenario
        assert "difficulty" in scenario
        assert "is_built_in" in scenario
        assert "expected_behaviors" in scenario
        assert "success_criteria" in scenario

    @pytest.mark.asyncio
    async def test_list_scenarios_multi_tenant_isolation(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: AsyncSession,
        create_test_scenario: Any,
        create_test_user: Any,
    ) -> None:
        """Test that users only see built-in scenarios and their own scenarios."""
        client, user = authenticated_test_client

        # Create another user
        other_user = await create_test_user(email="other@example.com")

        # Create scenarios
        await create_test_scenario(is_built_in=True, name="Built-in Scenario")
        await create_test_scenario(user_id=user.id, name="User1 Scenario")
        await create_test_scenario(user_id=other_user.id, name="User2 Scenario")

        response = await client.get("/api/v1/testing/scenarios")

        assert response.status_code == 200
        data = response.json()

        # User should see: built-in + their own (not other user's)
        scenario_names = [s["name"] for s in data["scenarios"]]
        assert "Built-in Scenario" in scenario_names
        assert "User1 Scenario" in scenario_names
        assert "User2 Scenario" not in scenario_names

    @pytest.mark.asyncio
    async def test_list_scenarios_with_pagination(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: AsyncSession,
        create_test_scenario: Any,
    ) -> None:
        """Test GET /testing/scenarios with pagination params."""
        client, user = authenticated_test_client

        # Create 5 scenarios
        for i in range(5):
            await create_test_scenario(user_id=user.id, name=f"Scenario {i}")

        response = await client.get(
            "/api/v1/testing/scenarios",
            params={"page": 1, "page_size": 2},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["page"] == 1
        assert data["page_size"] == 2
        assert len(data["scenarios"]) == 2
        assert data["total"] >= 5
        assert data["total_pages"] >= 3

        # Test page 2
        response2 = await client.get(
            "/api/v1/testing/scenarios",
            params={"page": 2, "page_size": 2},
        )
        assert response2.status_code == 200
        data2 = response2.json()
        assert data2["page"] == 2

    @pytest.mark.asyncio
    async def test_list_scenarios_filter_by_category(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: AsyncSession,
        create_test_scenario: Any,
    ) -> None:
        """Test GET /testing/scenarios with category filter."""
        client, _user = authenticated_test_client

        # Create scenarios in different categories
        await create_test_scenario(
            is_built_in=True,
            category=ScenarioCategory.GREETING.value,
            name="Greeting Test",
        )
        await create_test_scenario(
            is_built_in=True,
            category=ScenarioCategory.BOOKING.value,
            name="Booking Test",
        )
        await create_test_scenario(
            is_built_in=True,
            category=ScenarioCategory.OBJECTION.value,
            name="Objection Test",
        )

        response = await client.get(
            "/api/v1/testing/scenarios",
            params={"category": "greeting"},
        )

        assert response.status_code == 200
        data = response.json()
        # All returned scenarios should be in the greeting category
        for scenario in data["scenarios"]:
            assert scenario["category"] == "greeting"
        assert data["total"] >= 1

    @pytest.mark.asyncio
    async def test_list_scenarios_filter_by_difficulty(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: AsyncSession,
        create_test_scenario: Any,
    ) -> None:
        """Test GET /testing/scenarios with difficulty filter."""
        client, _user = authenticated_test_client

        # Create scenarios with different difficulties
        await create_test_scenario(
            is_built_in=True,
            difficulty=ScenarioDifficulty.EASY.value,
            name="Easy Test",
        )
        await create_test_scenario(
            is_built_in=True,
            difficulty=ScenarioDifficulty.HARD.value,
            name="Hard Test",
        )

        response = await client.get(
            "/api/v1/testing/scenarios",
            params={"difficulty": "hard"},
        )

        assert response.status_code == 200
        data = response.json()
        # All returned scenarios should be hard difficulty
        for scenario in data["scenarios"]:
            assert scenario["difficulty"] == "hard"

    @pytest.mark.asyncio
    async def test_list_scenarios_filter_built_in_only(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: AsyncSession,
        create_test_scenario: Any,
    ) -> None:
        """Test GET /testing/scenarios with built_in_only filter."""
        client, user = authenticated_test_client

        # Create built-in and user scenarios
        await create_test_scenario(is_built_in=True, name="Built-in 1")
        await create_test_scenario(is_built_in=True, name="Built-in 2")
        await create_test_scenario(user_id=user.id, name="User Scenario")

        response = await client.get(
            "/api/v1/testing/scenarios",
            params={"built_in_only": True},
        )

        assert response.status_code == 200
        data = response.json()
        # All returned scenarios should be built-in
        for scenario in data["scenarios"]:
            assert scenario["is_built_in"] is True
        assert data["total"] >= 2

    @pytest.mark.asyncio
    async def test_get_scenario_success(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: AsyncSession,
        create_test_scenario: Any,
    ) -> None:
        """Test GET /testing/scenarios/{id} returns scenario details."""
        client, user = authenticated_test_client

        # Create a scenario
        scenario = await create_test_scenario(
            user_id=user.id,
            name="Test Scenario Details",
            description="Detailed test scenario",
        )

        response = await client.get(f"/api/v1/testing/scenarios/{scenario.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(scenario.id)
        assert data["name"] == "Test Scenario Details"
        assert data["description"] == "Detailed test scenario"
        assert "caller_persona" in data
        assert "expected_behaviors" in data
        assert "success_criteria" in data
        assert "is_active" in data

    @pytest.mark.asyncio
    async def test_get_scenario_built_in_accessible(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: AsyncSession,
        create_test_scenario: Any,
    ) -> None:
        """Test that users can access built-in scenarios."""
        client, _user = authenticated_test_client

        # Create a built-in scenario
        scenario = await create_test_scenario(is_built_in=True, name="Built-in Test")

        response = await client.get(f"/api/v1/testing/scenarios/{scenario.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["is_built_in"] is True

    @pytest.mark.asyncio
    async def test_get_scenario_other_user_not_accessible(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: AsyncSession,
        create_test_scenario: Any,
        create_test_user: Any,
    ) -> None:
        """Test that users cannot access other users' scenarios."""
        client, _user = authenticated_test_client

        # Create scenario for another user
        other_user = await create_test_user(email="other@example.com")
        scenario = await create_test_scenario(
            user_id=other_user.id,
            name="Other User Scenario",
        )

        response = await client.get(f"/api/v1/testing/scenarios/{scenario.id}")

        # Should return 404 (not found) due to multi-tenant isolation
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_scenario_not_found(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test GET /testing/scenarios/{id} returns 404 for non-existent scenario."""
        client, _user = authenticated_test_client

        fake_id = str(uuid.uuid4())
        response = await client.get(f"/api/v1/testing/scenarios/{fake_id}")

        assert response.status_code == 404
        data = response.json()
        assert "detail" in data

    @pytest.mark.asyncio
    async def test_get_scenario_invalid_uuid(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test GET /testing/scenarios/{id} returns 400 for invalid UUID."""
        client, _user = authenticated_test_client

        response = await client.get("/api/v1/testing/scenarios/not-a-uuid")

        assert response.status_code == 400
        data = response.json()
        assert "detail" in data

    @pytest.mark.asyncio
    async def test_list_categories(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test GET /testing/categories returns available categories."""
        client, _user = authenticated_test_client

        response = await client.get("/api/v1/testing/categories")

        assert response.status_code == 200
        data = response.json()
        assert "categories" in data
        assert "difficulties" in data
        assert isinstance(data["categories"], list)
        assert isinstance(data["difficulties"], list)

        # Check expected categories exist
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
            assert cat in data["categories"]

        # Check expected difficulties exist
        assert "easy" in data["difficulties"]
        assert "medium" in data["difficulties"]
        assert "hard" in data["difficulties"]


# =============================================================================
# Seed Scenarios Tests
# =============================================================================


class TestSeedScenariosEndpoint:
    """Test scenario seeding endpoint."""

    @pytest.mark.asyncio
    async def test_seed_scenarios(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test POST /testing/scenarios/seed creates built-in scenarios."""
        client, _user = authenticated_test_client

        response = await client.post("/api/v1/testing/scenarios/seed")

        assert response.status_code == 200
        data = response.json()
        assert "message" in data
        assert "scenarios_created" in data
        assert isinstance(data["scenarios_created"], int)
        # First time should create scenarios
        assert data["scenarios_created"] >= 0

    @pytest.mark.asyncio
    async def test_seed_scenarios_idempotent(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test POST /testing/scenarios/seed is idempotent."""
        client, _user = authenticated_test_client

        # First seed
        response1 = await client.post("/api/v1/testing/scenarios/seed")
        assert response1.status_code == 200
        count1 = response1.json()["scenarios_created"]

        # Second seed should create 0 new scenarios (already seeded)
        response2 = await client.post("/api/v1/testing/scenarios/seed")
        assert response2.status_code == 200
        count2 = response2.json()["scenarios_created"]

        # Second call should create 0
        assert count2 == 0

        # Verify scenarios are in database
        list_response = await client.get("/api/v1/testing/scenarios")
        assert list_response.status_code == 200
        assert list_response.json()["total"] >= count1


# =============================================================================
# Run Test Endpoints Tests
# =============================================================================


class TestRunTestEndpoints:
    """Test test execution endpoints."""

    @pytest.mark.asyncio
    async def test_run_test_qa_disabled(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test POST /testing/run returns 400 when QA is disabled."""
        client, _user = authenticated_test_client

        with patch("app.api.testing.settings") as mock_settings:
            mock_settings.QA_ENABLED = False

            response = await client.post(
                "/api/v1/testing/run",
                json={
                    "scenario_id": str(uuid.uuid4()),
                    "agent_id": str(uuid.uuid4()),
                },
            )

            assert response.status_code == 400
            data = response.json()
            assert "detail" in data
            assert "disabled" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_run_test_no_anthropic_key(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test POST /testing/run returns 400 when Anthropic API key not configured."""
        client, _user = authenticated_test_client

        with patch("app.api.testing.settings") as mock_settings:
            mock_settings.QA_ENABLED = True
            mock_settings.ANTHROPIC_API_KEY = None

            response = await client.post(
                "/api/v1/testing/run",
                json={
                    "scenario_id": str(uuid.uuid4()),
                    "agent_id": str(uuid.uuid4()),
                },
            )

            assert response.status_code == 400
            data = response.json()
            assert "detail" in data
            assert "api key" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_run_test_scenario_not_found(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test POST /testing/run returns 404 for non-existent scenario."""
        client, _user = authenticated_test_client

        with patch("app.api.testing.settings") as mock_settings:
            mock_settings.QA_ENABLED = True
            mock_settings.ANTHROPIC_API_KEY = "test-key"

            response = await client.post(
                "/api/v1/testing/run",
                json={
                    "scenario_id": str(uuid.uuid4()),
                    "agent_id": str(uuid.uuid4()),
                },
            )

            # Should return 404 (scenario not found)
            assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_run_test_agent_not_found(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: AsyncSession,
        create_test_scenario: Any,
    ) -> None:
        """Test POST /testing/run returns 404 for non-existent agent."""
        client, user = authenticated_test_client

        # Create a scenario
        scenario = await create_test_scenario(is_built_in=True)

        with patch("app.api.testing.settings") as mock_settings:
            mock_settings.QA_ENABLED = True
            mock_settings.ANTHROPIC_API_KEY = "test-key"

            response = await client.post(
                "/api/v1/testing/run",
                json={
                    "scenario_id": str(scenario.id),
                    "agent_id": str(uuid.uuid4()),  # Non-existent agent
                },
            )

            # Should return 404 (agent not found)
            assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_run_test_success_mock(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: AsyncSession,
        create_test_scenario: Any,
        create_test_agent: Any,
    ) -> None:
        """Test POST /testing/run succeeds with mocked test runner."""
        client, user = authenticated_test_client

        # Create scenario and agent
        scenario = await create_test_scenario(is_built_in=True, name="Mock Test")
        agent = await create_test_agent(user_id=user.id, name="Test Agent")

        with patch("app.api.testing.settings") as mock_settings:
            mock_settings.QA_ENABLED = True
            mock_settings.ANTHROPIC_API_KEY = "test-key"

            with patch("app.api.testing.TestRunner") as MockRunner:
                # Mock the test runner
                mock_runner = AsyncMock()
                mock_test_run = TestRun(
                    id=uuid.uuid4(),
                    scenario_id=scenario.id,
                    agent_id=agent.id,
                    user_id=user.id,
                    status=TestRunStatus.PASSED.value,
                )
                mock_runner.run_scenario = AsyncMock(return_value=mock_test_run)
                MockRunner.return_value = mock_runner

                response = await client.post(
                    "/api/v1/testing/run",
                    json={
                        "scenario_id": str(scenario.id),
                        "agent_id": str(agent.id),
                    },
                )

                assert response.status_code == 200
                data = response.json()
                assert "message" in data
                assert "test_run_id" in data
                assert "status" in data

    @pytest.mark.asyncio
    async def test_run_all_tests_qa_disabled(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test POST /testing/run-all returns 400 when QA is disabled."""
        client, _user = authenticated_test_client

        with patch("app.api.testing.settings") as mock_settings:
            mock_settings.QA_ENABLED = False

            response = await client.post(
                "/api/v1/testing/run-all",
                json={"agent_id": str(uuid.uuid4())},
            )

            assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_run_all_tests_agent_not_found(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test POST /testing/run-all returns 404 for non-existent agent."""
        client, _user = authenticated_test_client

        with patch("app.api.testing.settings") as mock_settings:
            mock_settings.QA_ENABLED = True
            mock_settings.ANTHROPIC_API_KEY = "test-key"

            response = await client.post(
                "/api/v1/testing/run-all",
                json={"agent_id": str(uuid.uuid4())},
            )

            # Should return 404 (agent not found)
            assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_run_all_tests_no_scenarios(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: AsyncSession,
        create_test_agent: Any,
    ) -> None:
        """Test POST /testing/run-all returns 400 when no scenarios available."""
        client, user = authenticated_test_client

        # Create agent
        agent = await create_test_agent(user_id=user.id)

        with patch("app.api.testing.settings") as mock_settings:
            mock_settings.QA_ENABLED = True
            mock_settings.ANTHROPIC_API_KEY = "test-key"

            response = await client.post(
                "/api/v1/testing/run-all",
                json={"agent_id": str(agent.id)},
            )

            # Should return 400 (no scenarios available)
            assert response.status_code == 400
            data = response.json()
            assert "no scenarios" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_run_all_tests_success(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: AsyncSession,
        create_test_scenario: Any,
        create_test_agent: Any,
    ) -> None:
        """Test POST /testing/run-all queues tests successfully."""
        client, user = authenticated_test_client

        # Create scenarios and agent
        await create_test_scenario(is_built_in=True, name="Test 1")
        await create_test_scenario(is_built_in=True, name="Test 2")
        agent = await create_test_agent(user_id=user.id)

        with patch("app.api.testing.settings") as mock_settings:
            mock_settings.QA_ENABLED = True
            mock_settings.ANTHROPIC_API_KEY = "test-key"

            response = await client.post(
                "/api/v1/testing/run-all",
                json={"agent_id": str(agent.id)},
            )

            assert response.status_code == 200
            data = response.json()
            assert "message" in data
            assert "test_count" in data
            assert "queued" in data
            assert data["test_count"] >= 2
            assert data["queued"] is True

    @pytest.mark.asyncio
    async def test_run_all_tests_with_category_filter(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: AsyncSession,
        create_test_scenario: Any,
        create_test_agent: Any,
    ) -> None:
        """Test POST /testing/run-all with category filter."""
        client, user = authenticated_test_client

        # Create scenarios in different categories
        await create_test_scenario(
            is_built_in=True,
            category=ScenarioCategory.GREETING.value,
            name="Greeting",
        )
        await create_test_scenario(
            is_built_in=True,
            category=ScenarioCategory.BOOKING.value,
            name="Booking",
        )
        agent = await create_test_agent(user_id=user.id)

        with patch("app.api.testing.settings") as mock_settings:
            mock_settings.QA_ENABLED = True
            mock_settings.ANTHROPIC_API_KEY = "test-key"

            response = await client.post(
                "/api/v1/testing/run-all",
                json={
                    "agent_id": str(agent.id),
                    "category": "greeting",
                },
            )

            assert response.status_code == 200
            data = response.json()
            # Should only count greeting scenarios
            assert data["test_count"] >= 1


# =============================================================================
# Test Run Listing Tests
# =============================================================================


class TestTestRunEndpoints:
    """Test test run listing and retrieval endpoints."""

    @pytest.mark.asyncio
    async def test_list_test_runs_empty(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test GET /testing/runs returns empty list when no runs."""
        client, _user = authenticated_test_client

        response = await client.get("/api/v1/testing/runs")

        assert response.status_code == 200
        data = response.json()
        assert "runs" in data
        assert "total" in data
        assert "page" in data
        assert "page_size" in data
        assert data["total"] >= 0

    @pytest.mark.asyncio
    async def test_list_test_runs_with_data(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: AsyncSession,
        create_test_scenario: Any,
        create_test_agent: Any,
        create_test_run_record: Any,
    ) -> None:
        """Test GET /testing/runs returns test runs."""
        client, user = authenticated_test_client

        # Create test data
        scenario = await create_test_scenario(is_built_in=True)
        agent = await create_test_agent(user_id=user.id)
        await create_test_run_record(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=user.id,
        )

        response = await client.get("/api/v1/testing/runs")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] >= 1
        assert len(data["runs"]) >= 1

        # Check run structure
        run = data["runs"][0]
        assert "id" in run
        assert "scenario_id" in run
        assert "agent_id" in run
        assert "status" in run
        assert "overall_score" in run
        assert "passed" in run
        assert "created_at" in run

    @pytest.mark.asyncio
    async def test_list_test_runs_multi_tenant_isolation(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: AsyncSession,
        create_test_scenario: Any,
        create_test_agent: Any,
        create_test_run_record: Any,
        create_test_user: Any,
    ) -> None:
        """Test that users only see their own test runs."""
        client, user = authenticated_test_client

        # Create another user
        other_user = await create_test_user(email="other@example.com")

        # Create test data
        scenario = await create_test_scenario(is_built_in=True)
        user_agent = await create_test_agent(user_id=user.id, name="User Agent")
        other_agent = await create_test_agent(user_id=other_user.id, name="Other Agent")

        # Create test runs
        await create_test_run_record(
            scenario_id=scenario.id,
            agent_id=user_agent.id,
            user_id=user.id,
        )
        await create_test_run_record(
            scenario_id=scenario.id,
            agent_id=other_agent.id,
            user_id=other_user.id,
        )

        response = await client.get("/api/v1/testing/runs")

        assert response.status_code == 200
        data = response.json()

        # User should only see their own runs
        for run in data["runs"]:
            # All runs should belong to user's agent
            assert run["agent_id"] == str(user_agent.id)

    @pytest.mark.asyncio
    async def test_list_test_runs_with_pagination(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: AsyncSession,
        create_test_scenario: Any,
        create_test_agent: Any,
        create_test_run_record: Any,
    ) -> None:
        """Test GET /testing/runs with pagination params."""
        client, user = authenticated_test_client

        # Create test data
        scenario = await create_test_scenario(is_built_in=True)
        agent = await create_test_agent(user_id=user.id)

        # Create 5 test runs
        for _i in range(5):
            await create_test_run_record(
                scenario_id=scenario.id,
                agent_id=agent.id,
                user_id=user.id,
            )

        response = await client.get(
            "/api/v1/testing/runs",
            params={"page": 1, "page_size": 2},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["page"] == 1
        assert data["page_size"] == 2
        assert len(data["runs"]) == 2
        assert data["total"] >= 5

    @pytest.mark.asyncio
    async def test_list_test_runs_filter_by_status(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: AsyncSession,
        create_test_scenario: Any,
        create_test_agent: Any,
        create_test_run_record: Any,
    ) -> None:
        """Test GET /testing/runs with status filter."""
        client, user = authenticated_test_client

        # Create test data
        scenario = await create_test_scenario(is_built_in=True)
        agent = await create_test_agent(user_id=user.id)

        # Create runs with different statuses
        await create_test_run_record(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=user.id,
            status=TestRunStatus.PASSED.value,
        )
        await create_test_run_record(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=user.id,
            status=TestRunStatus.FAILED.value,
        )

        response = await client.get(
            "/api/v1/testing/runs",
            params={"status": "passed"},
        )

        assert response.status_code == 200
        data = response.json()
        # All returned runs should have passed status
        for run in data["runs"]:
            assert run["status"] == "passed"

    @pytest.mark.asyncio
    async def test_list_test_runs_filter_by_passed(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: AsyncSession,
        create_test_scenario: Any,
        create_test_agent: Any,
        create_test_run_record: Any,
    ) -> None:
        """Test GET /testing/runs with passed filter."""
        client, user = authenticated_test_client

        # Create test data
        scenario = await create_test_scenario(is_built_in=True)
        agent = await create_test_agent(user_id=user.id)

        # Create runs with different pass/fail
        await create_test_run_record(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=user.id,
            passed=True,
        )
        await create_test_run_record(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=user.id,
            passed=False,
        )

        response = await client.get(
            "/api/v1/testing/runs",
            params={"passed": False},
        )

        assert response.status_code == 200
        data = response.json()
        # All returned runs should be failed
        for run in data["runs"]:
            assert run["passed"] is False

    @pytest.mark.asyncio
    async def test_get_test_run_success(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: AsyncSession,
        create_test_scenario: Any,
        create_test_agent: Any,
        create_test_run_record: Any,
    ) -> None:
        """Test GET /testing/runs/{id} returns detailed test run."""
        client, user = authenticated_test_client

        # Create test run
        scenario = await create_test_scenario(is_built_in=True, name="Detail Test")
        agent = await create_test_agent(user_id=user.id, name="Detail Agent")
        test_run = await create_test_run_record(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=user.id,
            overall_score=90,
            passed=True,
        )

        response = await client.get(f"/api/v1/testing/runs/{test_run.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(test_run.id)
        assert data["scenario_id"] == str(scenario.id)
        assert data["agent_id"] == str(agent.id)
        assert data["overall_score"] == 90
        assert data["passed"] is True
        # Detail endpoint includes these fields
        assert "actual_transcript" in data
        assert "behavior_matches" in data
        assert "criteria_results" in data

    @pytest.mark.asyncio
    async def test_get_test_run_other_user_not_accessible(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: AsyncSession,
        create_test_scenario: Any,
        create_test_agent: Any,
        create_test_run_record: Any,
        create_test_user: Any,
    ) -> None:
        """Test that users cannot access other users' test runs."""
        client, _user = authenticated_test_client

        # Create test run for another user
        other_user = await create_test_user(email="other@example.com")
        scenario = await create_test_scenario(is_built_in=True)
        agent = await create_test_agent(user_id=other_user.id)
        test_run = await create_test_run_record(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=other_user.id,
        )

        response = await client.get(f"/api/v1/testing/runs/{test_run.id}")

        # Should return 404 (not found) due to multi-tenant isolation
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_test_run_not_found(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test GET /testing/runs/{id} returns 404 for non-existent run."""
        client, _user = authenticated_test_client

        fake_id = str(uuid.uuid4())
        response = await client.get(f"/api/v1/testing/runs/{fake_id}")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_test_run_invalid_uuid(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test GET /testing/runs/{id} returns 400 for invalid UUID."""
        client, _user = authenticated_test_client

        response = await client.get("/api/v1/testing/runs/not-a-uuid")

        assert response.status_code == 400


# =============================================================================
# Testing Summary Tests
# =============================================================================


class TestTestingSummaryEndpoint:
    """Test testing summary endpoint."""

    @pytest.mark.asyncio
    async def test_get_summary_agent_not_found(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test GET /testing/summary/{agent_id} returns 404 for non-existent agent."""
        client, _user = authenticated_test_client

        fake_id = str(uuid.uuid4())
        response = await client.get(f"/api/v1/testing/summary/{fake_id}")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_summary_invalid_uuid(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test GET /testing/summary/{agent_id} returns 400 for invalid UUID."""
        client, _user = authenticated_test_client

        response = await client.get("/api/v1/testing/summary/not-a-uuid")

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_get_summary_no_runs(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: AsyncSession,
        create_test_agent: Any,
    ) -> None:
        """Test GET /testing/summary/{agent_id} with no test runs."""
        client, user = authenticated_test_client

        # Create agent
        agent = await create_test_agent(user_id=user.id)

        response = await client.get(f"/api/v1/testing/summary/{agent.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["agent_id"] == str(agent.id)
        assert data["total_runs"] == 0
        assert data["passed"] == 0
        assert data["failed"] == 0
        assert data["errors"] == 0
        assert data["pass_rate"] == 0.0
        assert data["avg_score"] is None
        assert data["last_run_at"] is None

    @pytest.mark.asyncio
    async def test_get_summary_with_runs(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: AsyncSession,
        create_test_scenario: Any,
        create_test_agent: Any,
        create_test_run_record: Any,
    ) -> None:
        """Test GET /testing/summary/{agent_id} with test runs."""
        client, user = authenticated_test_client

        # Create test data
        scenario = await create_test_scenario(is_built_in=True)
        agent = await create_test_agent(user_id=user.id)

        # Create test runs: 2 passed, 1 failed
        await create_test_run_record(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=user.id,
            status=TestRunStatus.PASSED.value,
            passed=True,
            overall_score=85,
        )
        await create_test_run_record(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=user.id,
            status=TestRunStatus.PASSED.value,
            passed=True,
            overall_score=90,
        )
        await create_test_run_record(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=user.id,
            status=TestRunStatus.FAILED.value,
            passed=False,
            overall_score=50,
        )

        response = await client.get(f"/api/v1/testing/summary/{agent.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["agent_id"] == str(agent.id)
        assert data["total_runs"] == 3
        assert data["passed"] == 2
        assert data["failed"] == 1
        assert data["pass_rate"] == pytest.approx(2 / 3, rel=0.01)
        assert data["avg_score"] == pytest.approx((85 + 90 + 50) / 3, rel=0.01)
        assert data["last_run_at"] is not None

    @pytest.mark.asyncio
    async def test_get_summary_with_errors(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_session: AsyncSession,
        create_test_scenario: Any,
        create_test_agent: Any,
        create_test_run_record: Any,
    ) -> None:
        """Test GET /testing/summary/{agent_id} counts error runs."""
        client, user = authenticated_test_client

        # Create test data
        scenario = await create_test_scenario(is_built_in=True)
        agent = await create_test_agent(user_id=user.id)

        # Create test runs with errors
        await create_test_run_record(
            scenario_id=scenario.id,
            agent_id=agent.id,
            user_id=user.id,
            status=TestRunStatus.ERROR.value,
            passed=None,
            overall_score=None,
            error_message="Test execution failed",
        )

        response = await client.get(f"/api/v1/testing/summary/{agent.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["errors"] >= 1
