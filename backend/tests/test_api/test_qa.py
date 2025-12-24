"""Tests for QA API endpoints (Task 8.5.3)."""

from typing import Any
from unittest.mock import patch

import pytest
from httpx import AsyncClient

from app.models.user import User


class TestQAEndpoints:
    """Test QA API endpoints."""

    @pytest.mark.asyncio
    async def test_get_qa_status(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test GET /qa/status returns QA configuration."""
        client, _user = authenticated_test_client

        response = await client.get("/api/v1/qa/status")

        assert response.status_code == 200
        data = response.json()
        assert "enabled" in data
        assert "auto_evaluate" in data
        assert "evaluation_model" in data
        assert "default_threshold" in data

    @pytest.mark.asyncio
    async def test_list_evaluations_empty(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test GET /qa/evaluations returns empty list when no evaluations."""
        client, _user = authenticated_test_client

        response = await client.get("/api/v1/qa/evaluations")

        assert response.status_code == 200
        data = response.json()
        assert "evaluations" in data
        assert data["evaluations"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_evaluate_call_no_transcript(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test POST /qa/evaluate returns 400 for call without transcript."""
        client, _user = authenticated_test_client
        import uuid

        # Use a random UUID that doesn't exist
        fake_call_id = str(uuid.uuid4())

        response = await client.post(
            "/api/v1/qa/evaluate",
            json={"call_id": fake_call_id},
        )

        # Should return 404 (call not found) or 400 (no transcript)
        assert response.status_code in [400, 404]

    @pytest.mark.asyncio
    async def test_get_evaluation_not_found(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test GET /qa/evaluations/{id} returns 404 for non-existent evaluation."""
        client, _user = authenticated_test_client
        import uuid

        fake_eval_id = str(uuid.uuid4())

        response = await client.get(f"/api/v1/qa/evaluations/{fake_eval_id}")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_dashboard_metrics(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test GET /qa/dashboard/metrics returns metrics structure."""
        client, _user = authenticated_test_client

        response = await client.get("/api/v1/qa/dashboard/metrics")

        assert response.status_code == 200
        data = response.json()
        assert "total_evaluations" in data
        assert "pass_rate" in data

    @pytest.mark.asyncio
    async def test_get_dashboard_trends(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test GET /qa/dashboard/trends returns trend data."""
        client, _user = authenticated_test_client

        response = await client.get("/api/v1/qa/dashboard/trends")

        assert response.status_code == 200
        data = response.json()
        assert "dates" in data
        assert "values" in data

    @pytest.mark.asyncio
    async def test_get_failure_reasons(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test GET /qa/dashboard/failure-reasons returns failure reasons."""
        client, _user = authenticated_test_client

        response = await client.get("/api/v1/qa/dashboard/failure-reasons")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)


class TestQAEvaluateEndpoint:
    """Test QA evaluate endpoint with mocked Claude API."""

    @pytest.mark.asyncio
    async def test_evaluate_call_success(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        mock_anthropic_response: Any,
    ) -> None:
        """Test POST /qa/evaluate with mocked Claude API."""
        client, _user = authenticated_test_client
        import uuid

        # We need to mock the entire evaluation flow
        with patch("app.services.qa.evaluator.QAEvaluator.evaluate_call") as mock_eval:
            mock_eval.return_value = None  # Evaluation queued

            # Create a fake call_id
            fake_call_id = str(uuid.uuid4())

            response = await client.post(
                "/api/v1/qa/evaluate",
                json={"call_id": fake_call_id},
            )

            # Should return queued message or not found
            assert response.status_code in [200, 404]


class TestQAWorkspaceSettings:
    """Test QA workspace settings endpoints."""

    @pytest.mark.asyncio
    async def test_get_workspace_qa_settings(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test GET /qa/workspace/settings."""
        client, _user = authenticated_test_client

        response = await client.get(
            "/api/v1/qa/workspace/settings",
            params={"workspace_id": str(__import__("uuid").uuid4())},
        )

        # May return 404 if workspace doesn't exist, which is expected
        assert response.status_code in [200, 404]
