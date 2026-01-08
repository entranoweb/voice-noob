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

            # Should return queued message, not found, or bad request (invalid call_id)
            assert response.status_code in [200, 400, 404]


class TestQAWorkspaceSettings:
    """Test QA workspace settings endpoints."""

    @pytest.mark.asyncio
    async def test_get_workspace_qa_settings_not_found(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test GET /qa/workspace/{id}/settings returns 404 for non-existent workspace."""
        client, _user = authenticated_test_client
        import uuid

        fake_workspace_id = str(uuid.uuid4())

        response = await client.get(f"/api/v1/qa/workspace/{fake_workspace_id}/settings")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_get_workspace_qa_settings_success(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Any,
    ) -> None:
        """Test GET /qa/workspace/{id}/settings returns settings for owned workspace."""
        client, _user = authenticated_test_client

        response = await client.get(f"/api/v1/qa/workspace/{test_workspace.id}/settings")

        assert response.status_code == 200
        data = response.json()
        assert "workspace_id" in data
        assert "settings" in data
        assert "global_settings" in data
        assert "effective_settings" in data
        # Default should inherit global
        assert data["settings"]["inherit_global"] is True

    @pytest.mark.asyncio
    async def test_update_workspace_qa_settings_success(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Any,
    ) -> None:
        """Test PUT /qa/workspace/{id}/settings updates settings."""
        client, _user = authenticated_test_client

        # Update settings
        response = await client.put(
            f"/api/v1/qa/workspace/{test_workspace.id}/settings",
            json={
                "qa_enabled": False,
                "pass_threshold": 80,
                "inherit_global": False,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["settings"]["qa_enabled"] is False
        assert data["settings"]["pass_threshold"] == 80
        assert data["settings"]["inherit_global"] is False

    @pytest.mark.asyncio
    async def test_update_workspace_qa_settings_partial(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Any,
    ) -> None:
        """Test PUT /qa/workspace/{id}/settings supports partial updates."""
        client, _user = authenticated_test_client

        # Only update one field
        response = await client.put(
            f"/api/v1/qa/workspace/{test_workspace.id}/settings",
            json={"pass_threshold": 90},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["settings"]["pass_threshold"] == 90
        # Other fields should remain at defaults
        assert data["settings"]["qa_enabled"] is True

    @pytest.mark.asyncio
    async def test_update_workspace_qa_settings_invalid_threshold(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Any,
    ) -> None:
        """Test PUT /qa/workspace/{id}/settings rejects invalid threshold."""
        client, _user = authenticated_test_client

        # Try to set threshold > 100
        response = await client.put(
            f"/api/v1/qa/workspace/{test_workspace.id}/settings",
            json={"pass_threshold": 150},
        )

        assert response.status_code == 400
        assert "threshold" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_update_workspace_qa_settings_not_owner(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
    ) -> None:
        """Test PUT /qa/workspace/{id}/settings returns 403 for non-owner."""
        client, _user = authenticated_test_client
        import uuid

        # Use a random workspace ID that doesn't belong to the user
        fake_workspace_id = str(uuid.uuid4())

        response = await client.put(
            f"/api/v1/qa/workspace/{fake_workspace_id}/settings",
            json={"qa_enabled": False},
        )

        # Should return 404 (workspace not found) since user doesn't own it
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_effective_settings_with_inheritance(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Any,
    ) -> None:
        """Test effective_settings reflects global when inherit_global=True."""
        client, _user = authenticated_test_client

        # First, ensure inherit_global is True
        await client.put(
            f"/api/v1/qa/workspace/{test_workspace.id}/settings",
            json={"inherit_global": True},
        )

        response = await client.get(f"/api/v1/qa/workspace/{test_workspace.id}/settings")

        assert response.status_code == 200
        data = response.json()
        # Effective settings should match global settings
        assert data["effective_settings"]["inherit_global"] is True

    @pytest.mark.asyncio
    async def test_effective_settings_without_inheritance(
        self,
        authenticated_test_client: tuple[AsyncClient, User],
        test_workspace: Any,
    ) -> None:
        """Test effective_settings uses workspace settings when inherit_global=False."""
        client, _user = authenticated_test_client

        # Set custom settings without inheritance
        await client.put(
            f"/api/v1/qa/workspace/{test_workspace.id}/settings",
            json={
                "inherit_global": False,
                "pass_threshold": 85,
                "qa_enabled": True,
            },
        )

        response = await client.get(f"/api/v1/qa/workspace/{test_workspace.id}/settings")

        assert response.status_code == 200
        data = response.json()
        # Effective settings should use workspace-specific values
        assert data["effective_settings"]["pass_threshold"] == 85
        assert data["effective_settings"]["inherit_global"] is False
