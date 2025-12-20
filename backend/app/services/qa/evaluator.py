"""QA Evaluator Service for post-call evaluation using Claude API.

This service evaluates completed calls using Claude to generate quality scores
and actionable insights for improving voice agent performance.
"""

import time
import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.agent import Agent
from app.models.call_evaluation import CallEvaluation
from app.models.call_record import CallRecord

logger = structlog.get_logger()

# Evaluation prompt template
EVALUATION_PROMPT_V1 = """You are an expert QA evaluator for voice AI agents. Analyze this call transcript and provide a detailed evaluation.

## Agent Information
- Agent Name: {agent_name}
- System Prompt: {system_prompt}

## Call Information
- Direction: {direction}
- Duration: {duration_seconds} seconds
- Status: {status}

## Transcript
{transcript}

## Evaluation Criteria

Score each category from 0-100:

1. **Intent Completion** - Did the agent successfully identify and fulfill the caller's objectives?
2. **Tool Usage** - Were tools used appropriately and effectively?
3. **Compliance** - Did the agent follow the system prompt and maintain proper conduct?
4. **Response Quality** - Were responses clear, helpful, and contextually appropriate?
5. **Coherence** - Was the conversation logical and well-structured?
6. **Relevance** - Were responses relevant to the caller's needs?
7. **Groundedness** - Were responses factually grounded (not hallucinating)?
8. **Fluency** - Was the language natural and easy to understand?

## Response Format

Respond with a JSON object (no markdown code blocks):
{{
    "overall_score": <0-100>,
    "intent_completion": <0-100>,
    "tool_usage": <0-100 or null if no tools were used>,
    "compliance": <0-100>,
    "response_quality": <0-100>,
    "coherence": <0-100>,
    "relevance": <0-100>,
    "groundedness": <0-100>,
    "fluency": <0-100>,
    "overall_sentiment": "<positive|negative|neutral>",
    "sentiment_score": <-1.0 to 1.0>,
    "escalation_risk": <0.0 to 1.0>,
    "objectives_detected": ["objective1", "objective2"],
    "objectives_completed": ["objective1"],
    "failure_reasons": ["reason1"] or [],
    "recommendations": ["recommendation1", "recommendation2"],
    "turn_analysis": [
        {{
            "turn": 1,
            "speaker": "user|agent",
            "quality_score": <0-100>,
            "issues": ["issue1"] or []
        }}
    ]
}}
"""

# Token costs for Claude models (as of Dec 2024, in cents per 1K tokens)
MODEL_COSTS = {
    "claude-sonnet-4-20250514": {"input": 0.3, "output": 1.5},
    "claude-3-5-sonnet-20241022": {"input": 0.3, "output": 1.5},
    "claude-3-haiku-20240307": {"input": 0.025, "output": 0.125},
}


class QAEvaluator:
    """QA Evaluator using Claude API for post-call analysis."""

    def __init__(self, db: AsyncSession):
        """Initialize the evaluator.

        Args:
            db: Database session
        """
        self.db = db
        self.logger = logger.bind(component="qa_evaluator")
        self._client: Any = None

    async def _get_client(self) -> Any:
        """Get or create Anthropic client.

        Returns:
            Anthropic async client
        """
        if self._client is None:
            try:
                import anthropic

                api_key = settings.ANTHROPIC_API_KEY
                if not api_key:
                    msg = "ANTHROPIC_API_KEY not configured"
                    raise ValueError(msg)
                self._client = anthropic.AsyncAnthropic(api_key=api_key)
            except ImportError as e:
                msg = "anthropic package not installed"
                raise ImportError(msg) from e
        return self._client

    async def evaluate_call(  # noqa: PLR0911
        self, call_id: uuid.UUID
    ) -> CallEvaluation | None:
        """Evaluate a completed call.

        Args:
            call_id: UUID of the call to evaluate

        Returns:
            CallEvaluation if successful, None if evaluation failed or skipped
        """
        log = self.logger.bind(call_id=str(call_id))

        # Check if QA is enabled
        if not settings.QA_ENABLED:
            log.debug("qa_disabled")
            return None

        # Get call record with agent
        result = await self.db.execute(select(CallRecord).where(CallRecord.id == call_id))
        call_record = result.scalar_one_or_none()

        if not call_record:
            log.warning("call_not_found")
            return None

        # Check if already evaluated
        existing = await self.db.execute(
            select(CallEvaluation).where(CallEvaluation.call_id == call_id)
        )
        if existing.scalar_one_or_none():
            log.info("already_evaluated")
            return None

        # Need transcript for evaluation
        if not call_record.transcript:
            log.warning("no_transcript")
            return None

        # Get agent info
        agent: Agent | None = None
        if call_record.agent_id:
            agent_result = await self.db.execute(
                select(Agent).where(Agent.id == call_record.agent_id)
            )
            agent = agent_result.scalar_one_or_none()

        log.info(
            "starting_evaluation",
            agent_id=str(call_record.agent_id) if call_record.agent_id else None,
        )

        try:
            start_time = time.monotonic()

            # Build evaluation prompt
            prompt = EVALUATION_PROMPT_V1.format(
                agent_name=agent.name if agent else "Unknown Agent",
                system_prompt=agent.system_prompt if agent else "N/A",
                direction=call_record.direction,
                duration_seconds=call_record.duration_seconds,
                status=call_record.status,
                transcript=call_record.transcript,
            )

            # Call Claude API
            client = await self._get_client()
            model = settings.QA_EVALUATION_MODEL

            response = await client.messages.create(
                model=model,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )

            evaluation_latency_ms = int((time.monotonic() - start_time) * 1000)

            # Parse response
            response_text = response.content[0].text
            evaluation_data = self._parse_evaluation_response(response_text)

            if not evaluation_data:
                log.error("failed_to_parse_response", response=response_text[:500])
                return None

            # Calculate cost
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            cost_info = MODEL_COSTS.get(model, MODEL_COSTS["claude-sonnet-4-20250514"])
            cost_cents = (input_tokens / 1000) * cost_info["input"] + (
                output_tokens / 1000
            ) * cost_info["output"]

            # Determine pass/fail
            overall_score = evaluation_data.get("overall_score", 0)
            passed = overall_score >= settings.QA_DEFAULT_THRESHOLD

            # Create evaluation record
            evaluation = CallEvaluation(
                call_id=call_id,
                agent_id=call_record.agent_id,
                workspace_id=call_record.workspace_id,
                overall_score=overall_score,
                intent_completion=evaluation_data.get("intent_completion"),
                tool_usage=evaluation_data.get("tool_usage"),
                compliance=evaluation_data.get("compliance"),
                response_quality=evaluation_data.get("response_quality"),
                passed=passed,
                coherence=evaluation_data.get("coherence"),
                relevance=evaluation_data.get("relevance"),
                groundedness=evaluation_data.get("groundedness"),
                fluency=evaluation_data.get("fluency"),
                overall_sentiment=evaluation_data.get("overall_sentiment"),
                sentiment_score=evaluation_data.get("sentiment_score"),
                escalation_risk=evaluation_data.get("escalation_risk"),
                objectives_detected=evaluation_data.get("objectives_detected"),
                objectives_completed=evaluation_data.get("objectives_completed"),
                failure_reasons=evaluation_data.get("failure_reasons"),
                recommendations=evaluation_data.get("recommendations"),
                turn_analysis=evaluation_data.get("turn_analysis"),
                evaluation_model=model,
                evaluation_latency_ms=evaluation_latency_ms,
                evaluation_cost_cents=cost_cents,
                evaluation_prompt_version="v1",
            )

            self.db.add(evaluation)
            await self.db.commit()
            await self.db.refresh(evaluation)

            log.info(
                "evaluation_completed",
                overall_score=overall_score,
                passed=passed,
                latency_ms=evaluation_latency_ms,
                cost_cents=round(cost_cents, 4),
            )

            # Send failure alerts if evaluation failed
            if not passed:
                await self._send_failure_alerts(evaluation)

            return evaluation

        except Exception:
            log.exception("evaluation_failed")
            return None

    def _parse_evaluation_response(self, response_text: str) -> dict[str, Any] | None:
        """Parse Claude's JSON response.

        Args:
            response_text: Raw response text from Claude

        Returns:
            Parsed evaluation data or None if parsing failed
        """
        import json
        import re
        from typing import cast

        def coerce_numeric_fields(data: dict[str, Any]) -> dict[str, Any]:
            """Coerce string numbers to proper types for numeric fields."""
            numeric_int_fields = [
                "overall_score", "intent_completion", "tool_usage", "compliance",
                "response_quality", "coherence", "relevance", "groundedness", "fluency",
            ]
            numeric_float_fields = ["sentiment_score", "escalation_risk"]

            for field in numeric_int_fields:
                if field in data and data[field] is not None:
                    try:
                        data[field] = int(float(str(data[field])))
                    except (ValueError, TypeError):
                        data[field] = None

            for field in numeric_float_fields:
                if field in data and data[field] is not None:
                    try:
                        data[field] = float(str(data[field]))
                    except (ValueError, TypeError):
                        data[field] = None

            return data

        # Try to parse as-is first
        try:
            result = json.loads(response_text)
            if isinstance(result, dict):
                return coerce_numeric_fields(cast("dict[str, Any]", result))
        except json.JSONDecodeError:
            pass

        # Try to extract JSON from markdown code blocks
        json_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", response_text)
        if json_match:
            try:
                result = json.loads(json_match.group(1))
                if isinstance(result, dict):
                    return coerce_numeric_fields(cast("dict[str, Any]", result))
            except json.JSONDecodeError:
                pass

        # Try to find JSON object in response (non-greedy to get first complete object)
        json_match = re.search(r"\{[\s\S]*?\}", response_text)
        if json_match:
            # Try progressively larger matches until we get valid JSON
            start_idx = json_match.start()
            for end_idx in range(json_match.end(), len(response_text) + 1):
                candidate = response_text[start_idx:end_idx]
                if candidate.count("{") == candidate.count("}"):
                    try:
                        result = json.loads(candidate)
                        if isinstance(result, dict):
                            return coerce_numeric_fields(cast("dict[str, Any]", result))
                    except json.JSONDecodeError:
                        continue

        return None

    async def _send_failure_alerts(self, evaluation: CallEvaluation) -> None:
        """Send failure alerts when an evaluation fails.

        Args:
            evaluation: The failed CallEvaluation
        """
        from app.services.qa.alerts import create_alert, send_failure_alert

        log = self.logger.bind(
            evaluation_id=str(evaluation.id),
            call_id=str(evaluation.call_id),
        )

        try:
            # Send webhook/Slack alerts
            await send_failure_alert(self.db, evaluation)

            # Create an alert record in the workspace
            if evaluation.workspace_id:
                severity = "high" if evaluation.overall_score < 50 else "medium"  # noqa: PLR2004
                failure_reasons = evaluation.failure_reasons or []
                message = f"Call evaluation failed with score {evaluation.overall_score}/100"
                if failure_reasons:
                    message += f": {', '.join(failure_reasons[:3])}"

                await create_alert(
                    db=self.db,
                    alert_type="qa_failure",
                    severity=severity,
                    workspace_id=evaluation.workspace_id,
                    agent_id=evaluation.agent_id,
                    message=message,
                    metadata={
                        "evaluation_id": str(evaluation.id),
                        "call_id": str(evaluation.call_id),
                        "overall_score": evaluation.overall_score,
                        "failure_reasons": failure_reasons,
                    },
                )

            log.info("failure_alerts_sent")

        except Exception:
            log.exception("failed_to_send_failure_alerts")


async def trigger_qa_evaluation(call_id: uuid.UUID) -> None:
    """Background task to trigger QA evaluation for a completed call.

    This function creates its own database session to avoid issues with
    session lifecycle in background tasks.

    Args:
        call_id: UUID of the call to evaluate
    """
    log = logger.bind(call_id=str(call_id), component="qa_trigger")

    if not settings.QA_ENABLED:
        log.debug("qa_disabled_skipping")
        return

    if not settings.QA_AUTO_EVALUATE:
        log.debug("auto_evaluate_disabled")
        return

    log.info("triggering_qa_evaluation")

    try:
        async with AsyncSessionLocal() as db:
            evaluator = QAEvaluator(db)
            await evaluator.evaluate_call(call_id)
    except Exception:
        log.exception("qa_evaluation_trigger_failed")
