"""QA Alert Service for sending failure notifications.

Sends alerts via webhooks when calls fail QA evaluation.
"""

import hashlib
import hmac
import time
import uuid
from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.call_evaluation import CallEvaluation
from app.models.workspace import Workspace

logger = structlog.get_logger()


async def send_failure_alert(
    db: AsyncSession,
    evaluation: CallEvaluation,
) -> bool:
    """Send failure alert for a failed evaluation.

    Args:
        db: Database session
        evaluation: The failed CallEvaluation

    Returns:
        True if alert was sent successfully, False otherwise
    """
    log = logger.bind(
        evaluation_id=str(evaluation.id),
        call_id=str(evaluation.call_id),
        component="qa_alerts",
    )

    if not settings.QA_ALERT_ON_FAILURE:
        log.debug("alerts_disabled")
        return False

    # Get workspace settings for alert configuration
    webhook_url = None
    slack_webhook = None

    if evaluation.workspace_id:
        result = await db.execute(select(Workspace).where(Workspace.id == evaluation.workspace_id))
        workspace = result.scalar_one_or_none()
        if workspace and workspace.settings:
            webhook_url = workspace.settings.get("qa_alert_webhook")
            slack_webhook = workspace.settings.get("qa_slack_webhook")

    if not webhook_url and not slack_webhook:
        log.debug("no_alert_webhooks_configured")
        return False

    # Build alert payload
    alert_payload = _build_alert_payload(evaluation)

    sent = False

    # Send to generic webhook
    if webhook_url:
        try:
            success = await _send_webhook_alert(webhook_url, alert_payload)
            if success:
                log.info("webhook_alert_sent", webhook_url=webhook_url[:50])
                sent = True
        except Exception:
            log.exception("webhook_alert_failed")

    # Send to Slack
    if slack_webhook:
        try:
            success = await _send_slack_alert(slack_webhook, evaluation)
            if success:
                log.info("slack_alert_sent")
                sent = True
        except Exception:
            log.exception("slack_alert_failed")

    return sent


def _build_alert_payload(evaluation: CallEvaluation) -> dict[str, Any]:
    """Build webhook alert payload.

    Args:
        evaluation: The CallEvaluation

    Returns:
        Alert payload dict
    """
    return {
        "alert_type": "qa_failure",
        "severity": "high" if evaluation.overall_score < 50 else "medium",  # noqa: PLR2004
        "timestamp": time.time(),
        "evaluation": {
            "id": str(evaluation.id),
            "call_id": str(evaluation.call_id),
            "agent_id": str(evaluation.agent_id) if evaluation.agent_id else None,
            "overall_score": evaluation.overall_score,
            "passed": evaluation.passed,
            "failure_reasons": evaluation.failure_reasons or [],
            "recommendations": evaluation.recommendations or [],
            "scores": {
                "intent_completion": evaluation.intent_completion,
                "tool_usage": evaluation.tool_usage,
                "compliance": evaluation.compliance,
                "response_quality": evaluation.response_quality,
            },
        },
    }


async def _send_webhook_alert(
    webhook_url: str,
    payload: dict[str, Any],
    secret: str | None = None,
) -> bool:
    """Send alert to a webhook URL.

    Args:
        webhook_url: Target webhook URL
        payload: Alert payload
        secret: Optional secret for HMAC signature

    Returns:
        True if successful
    """
    headers = {"Content-Type": "application/json"}

    # Add HMAC signature if secret provided
    if secret:
        import json

        body = json.dumps(payload)
        signature = hmac.new(
            secret.encode(),
            body.encode(),
            hashlib.sha256,
        ).hexdigest()
        headers["X-Signature-256"] = f"sha256={signature}"

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(webhook_url, json=payload, headers=headers)
        return response.status_code < 400  # noqa: PLR2004


async def _send_slack_alert(
    slack_webhook: str,
    evaluation: CallEvaluation,
) -> bool:
    """Send alert to Slack webhook.

    Args:
        slack_webhook: Slack incoming webhook URL
        evaluation: The CallEvaluation

    Returns:
        True if successful
    """
    # Build Slack message blocks
    severity_emoji = "🔴" if evaluation.overall_score < 50 else "🟡"  # noqa: PLR2004
    failure_reasons = evaluation.failure_reasons or ["No specific reasons identified"]

    slack_payload = {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{severity_emoji} QA Evaluation Failed",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Score:* {evaluation.overall_score}/100"},
                    {"type": "mrkdwn", "text": f"*Call ID:* `{evaluation.call_id}`"},
                    {
                        "type": "mrkdwn",
                        "text": f"*Agent ID:* `{evaluation.agent_id or 'N/A'}`",
                    },
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Failure Reasons:*\n• " + "\n• ".join(failure_reasons[:5]),
                },
            },
        ],
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(slack_webhook, json=slack_payload)
        return response.status_code == 200  # noqa: PLR2004


async def check_score_drop_alert(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    threshold_drop: float = 10.0,
) -> bool:
    """Check if agent's score has dropped significantly.

    Args:
        db: Database session
        workspace_id: Workspace ID
        agent_id: Agent ID
        threshold_drop: Minimum score drop to trigger alert (percentage points)

    Returns:
        True if alert was triggered
    """
    # Get recent evaluations (last 7 days vs previous 7 days)
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func

    now = datetime.now(UTC)
    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)

    # Recent week average
    recent_result = await db.execute(
        select(func.avg(CallEvaluation.overall_score)).where(
            CallEvaluation.workspace_id == workspace_id,
            CallEvaluation.agent_id == agent_id,
            CallEvaluation.created_at >= week_ago,
        )
    )
    recent_avg = recent_result.scalar()

    # Previous week average
    prev_result = await db.execute(
        select(func.avg(CallEvaluation.overall_score)).where(
            CallEvaluation.workspace_id == workspace_id,
            CallEvaluation.agent_id == agent_id,
            CallEvaluation.created_at >= two_weeks_ago,
            CallEvaluation.created_at < week_ago,
        )
    )
    prev_avg = prev_result.scalar()

    if recent_avg is None or prev_avg is None:
        return False

    drop = prev_avg - recent_avg
    if drop >= threshold_drop:
        logger.warning(
            "score_drop_detected",
            agent_id=str(agent_id),
            previous_avg=prev_avg,
            recent_avg=recent_avg,
            drop=drop,
        )
        return True

    return False


async def check_failure_spike_alert(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    threshold_rate: float = 0.3,
) -> bool:
    """Check if agent's failure rate has spiked.

    Args:
        db: Database session
        workspace_id: Workspace ID
        agent_id: Agent ID
        threshold_rate: Failure rate threshold to trigger alert (0.0-1.0)

    Returns:
        True if alert was triggered
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func

    # Get last 24 hours failure rate
    day_ago = datetime.now(UTC) - timedelta(days=1)

    total_result = await db.execute(
        select(func.count(CallEvaluation.id)).where(
            CallEvaluation.workspace_id == workspace_id,
            CallEvaluation.agent_id == agent_id,
            CallEvaluation.created_at >= day_ago,
        )
    )
    total = total_result.scalar() or 0

    if total < 5:  # Not enough data  # noqa: PLR2004
        return False

    failed_result = await db.execute(
        select(func.count(CallEvaluation.id)).where(
            CallEvaluation.workspace_id == workspace_id,
            CallEvaluation.agent_id == agent_id,
            CallEvaluation.created_at >= day_ago,
            CallEvaluation.passed == False,  # noqa: E712
        )
    )
    failed = failed_result.scalar() or 0

    failure_rate = failed / total
    if failure_rate >= threshold_rate:
        logger.warning(
            "failure_spike_detected",
            agent_id=str(agent_id),
            failure_rate=failure_rate,
            failed=failed,
            total=total,
        )
        return True

    return False


async def create_alert(
    db: AsyncSession,
    alert_type: str,
    severity: str,
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID | None = None,
    message: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create and store a QA alert in workspace settings.

    Alerts are stored in workspace.settings["qa_alerts"] as a list.
    This avoids needing a separate table for MVP.

    Args:
        db: Database session
        alert_type: Type of alert (score_drop, failure_spike, compliance_breach)
        severity: Alert severity (low, medium, high, critical)
        workspace_id: Workspace ID
        agent_id: Optional agent ID
        message: Alert message
        metadata: Optional additional metadata

    Returns:
        The created alert dict
    """
    from datetime import UTC, datetime

    result = await db.execute(select(Workspace).where(Workspace.id == workspace_id))
    workspace = result.scalar_one_or_none()

    if not workspace:
        logger.warning("workspace_not_found_for_alert", workspace_id=str(workspace_id))
        return {}

    # Create alert record
    alert = {
        "id": str(uuid.uuid4()),
        "type": alert_type,
        "severity": severity,
        "agent_id": str(agent_id) if agent_id else None,
        "message": message,
        "metadata": metadata or {},
        "acknowledged": False,
        "acknowledged_at": None,
        "acknowledged_by": None,
        "created_at": datetime.now(UTC).isoformat(),
    }

    # Store in workspace settings
    settings_dict = workspace.settings or {}
    alerts_list = settings_dict.get("qa_alerts", [])

    # Keep only last 100 alerts
    alerts_list = [*alerts_list[-99:], alert]
    settings_dict["qa_alerts"] = alerts_list
    workspace.settings = settings_dict

    await db.commit()

    logger.info(
        "alert_created",
        alert_id=alert["id"],
        alert_type=alert_type,
        severity=severity,
        workspace_id=str(workspace_id),
    )

    return alert


async def get_alerts(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    acknowledged: bool | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Get QA alerts for a workspace.

    Args:
        db: Database session
        workspace_id: Workspace ID
        acknowledged: Filter by acknowledged status (None = all)
        limit: Max alerts to return

    Returns:
        List of alert dicts
    """
    result = await db.execute(select(Workspace).where(Workspace.id == workspace_id))
    workspace = result.scalar_one_or_none()

    if not workspace or not workspace.settings:
        return []

    alerts = workspace.settings.get("qa_alerts", [])

    # Filter by acknowledged status if specified
    if acknowledged is not None:
        alerts = [a for a in alerts if a.get("acknowledged") == acknowledged]

    # Return most recent first, limited
    return list(reversed(alerts[-limit:]))


async def acknowledge_alert(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    alert_id: str,
    user_id: int,
) -> dict[str, Any] | None:
    """Acknowledge an alert.

    Args:
        db: Database session
        workspace_id: Workspace ID
        alert_id: Alert ID to acknowledge
        user_id: User acknowledging the alert

    Returns:
        Updated alert dict or None if not found
    """
    from datetime import UTC, datetime

    result = await db.execute(select(Workspace).where(Workspace.id == workspace_id))
    workspace = result.scalar_one_or_none()

    if not workspace or not workspace.settings:
        return None

    alerts = workspace.settings.get("qa_alerts", [])

    # Find and update the alert
    for alert in alerts:
        if alert.get("id") == alert_id:
            alert["acknowledged"] = True
            alert["acknowledged_at"] = datetime.now(UTC).isoformat()
            alert["acknowledged_by"] = user_id

            workspace.settings = {**workspace.settings, "qa_alerts": alerts}
            await db.commit()

            logger.info("alert_acknowledged", alert_id=alert_id, user_id=user_id)
            return dict(alert)

    return None
