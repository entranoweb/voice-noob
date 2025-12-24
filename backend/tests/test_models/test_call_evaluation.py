"""Tests for CallEvaluation model (Task 8.5.2)."""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call_evaluation import CallEvaluation


class TestCallEvaluationModel:
    """Test CallEvaluation model creation and relationships."""

    @pytest.mark.asyncio
    async def test_create_evaluation(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_workspace: Any,
        create_test_agent: Any,
        create_test_call_record: Any,
    ) -> None:
        """Test creating a basic evaluation."""
        user = await create_test_user()
        workspace = await create_test_workspace(user.id)
        agent = await create_test_agent(user.id)
        call_record = await create_test_call_record(agent_id=agent.id, workspace_id=workspace.id)

        evaluation = CallEvaluation(
            call_id=call_record.id,
            agent_id=agent.id,
            workspace_id=workspace.id,
            overall_score=85,
            intent_completion=90,
            tool_usage=80,
            compliance=95,
            response_quality=75,
            passed=True,
            evaluation_model="claude-sonnet-4-20250514",
        )
        test_session.add(evaluation)
        await test_session.commit()
        await test_session.refresh(evaluation)

        assert evaluation.id is not None
        assert evaluation.overall_score == 85
        assert evaluation.passed is True

    @pytest.mark.asyncio
    async def test_evaluation_with_quality_metrics(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_workspace: Any,
        create_test_agent: Any,
        create_test_call_record: Any,
    ) -> None:
        """Test evaluation with all quality metrics."""
        user = await create_test_user()
        workspace = await create_test_workspace(user.id)
        agent = await create_test_agent(user.id)
        call_record = await create_test_call_record(agent_id=agent.id, workspace_id=workspace.id)

        evaluation = CallEvaluation(
            call_id=call_record.id,
            agent_id=agent.id,
            workspace_id=workspace.id,
            overall_score=78,
            passed=True,
            coherence=80,
            relevance=75,
            groundedness=82,
            fluency=76,
            overall_sentiment="positive",
            sentiment_score=0.6,
            escalation_risk=0.2,
            evaluation_model="claude-sonnet-4-20250514",
        )
        test_session.add(evaluation)
        await test_session.commit()

        assert evaluation.coherence == 80
        assert evaluation.overall_sentiment == "positive"
        assert evaluation.sentiment_score == 0.6

    @pytest.mark.asyncio
    async def test_passed_threshold_logic(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_workspace: Any,
        create_test_call_record: Any,
    ) -> None:
        """Test that score >= 70 means passed."""
        user = await create_test_user()
        workspace = await create_test_workspace(user.id)
        call_record = await create_test_call_record(workspace_id=workspace.id)

        # Score 70 should pass
        eval_pass = CallEvaluation(
            call_id=call_record.id,
            workspace_id=workspace.id,
            overall_score=70,
            passed=True,  # 70 >= 70
            evaluation_model="claude-sonnet-4-20250514",
        )
        test_session.add(eval_pass)
        await test_session.commit()

        assert eval_pass.passed is True

    @pytest.mark.asyncio
    async def test_failed_threshold_logic(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_workspace: Any,
        create_test_call_record: Any,
    ) -> None:
        """Test that score < 70 means failed."""
        user = await create_test_user()
        workspace = await create_test_workspace(user.id)
        call_record = await create_test_call_record(workspace_id=workspace.id)

        # Score 69 should fail
        eval_fail = CallEvaluation(
            call_id=call_record.id,
            workspace_id=workspace.id,
            overall_score=69,
            passed=False,  # 69 < 70
            evaluation_model="claude-sonnet-4-20250514",
        )
        test_session.add(eval_fail)
        await test_session.commit()

        assert eval_fail.passed is False

    @pytest.mark.asyncio
    async def test_evaluation_with_failure_reasons(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_workspace: Any,
        create_test_call_record: Any,
    ) -> None:
        """Test evaluation with failure reasons."""
        user = await create_test_user()
        workspace = await create_test_workspace(user.id)
        call_record = await create_test_call_record(workspace_id=workspace.id)

        evaluation = CallEvaluation(
            call_id=call_record.id,
            workspace_id=workspace.id,
            overall_score=45,
            passed=False,
            failure_reasons=["Intent not completed", "Compliance violation"],
            recommendations=["Improve greeting", "Add escalation path"],
            evaluation_model="claude-sonnet-4-20250514",
        )
        test_session.add(evaluation)
        await test_session.commit()

        assert evaluation.failure_reasons is not None
        assert len(evaluation.failure_reasons) == 2
        assert "Intent not completed" in evaluation.failure_reasons

    @pytest.mark.asyncio
    async def test_evaluation_objectives(
        self,
        test_session: AsyncSession,
        create_test_user: Any,
        create_test_workspace: Any,
        create_test_call_record: Any,
    ) -> None:
        """Test evaluation with objectives detected/completed."""
        user = await create_test_user()
        workspace = await create_test_workspace(user.id)
        call_record = await create_test_call_record(workspace_id=workspace.id)

        evaluation = CallEvaluation(
            call_id=call_record.id,
            workspace_id=workspace.id,
            overall_score=80,
            passed=True,
            objectives_detected=["book appointment", "get information"],
            objectives_completed=["book appointment"],
            evaluation_model="claude-sonnet-4-20250514",
        )
        test_session.add(evaluation)
        await test_session.commit()

        assert evaluation.objectives_detected is not None
        assert len(evaluation.objectives_detected) == 2
        assert len(evaluation.objectives_completed) == 1  # type: ignore[arg-type]
