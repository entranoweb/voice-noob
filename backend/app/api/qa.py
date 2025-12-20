"""QA Testing Framework API routes.

Provides endpoints for managing call evaluations and QA metrics.
"""

import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, user_id_to_uuid
from app.core.config import settings
from app.db.session import get_db
from app.models.call_evaluation import CallEvaluation
from app.models.call_record import CallRecord
from app.models.workspace import Workspace
from app.services.qa.evaluator import QAEvaluator


async def _verify_workspace_ownership(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: int,
) -> Workspace:
    """Verify that a workspace belongs to the current user.

    Args:
        db: Database session
        workspace_id: Workspace ID to verify
        user_id: User ID to check ownership against

    Returns:
        The workspace if ownership verified

    Raises:
        HTTPException: If workspace not found or not owned by user
    """
    result = await db.execute(
        select(Workspace).where(Workspace.id == workspace_id)
    )
    workspace = result.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    if workspace.user_id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to access this workspace")

    return workspace

router = APIRouter(prefix="/api/v1/qa", tags=["qa"])
logger = structlog.get_logger()


def _parse_uuid(value: str, field_name: str = "ID") -> uuid.UUID:
    """Parse UUID string with proper error handling.

    Args:
        value: String value to parse as UUID
        field_name: Field name for error message

    Returns:
        Parsed UUID

    Raises:
        HTTPException: If the value is not a valid UUID
    """
    try:
        return uuid.UUID(value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name} format") from e


# =============================================================================
# Pydantic Schemas
# =============================================================================


class CallEvaluationResponse(BaseModel):
    """Call evaluation response."""

    id: str
    call_id: str
    agent_id: str | None
    workspace_id: str | None
    overall_score: int
    intent_completion: int | None
    tool_usage: int | None
    compliance: int | None
    response_quality: int | None
    passed: bool
    coherence: int | None
    relevance: int | None
    groundedness: int | None
    fluency: int | None
    overall_sentiment: str | None
    sentiment_score: float | None
    escalation_risk: float | None
    objectives_detected: list[str] | None
    objectives_completed: list[str] | None
    failure_reasons: list[str] | None
    recommendations: list[str] | None
    evaluation_model: str
    evaluation_latency_ms: int | None
    evaluation_cost_cents: float | None
    created_at: datetime

    model_config = {"from_attributes": True}


class CallEvaluationListResponse(BaseModel):
    """Paginated call evaluations response."""

    evaluations: list[CallEvaluationResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class QAMetricsResponse(BaseModel):
    """Aggregated QA metrics response."""

    total_evaluations: int
    total_passed: int
    total_failed: int
    pass_rate: float
    avg_overall_score: float
    avg_intent_completion: float | None
    avg_tool_usage: float | None
    avg_compliance: float | None
    avg_response_quality: float | None
    avg_coherence: float | None
    avg_relevance: float | None
    avg_groundedness: float | None
    avg_fluency: float | None
    total_cost_cents: float


class EvaluateCallRequest(BaseModel):
    """Request to manually trigger call evaluation."""

    call_id: str


class EvaluateCallResponse(BaseModel):
    """Response after triggering evaluation."""

    message: str
    evaluation_id: str | None = None
    queued: bool = False


class QAStatusResponse(BaseModel):
    """QA system status response."""

    enabled: bool
    auto_evaluate: bool
    evaluation_model: str
    default_threshold: int
    api_key_configured: bool


# =============================================================================
# QA Status Endpoint
# =============================================================================


@router.get("/status", response_model=QAStatusResponse)
async def get_qa_status(
    current_user: CurrentUser,
) -> QAStatusResponse:
    """Get QA system status and configuration.

    Returns current QA settings and whether the system is properly configured.
    """
    return QAStatusResponse(
        enabled=settings.QA_ENABLED,
        auto_evaluate=settings.QA_AUTO_EVALUATE,
        evaluation_model=settings.QA_EVALUATION_MODEL,
        default_threshold=settings.QA_DEFAULT_THRESHOLD,
        api_key_configured=bool(settings.ANTHROPIC_API_KEY),
    )


# =============================================================================
# Evaluation Endpoints
# =============================================================================


@router.get("/evaluations", response_model=CallEvaluationListResponse)
async def list_evaluations(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    agent_id: str | None = Query(default=None, description="Filter by agent ID"),
    workspace_id: str | None = Query(default=None, description="Filter by workspace ID"),
    passed: bool | None = Query(default=None, description="Filter by pass/fail status"),
) -> CallEvaluationListResponse:
    """List call evaluations with pagination and filters.

    Args:
        current_user: Authenticated user
        db: Database session
        page: Page number (1-indexed)
        page_size: Number of items per page
        agent_id: Optional agent ID filter
        workspace_id: Optional workspace ID filter
        passed: Optional pass/fail filter
    """
    log = logger.bind(user_id=current_user.id)
    log.info("listing_evaluations", page=page, page_size=page_size)

    user_uuid = user_id_to_uuid(current_user.id)

    # Build query - join with CallRecord to filter by user
    query = (
        select(CallEvaluation)
        .join(CallRecord, CallEvaluation.call_id == CallRecord.id)
        .where(CallRecord.user_id == user_uuid)
    )

    # Apply filters
    if agent_id:
        query = query.where(CallEvaluation.agent_id == _parse_uuid(agent_id, "agent_id"))
    if workspace_id:
        query = query.where(
            CallEvaluation.workspace_id == _parse_uuid(workspace_id, "workspace_id")
        )
    if passed is not None:
        query = query.where(CallEvaluation.passed == passed)

    # Get total count
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Apply pagination and ordering
    query = query.order_by(desc(CallEvaluation.created_at))
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    evaluations = result.scalars().all()

    total_pages = (total + page_size - 1) // page_size

    return CallEvaluationListResponse(
        evaluations=[
            CallEvaluationResponse(
                id=str(e.id),
                call_id=str(e.call_id),
                agent_id=str(e.agent_id) if e.agent_id else None,
                workspace_id=str(e.workspace_id) if e.workspace_id else None,
                overall_score=e.overall_score,
                intent_completion=e.intent_completion,
                tool_usage=e.tool_usage,
                compliance=e.compliance,
                response_quality=e.response_quality,
                passed=e.passed,
                coherence=e.coherence,
                relevance=e.relevance,
                groundedness=e.groundedness,
                fluency=e.fluency,
                overall_sentiment=e.overall_sentiment,
                sentiment_score=e.sentiment_score,
                escalation_risk=e.escalation_risk,
                objectives_detected=e.objectives_detected,
                objectives_completed=e.objectives_completed,
                failure_reasons=e.failure_reasons,
                recommendations=e.recommendations,
                evaluation_model=e.evaluation_model,
                evaluation_latency_ms=e.evaluation_latency_ms,
                evaluation_cost_cents=e.evaluation_cost_cents,
                created_at=e.created_at,
            )
            for e in evaluations
        ],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.get("/evaluations/{evaluation_id}", response_model=CallEvaluationResponse)
async def get_evaluation(
    evaluation_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CallEvaluationResponse:
    """Get a specific call evaluation.

    Args:
        evaluation_id: Evaluation ID
        current_user: Authenticated user
        db: Database session
    """
    log = logger.bind(user_id=current_user.id, evaluation_id=evaluation_id)
    log.info("getting_evaluation")

    evaluation_uuid = _parse_uuid(evaluation_id, "evaluation_id")
    user_uuid = user_id_to_uuid(current_user.id)

    result = await db.execute(
        select(CallEvaluation)
        .join(CallRecord, CallEvaluation.call_id == CallRecord.id)
        .where(
            CallEvaluation.id == evaluation_uuid,
            CallRecord.user_id == user_uuid,
        )
    )
    evaluation = result.scalar_one_or_none()

    if not evaluation:
        raise HTTPException(status_code=404, detail="Evaluation not found")

    return CallEvaluationResponse(
        id=str(evaluation.id),
        call_id=str(evaluation.call_id),
        agent_id=str(evaluation.agent_id) if evaluation.agent_id else None,
        workspace_id=str(evaluation.workspace_id) if evaluation.workspace_id else None,
        overall_score=evaluation.overall_score,
        intent_completion=evaluation.intent_completion,
        tool_usage=evaluation.tool_usage,
        compliance=evaluation.compliance,
        response_quality=evaluation.response_quality,
        passed=evaluation.passed,
        coherence=evaluation.coherence,
        relevance=evaluation.relevance,
        groundedness=evaluation.groundedness,
        fluency=evaluation.fluency,
        overall_sentiment=evaluation.overall_sentiment,
        sentiment_score=evaluation.sentiment_score,
        escalation_risk=evaluation.escalation_risk,
        objectives_detected=evaluation.objectives_detected,
        objectives_completed=evaluation.objectives_completed,
        failure_reasons=evaluation.failure_reasons,
        recommendations=evaluation.recommendations,
        evaluation_model=evaluation.evaluation_model,
        evaluation_latency_ms=evaluation.evaluation_latency_ms,
        evaluation_cost_cents=evaluation.evaluation_cost_cents,
        created_at=evaluation.created_at,
    )


@router.get("/calls/{call_id}/evaluation", response_model=CallEvaluationResponse)
async def get_call_evaluation(
    call_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CallEvaluationResponse:
    """Get evaluation for a specific call.

    Args:
        call_id: Call record ID
        current_user: Authenticated user
        db: Database session
    """
    log = logger.bind(user_id=current_user.id, call_id=call_id)
    log.info("getting_call_evaluation")

    call_uuid = _parse_uuid(call_id, "call_id")
    user_uuid = user_id_to_uuid(current_user.id)

    # Verify user owns the call
    call_result = await db.execute(
        select(CallRecord).where(
            CallRecord.id == call_uuid,
            CallRecord.user_id == user_uuid,
        )
    )
    call_record = call_result.scalar_one_or_none()

    if not call_record:
        raise HTTPException(status_code=404, detail="Call not found")

    result = await db.execute(select(CallEvaluation).where(CallEvaluation.call_id == call_uuid))
    evaluation = result.scalar_one_or_none()

    if not evaluation:
        raise HTTPException(status_code=404, detail="Evaluation not found for this call")

    return CallEvaluationResponse(
        id=str(evaluation.id),
        call_id=str(evaluation.call_id),
        agent_id=str(evaluation.agent_id) if evaluation.agent_id else None,
        workspace_id=str(evaluation.workspace_id) if evaluation.workspace_id else None,
        overall_score=evaluation.overall_score,
        intent_completion=evaluation.intent_completion,
        tool_usage=evaluation.tool_usage,
        compliance=evaluation.compliance,
        response_quality=evaluation.response_quality,
        passed=evaluation.passed,
        coherence=evaluation.coherence,
        relevance=evaluation.relevance,
        groundedness=evaluation.groundedness,
        fluency=evaluation.fluency,
        overall_sentiment=evaluation.overall_sentiment,
        sentiment_score=evaluation.sentiment_score,
        escalation_risk=evaluation.escalation_risk,
        objectives_detected=evaluation.objectives_detected,
        objectives_completed=evaluation.objectives_completed,
        failure_reasons=evaluation.failure_reasons,
        recommendations=evaluation.recommendations,
        evaluation_model=evaluation.evaluation_model,
        evaluation_latency_ms=evaluation.evaluation_latency_ms,
        evaluation_cost_cents=evaluation.evaluation_cost_cents,
        created_at=evaluation.created_at,
    )


@router.post("/evaluate", response_model=EvaluateCallResponse)
async def evaluate_call(
    request: EvaluateCallRequest,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> EvaluateCallResponse:
    """Manually trigger evaluation for a specific call.

    Args:
        request: Evaluation request with call_id
        background_tasks: FastAPI background tasks
        current_user: Authenticated user
        db: Database session
    """
    log = logger.bind(user_id=current_user.id, call_id=request.call_id)
    log.info("manual_evaluation_requested")

    if not settings.QA_ENABLED:
        raise HTTPException(status_code=400, detail="QA evaluation is disabled")

    if not settings.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=400, detail="Anthropic API key not configured")

    call_uuid = _parse_uuid(request.call_id, "call_id")
    user_uuid = user_id_to_uuid(current_user.id)

    # Verify user owns the call
    call_result = await db.execute(
        select(CallRecord).where(
            CallRecord.id == call_uuid,
            CallRecord.user_id == user_uuid,
        )
    )
    call_record = call_result.scalar_one_or_none()

    if not call_record:
        raise HTTPException(status_code=404, detail="Call not found")

    # Check if already evaluated
    existing = await db.execute(select(CallEvaluation).where(CallEvaluation.call_id == call_uuid))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Call has already been evaluated")

    # Check if call has transcript
    if not call_record.transcript:
        raise HTTPException(status_code=400, detail="Call has no transcript to evaluate")

    # Run evaluation synchronously for manual requests
    evaluator = QAEvaluator(db)
    evaluation = await evaluator.evaluate_call(call_uuid)

    if evaluation:
        return EvaluateCallResponse(
            message="Evaluation completed successfully",
            evaluation_id=str(evaluation.id),
            queued=False,
        )
    raise HTTPException(status_code=500, detail="Evaluation failed")


# =============================================================================
# Metrics Endpoints
# =============================================================================


@router.get("/metrics", response_model=QAMetricsResponse)
async def get_qa_metrics(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    agent_id: str | None = Query(default=None, description="Filter by agent ID"),
    workspace_id: str | None = Query(default=None, description="Filter by workspace ID"),
) -> QAMetricsResponse:
    """Get aggregated QA metrics.

    Args:
        current_user: Authenticated user
        db: Database session
        agent_id: Optional agent ID filter
        workspace_id: Optional workspace ID filter
    """
    log = logger.bind(user_id=current_user.id)
    log.info("getting_qa_metrics")

    user_uuid = user_id_to_uuid(current_user.id)

    # Build base query joining with CallRecord for user filtering
    base_query = (
        select(CallEvaluation)
        .join(CallRecord, CallEvaluation.call_id == CallRecord.id)
        .where(CallRecord.user_id == user_uuid)
    )

    if agent_id:
        base_query = base_query.where(CallEvaluation.agent_id == _parse_uuid(agent_id, "agent_id"))
    if workspace_id:
        base_query = base_query.where(
            CallEvaluation.workspace_id == _parse_uuid(workspace_id, "workspace_id")
        )

    # Get all evaluations for aggregation
    result = await db.execute(base_query)
    evaluations = result.scalars().all()

    if not evaluations:
        return QAMetricsResponse(
            total_evaluations=0,
            total_passed=0,
            total_failed=0,
            pass_rate=0.0,
            avg_overall_score=0.0,
            avg_intent_completion=None,
            avg_tool_usage=None,
            avg_compliance=None,
            avg_response_quality=None,
            avg_coherence=None,
            avg_relevance=None,
            avg_groundedness=None,
            avg_fluency=None,
            total_cost_cents=0.0,
        )

    total = len(evaluations)
    passed = sum(1 for e in evaluations if e.passed)
    failed = total - passed

    def safe_avg(values: list[int | None]) -> float | None:
        valid = [v for v in values if v is not None]
        return sum(valid) / len(valid) if valid else None

    return QAMetricsResponse(
        total_evaluations=total,
        total_passed=passed,
        total_failed=failed,
        pass_rate=passed / total if total > 0 else 0.0,
        avg_overall_score=sum(e.overall_score for e in evaluations) / total,
        avg_intent_completion=safe_avg([e.intent_completion for e in evaluations]),
        avg_tool_usage=safe_avg([e.tool_usage for e in evaluations]),
        avg_compliance=safe_avg([e.compliance for e in evaluations]),
        avg_response_quality=safe_avg([e.response_quality for e in evaluations]),
        avg_coherence=safe_avg([e.coherence for e in evaluations]),
        avg_relevance=safe_avg([e.relevance for e in evaluations]),
        avg_groundedness=safe_avg([e.groundedness for e in evaluations]),
        avg_fluency=safe_avg([e.fluency for e in evaluations]),
        total_cost_cents=sum(e.evaluation_cost_cents or 0 for e in evaluations),
    )


# =============================================================================
# Dashboard Schemas
# =============================================================================


class DashboardMetricsResponse(BaseModel):
    """Dashboard metrics response."""

    total_evaluations: int
    passed_count: int
    failed_count: int
    pass_rate: float
    average_score: float
    score_breakdown: dict[str, float]
    quality_metrics: dict[str, float]
    sentiment_distribution: dict[str, int]
    latency: dict[str, float]
    period_days: int


class TrendDataResponse(BaseModel):
    """Trend data for charts."""

    dates: list[str]
    values: list[float]
    metric: str


class FailureReasonResponse(BaseModel):
    """Top failure reason with count."""

    reason: str
    count: int


class AgentComparisonResponse(BaseModel):
    """Agent comparison stats."""

    agent_id: str
    total_evaluations: int
    average_score: float
    pass_rate: float


# =============================================================================
# Dashboard Endpoints
# =============================================================================


@router.get("/dashboard/metrics", response_model=DashboardMetricsResponse)
async def get_dashboard_metrics_endpoint(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    agent_id: str | None = Query(default=None, description="Filter by agent ID"),
    workspace_id: str | None = Query(default=None, description="Filter by workspace ID"),
    days: int = Query(default=7, ge=1, le=90, description="Number of days to include"),
) -> DashboardMetricsResponse:
    """Get dashboard metrics with aggregated data.

    Args:
        current_user: Authenticated user
        db: Database session
        agent_id: Optional agent ID filter
        workspace_id: Optional workspace ID filter (must belong to current user)
        days: Number of days to include (1-90)
    """
    from app.services.qa.dashboard import get_dashboard_metrics

    log = logger.bind(user_id=current_user.id)
    log.info("getting_dashboard_metrics", days=days)

    agent_uuid = _parse_uuid(agent_id, "agent_id") if agent_id else None
    workspace_uuid = None
    if workspace_id:
        workspace_uuid = _parse_uuid(workspace_id, "workspace_id")
        # Verify user owns this workspace
        await _verify_workspace_ownership(db, workspace_uuid, current_user.id)

    metrics = await get_dashboard_metrics(
        db=db,
        workspace_id=workspace_uuid,
        agent_id=agent_uuid,
        days=days,
    )

    return DashboardMetricsResponse(**metrics)


@router.get("/dashboard/trends", response_model=TrendDataResponse)
async def get_dashboard_trends(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    agent_id: str | None = Query(default=None, description="Filter by agent ID"),
    workspace_id: str | None = Query(default=None, description="Filter by workspace ID"),
    metric: str = Query(default="overall_score", description="Metric to trend"),
    days: int = Query(default=30, ge=1, le=90, description="Number of days to include"),
) -> TrendDataResponse:
    """Get trend data for charts.

    Args:
        current_user: Authenticated user
        db: Database session
        agent_id: Optional agent ID filter
        workspace_id: Optional workspace ID filter (must belong to current user)
        metric: Metric to trend (overall_score, pass_rate, intent_completion, etc.)
        days: Number of days to include (1-90)
    """
    from app.services.qa.dashboard import get_trends

    log = logger.bind(user_id=current_user.id)
    log.info("getting_dashboard_trends", metric=metric, days=days)

    agent_uuid = _parse_uuid(agent_id, "agent_id") if agent_id else None
    workspace_uuid = None
    if workspace_id:
        workspace_uuid = _parse_uuid(workspace_id, "workspace_id")
        # Verify user owns this workspace
        await _verify_workspace_ownership(db, workspace_uuid, current_user.id)

    trends = await get_trends(
        db=db,
        workspace_id=workspace_uuid,
        agent_id=agent_uuid,
        metric=metric,
        days=days,
    )

    return TrendDataResponse(**trends)


@router.get("/dashboard/failure-reasons", response_model=list[FailureReasonResponse])
async def get_dashboard_failure_reasons(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    agent_id: str | None = Query(default=None, description="Filter by agent ID"),
    workspace_id: str | None = Query(default=None, description="Filter by workspace ID"),
    days: int = Query(default=7, ge=1, le=90, description="Number of days to include"),
    limit: int = Query(default=10, ge=1, le=50, description="Max results"),
) -> list[FailureReasonResponse]:
    """Get top failure reasons.

    Args:
        current_user: Authenticated user
        db: Database session
        agent_id: Optional agent ID filter
        workspace_id: Optional workspace ID filter (must belong to current user)
        days: Number of days to include (1-90)
        limit: Maximum number of results (1-50)
    """
    from app.services.qa.dashboard import get_top_failure_reasons

    log = logger.bind(user_id=current_user.id)
    log.info("getting_failure_reasons", days=days, limit=limit)

    agent_uuid = _parse_uuid(agent_id, "agent_id") if agent_id else None
    workspace_uuid = None
    if workspace_id:
        workspace_uuid = _parse_uuid(workspace_id, "workspace_id")
        # Verify user owns this workspace
        await _verify_workspace_ownership(db, workspace_uuid, current_user.id)

    reasons = await get_top_failure_reasons(
        db=db,
        workspace_id=workspace_uuid,
        agent_id=agent_uuid,
        days=days,
        limit=limit,
    )

    return [FailureReasonResponse(**r) for r in reasons]


@router.get("/dashboard/agent-comparison", response_model=list[AgentComparisonResponse])
async def get_dashboard_agent_comparison(
    workspace_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    days: int = Query(default=7, ge=1, le=90, description="Number of days to include"),
) -> list[AgentComparisonResponse]:
    """Compare agents within a workspace.

    Args:
        workspace_id: Workspace ID to compare agents in (must belong to current user)
        current_user: Authenticated user
        db: Database session
        days: Number of days to include (1-90)
    """
    from app.services.qa.dashboard import get_agent_comparison

    log = logger.bind(user_id=current_user.id, workspace_id=workspace_id)
    log.info("getting_agent_comparison", days=days)

    workspace_uuid = _parse_uuid(workspace_id, "workspace_id")
    # Verify user owns this workspace
    await _verify_workspace_ownership(db, workspace_uuid, current_user.id)

    agents = await get_agent_comparison(
        db=db,
        workspace_id=workspace_uuid,
        days=days,
    )

    return [AgentComparisonResponse(**a) for a in agents]


# =============================================================================
# Alert Schemas
# =============================================================================


class AlertResponse(BaseModel):
    """QA Alert response."""

    id: str
    type: str
    severity: str
    agent_id: str | None
    message: str
    metadata: dict[str, Any] | None
    acknowledged: bool
    acknowledged_at: str | None
    acknowledged_by: int | None
    created_at: str


class AcknowledgeAlertRequest(BaseModel):
    """Request to acknowledge an alert."""



# =============================================================================
# Alert Endpoints
# =============================================================================


@router.get("/alerts", response_model=list[AlertResponse])
async def get_qa_alerts(
    workspace_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    acknowledged: bool | None = Query(default=None, description="Filter by acknowledged status"),
    limit: int = Query(default=50, ge=1, le=100, description="Max alerts to return"),
) -> list[AlertResponse]:
    """Get QA alerts for a workspace.

    Args:
        workspace_id: Workspace ID (must belong to current user)
        current_user: Authenticated user
        db: Database session
        acknowledged: Filter by acknowledged status (None = all)
        limit: Maximum number of alerts to return
    """
    from app.services.qa.alerts import get_alerts

    log = logger.bind(user_id=current_user.id, workspace_id=workspace_id)
    log.info("getting_qa_alerts")

    workspace_uuid = _parse_uuid(workspace_id, "workspace_id")
    # Verify user owns this workspace
    await _verify_workspace_ownership(db, workspace_uuid, current_user.id)

    alerts = await get_alerts(
        db=db,
        workspace_id=workspace_uuid,
        acknowledged=acknowledged,
        limit=limit,
    )

    return [AlertResponse(**a) for a in alerts]


@router.post("/alerts/{alert_id}/acknowledge", response_model=AlertResponse)
async def acknowledge_qa_alert(
    alert_id: str,
    workspace_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AlertResponse:
    """Acknowledge a QA alert.

    Args:
        alert_id: Alert ID to acknowledge
        workspace_id: Workspace ID (must belong to current user)
        current_user: Authenticated user
        db: Database session
    """
    from app.services.qa.alerts import acknowledge_alert

    log = logger.bind(user_id=current_user.id, alert_id=alert_id)
    log.info("acknowledging_alert")

    workspace_uuid = _parse_uuid(workspace_id, "workspace_id")
    # Verify user owns this workspace
    await _verify_workspace_ownership(db, workspace_uuid, current_user.id)

    alert = await acknowledge_alert(
        db=db,
        workspace_id=workspace_uuid,
        alert_id=alert_id,
        user_id=current_user.id,
    )

    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    return AlertResponse(**alert)
