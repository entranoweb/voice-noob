"""QA Dashboard Metrics Service.

Provides aggregated metrics, trends, and analytics for QA evaluations.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call_evaluation import CallEvaluation

logger = structlog.get_logger()


async def get_dashboard_metrics(
    db: AsyncSession,
    workspace_id: UUID | None = None,
    agent_id: UUID | None = None,
    days: int = 7,
) -> dict[str, Any]:
    """Get aggregated dashboard metrics.

    Args:
        db: Database session
        workspace_id: Filter by workspace (optional)
        agent_id: Filter by agent (optional)
        days: Number of days to include

    Returns:
        Dict with dashboard metrics
    """
    since = datetime.now(UTC) - timedelta(days=days)

    # Build base query filters
    filters = [CallEvaluation.created_at >= since]
    if workspace_id:
        filters.append(CallEvaluation.workspace_id == workspace_id)
    if agent_id:
        filters.append(CallEvaluation.agent_id == agent_id)

    # Total evaluations
    total_result = await db.execute(
        select(func.count(CallEvaluation.id)).where(*filters)
    )
    total_evaluations = total_result.scalar() or 0

    if total_evaluations == 0:
        return _empty_metrics()

    # Pass rate
    passed_result = await db.execute(
        select(func.count(CallEvaluation.id)).where(
            *filters,
            CallEvaluation.passed == True,  # noqa: E712
        )
    )
    passed_count = passed_result.scalar() or 0
    pass_rate = passed_count / total_evaluations if total_evaluations > 0 else 0

    # Average scores
    scores_result = await db.execute(
        select(
            func.avg(CallEvaluation.overall_score),
            func.avg(CallEvaluation.intent_completion),
            func.avg(CallEvaluation.tool_usage),
            func.avg(CallEvaluation.compliance),
            func.avg(CallEvaluation.response_quality),
        ).where(*filters)
    )
    scores = scores_result.one()

    # Quality metrics (if enabled)
    quality_result = await db.execute(
        select(
            func.avg(CallEvaluation.coherence),
            func.avg(CallEvaluation.relevance),
            func.avg(CallEvaluation.groundedness),
            func.avg(CallEvaluation.fluency),
        ).where(*filters)
    )
    quality = quality_result.one()

    # Sentiment distribution
    sentiment_result = await db.execute(
        select(
            CallEvaluation.overall_sentiment,
            func.count(CallEvaluation.id),
        )
        .where(*filters)
        .group_by(CallEvaluation.overall_sentiment)
    )
    sentiment_dist = {row[0]: row[1] for row in sentiment_result.all() if row[0]}

    # Latency percentiles
    latency_result = await db.execute(
        select(
            func.avg(CallEvaluation.latency_p50_ms),
            func.avg(CallEvaluation.latency_p90_ms),
            func.avg(CallEvaluation.latency_p95_ms),
        ).where(*filters)
    )
    latency = latency_result.one()

    # Failed evaluations count
    failed_count = total_evaluations - passed_count

    return {
        "total_evaluations": total_evaluations,
        "passed_count": passed_count,
        "failed_count": failed_count,
        "pass_rate": round(pass_rate, 4),
        "average_score": round(scores[0] or 0, 2),
        "score_breakdown": {
            "intent_completion": round(scores[1] or 0, 2),
            "tool_usage": round(scores[2] or 0, 2),
            "compliance": round(scores[3] or 0, 2),
            "response_quality": round(scores[4] or 0, 2),
        },
        "quality_metrics": {
            "coherence": round(quality[0] or 0, 2),
            "relevance": round(quality[1] or 0, 2),
            "groundedness": round(quality[2] or 0, 2),
            "fluency": round(quality[3] or 0, 2),
        },
        "sentiment_distribution": sentiment_dist,
        "latency": {
            "p50_ms": round(latency[0] or 0, 2),
            "p90_ms": round(latency[1] or 0, 2),
            "p95_ms": round(latency[2] or 0, 2),
        },
        "period_days": days,
    }


def _empty_metrics() -> dict[str, Any]:
    """Return empty metrics structure."""
    return {
        "total_evaluations": 0,
        "passed_count": 0,
        "failed_count": 0,
        "pass_rate": 0,
        "average_score": 0,
        "score_breakdown": {
            "intent_completion": 0,
            "tool_usage": 0,
            "compliance": 0,
            "response_quality": 0,
        },
        "quality_metrics": {
            "coherence": 0,
            "relevance": 0,
            "groundedness": 0,
            "fluency": 0,
        },
        "sentiment_distribution": {},
        "latency": {
            "p50_ms": 0,
            "p90_ms": 0,
            "p95_ms": 0,
        },
        "period_days": 0,
    }


async def get_trends(
    db: AsyncSession,
    workspace_id: UUID | None = None,
    agent_id: UUID | None = None,
    metric: str = "overall_score",
    days: int = 30,
) -> dict[str, Any]:
    """Get trend data for charts.

    Args:
        db: Database session
        workspace_id: Filter by workspace (optional)
        agent_id: Filter by agent (optional)
        metric: Metric to trend (overall_score, pass_rate, etc.)
        days: Number of days to include

    Returns:
        Dict with trend data points
    """
    since = datetime.now(UTC) - timedelta(days=days)

    # Build base filters
    filters = [CallEvaluation.created_at >= since]
    if workspace_id:
        filters.append(CallEvaluation.workspace_id == workspace_id)
    if agent_id:
        filters.append(CallEvaluation.agent_id == agent_id)

    # Daily aggregation based on metric type
    if metric == "pass_rate":
        query = (
            select(
                func.date(CallEvaluation.created_at).label("date"),
                func.count(CallEvaluation.id).label("total"),
                func.sum(
                    case((CallEvaluation.passed == True, 1), else_=0)  # noqa: E712
                ).label("passed"),
            )
            .where(*filters)
            .group_by(func.date(CallEvaluation.created_at))
            .order_by(func.date(CallEvaluation.created_at))
        )
        result = await db.execute(query)
        rows = result.all()

        dates = []
        values = []
        for row in rows:
            dates.append(str(row.date))
            rate = (row.passed or 0) / row.total if row.total > 0 else 0
            values.append(round(rate, 4))

        return {"dates": dates, "values": values, "metric": metric}

    # Default: average score
    metric_col = getattr(CallEvaluation, metric, CallEvaluation.overall_score)
    query = (
        select(
            func.date(CallEvaluation.created_at).label("date"),
            func.avg(metric_col).label("avg_value"),
        )
        .where(*filters)
        .group_by(func.date(CallEvaluation.created_at))
        .order_by(func.date(CallEvaluation.created_at))
    )
    result = await db.execute(query)
    rows = result.all()

    dates = []
    values = []
    for row in rows:
        dates.append(str(row.date))
        values.append(round(row.avg_value or 0, 2))

    return {"dates": dates, "values": values, "metric": metric}


async def get_top_failure_reasons(
    db: AsyncSession,
    workspace_id: UUID | None = None,
    agent_id: UUID | None = None,
    days: int = 7,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Get top failure reasons with counts.

    Args:
        db: Database session
        workspace_id: Filter by workspace (optional)
        agent_id: Filter by agent (optional)
        days: Number of days to include
        limit: Max number of reasons to return

    Returns:
        List of failure reasons with counts
    """
    since = datetime.now(UTC) - timedelta(days=days)

    # Build filters
    filters = [
        CallEvaluation.created_at >= since,
        CallEvaluation.passed == False,  # noqa: E712
        CallEvaluation.failure_reasons.isnot(None),
    ]
    if workspace_id:
        filters.append(CallEvaluation.workspace_id == workspace_id)
    if agent_id:
        filters.append(CallEvaluation.agent_id == agent_id)

    # Get failed evaluations with failure reasons
    result = await db.execute(
        select(CallEvaluation.failure_reasons).where(*filters)
    )
    rows = result.all()

    # Aggregate failure reasons
    reason_counts: dict[str, int] = {}
    for row in rows:
        if row.failure_reasons:
            for reason in row.failure_reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1

    # Sort by count and take top N
    sorted_reasons = sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)
    top_reasons = sorted_reasons[:limit]

    return [{"reason": reason, "count": count} for reason, count in top_reasons]


async def get_agent_comparison(
    db: AsyncSession,
    workspace_id: UUID,
    days: int = 7,
) -> list[dict[str, Any]]:
    """Compare agents within a workspace.

    Args:
        db: Database session
        workspace_id: Workspace to compare agents in
        days: Number of days to include

    Returns:
        List of agent stats for comparison
    """
    since = datetime.now(UTC) - timedelta(days=days)

    query = (
        select(
            CallEvaluation.agent_id,
            func.count(CallEvaluation.id).label("total"),
            func.avg(CallEvaluation.overall_score).label("avg_score"),
            func.sum(
                case((CallEvaluation.passed == True, 1), else_=0)  # noqa: E712
            ).label("passed"),
        )
        .where(
            CallEvaluation.workspace_id == workspace_id,
            CallEvaluation.created_at >= since,
            CallEvaluation.agent_id.isnot(None),
        )
        .group_by(CallEvaluation.agent_id)
        .order_by(func.avg(CallEvaluation.overall_score).desc())
    )

    result = await db.execute(query)
    rows = result.all()

    agents = []
    for row in rows:
        pass_rate = (row.passed or 0) / row.total if row.total > 0 else 0
        agents.append({
            "agent_id": str(row.agent_id),
            "total_evaluations": row.total,
            "average_score": round(row.avg_score or 0, 2),
            "pass_rate": round(pass_rate, 4),
        })

    return agents
