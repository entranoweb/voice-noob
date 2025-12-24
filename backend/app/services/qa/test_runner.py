"""Test Runner service for executing test scenarios against voice agents.

Simulates conversations and evaluates agent responses against expected behaviors.
"""

import time
import uuid
from datetime import UTC, datetime
from typing import Any

import anthropic
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.agent import Agent
from app.models.test_scenario import TestRun, TestRunStatus, TestScenario
from app.services.qa.resilience import (
    call_claude_with_resilience,
    get_anthropic_client,
)
from app.services.qa.scenarios import get_built_in_scenarios

logger = structlog.get_logger()

# Evaluation prompt for test runs
TEST_EVALUATION_PROMPT = """You are evaluating a voice agent's response in a test scenario.

## Scenario Information
- Name: {scenario_name}
- Category: {category}
- Caller Persona: {caller_persona}

## Expected Behaviors
{expected_behaviors}

## Success Criteria
{success_criteria}

## Agent's System Prompt
{system_prompt}

## Conversation
{conversation}

## Evaluation Task

Analyze the agent's responses and determine:
1. Did the agent exhibit each expected behavior? (yes/no for each)
2. Were the success criteria met?
3. What issues were found (if any)?
4. What recommendations would improve the agent?
5. Overall score (0-100)
6. Pass/Fail determination

Respond with JSON (no markdown):
{{
    "overall_score": <0-100>,
    "passed": <true/false>,
    "behavior_matches": {{
        "<behavior>": <true/false>,
        ...
    }},
    "criteria_results": {{
        "<criterion>": {{"met": <true/false>, "reason": "<explanation>"}},
        ...
    }},
    "issues_found": ["<issue1>", "<issue2>"],
    "recommendations": ["<recommendation1>", "<recommendation2>"]
}}
"""


class TestRunner:
    """Executes test scenarios against voice agents."""

    def __init__(self, db: AsyncSession):
        """Initialize the test runner.

        Args:
            db: Database session
        """
        self.db = db
        self.logger = logger.bind(component="test_runner")
        self._client: Any = None

    async def _get_client(self) -> anthropic.AsyncAnthropic:
        """Get or create Anthropic client with timeout configured.

        Returns:
            Anthropic async client with resilience settings.

        Raises:
            ValueError: If ANTHROPIC_API_KEY not configured.
        """
        if self._client is None:
            self._client = get_anthropic_client()
        return self._client  # type: ignore[no-any-return]

    async def seed_built_in_scenarios(self) -> int:
        """Seed built-in test scenarios to database.

        Returns:
            Number of scenarios created
        """
        log = self.logger.bind(action="seed_scenarios")

        # Check if already seeded
        result = await self.db.execute(
            select(TestScenario).where(TestScenario.is_built_in == True)  # noqa: E712
        )
        existing = result.scalars().all()

        if existing:
            log.info("scenarios_already_seeded", count=len(existing))
            return 0

        # Seed scenarios
        scenarios = get_built_in_scenarios()
        created = 0

        for scenario_data in scenarios:
            scenario = TestScenario(
                name=scenario_data["name"],
                description=scenario_data["description"],
                category=scenario_data["category"],
                difficulty=scenario_data["difficulty"],
                caller_persona=scenario_data["caller_persona"],
                conversation_flow=scenario_data["conversation_flow"],
                expected_behaviors=scenario_data["expected_behaviors"],
                expected_tool_calls=scenario_data.get("expected_tool_calls"),
                success_criteria=scenario_data["success_criteria"],
                is_active=True,
                is_built_in=True,
                tags=scenario_data.get("tags"),
            )
            self.db.add(scenario)
            created += 1

        await self.db.commit()
        log.info("scenarios_seeded", count=created)
        return created

    async def run_scenario(
        self,
        scenario_id: uuid.UUID,
        agent_id: uuid.UUID,
        user_id: int,
        workspace_id: uuid.UUID | None = None,
    ) -> TestRun:
        """Execute a test scenario against an agent.

        Args:
            scenario_id: ID of the scenario to run
            agent_id: ID of the agent to test
            user_id: ID of the user running the test
            workspace_id: Optional workspace ID

        Returns:
            TestRun with results
        """
        log = self.logger.bind(
            scenario_id=str(scenario_id),
            agent_id=str(agent_id),
        )

        # Get scenario
        scenario_result = await self.db.execute(
            select(TestScenario).where(TestScenario.id == scenario_id)
        )
        scenario = scenario_result.scalar_one_or_none()

        if not scenario:
            msg = f"Scenario {scenario_id} not found"
            raise ValueError(msg)

        # Get agent
        agent_result = await self.db.execute(select(Agent).where(Agent.id == agent_id))
        agent = agent_result.scalar_one_or_none()

        if not agent:
            msg = f"Agent {agent_id} not found"
            raise ValueError(msg)

        # Create test run record
        test_run = TestRun(
            scenario_id=scenario_id,
            agent_id=agent_id,
            workspace_id=workspace_id,
            user_id=user_id,
            status=TestRunStatus.RUNNING.value,
            started_at=datetime.now(UTC),
        )
        self.db.add(test_run)
        await self.db.commit()
        await self.db.refresh(test_run)

        log.info("test_run_started", test_run_id=str(test_run.id))

        try:
            start_time = time.monotonic()

            # Simulate conversation and get agent responses
            conversation = await self._simulate_conversation(
                agent=agent,
                scenario=scenario,
            )

            # Evaluate the conversation
            evaluation = await self._evaluate_conversation(
                agent=agent,
                scenario=scenario,
                conversation=conversation,
            )

            duration_ms = int((time.monotonic() - start_time) * 1000)

            # Update test run with results
            test_run.status = (
                TestRunStatus.PASSED.value if evaluation["passed"] else TestRunStatus.FAILED.value
            )
            test_run.completed_at = datetime.now(UTC)
            test_run.duration_ms = duration_ms
            test_run.overall_score = evaluation.get("overall_score", 0)
            test_run.passed = evaluation["passed"]
            test_run.actual_transcript = conversation
            test_run.behavior_matches = evaluation.get("behavior_matches")
            test_run.criteria_results = evaluation.get("criteria_results")
            test_run.issues_found = evaluation.get("issues_found")
            test_run.recommendations = evaluation.get("recommendations")

            await self.db.commit()
            await self.db.refresh(test_run)

            log.info(
                "test_run_completed",
                test_run_id=str(test_run.id),
                passed=test_run.passed,
                score=test_run.overall_score,
                duration_ms=duration_ms,
            )

        except Exception as e:
            log.exception("test_run_failed", error=str(e))
            test_run.status = TestRunStatus.ERROR.value
            test_run.completed_at = datetime.now(UTC)
            test_run.error_message = str(e)
            await self.db.commit()

        return test_run

    async def _simulate_conversation(
        self,
        agent: Agent,
        scenario: TestScenario,
    ) -> list[dict[str, Any]]:
        """Simulate a conversation using the scenario's conversation flow.

        Uses Claude to generate agent responses based on the agent's system prompt.

        Args:
            agent: The agent being tested
            scenario: The test scenario

        Returns:
            List of conversation turns with actual agent responses
        """
        client = await self._get_client()
        conversation: list[dict[str, Any]] = []

        # Build conversation history for context
        messages: list[dict[str, str]] = []

        for turn in scenario.conversation_flow:
            if turn["speaker"] == "user":
                # Add user message
                user_message = turn["message"]
                messages.append({"role": "user", "content": user_message})
                conversation.append(
                    {
                        "speaker": "user",
                        "message": user_message,
                        "timestamp": datetime.now(UTC).isoformat(),
                    }
                )

                # Get agent response with resilience (retry + circuit breaker)
                response = await call_claude_with_resilience(
                    client=client,
                    model=settings.QA_EVALUATION_MODEL,
                    max_tokens=500,
                    messages=messages,
                    system=agent.system_prompt,
                )

                agent_response = response.content[0].text
                messages.append({"role": "assistant", "content": agent_response})
                conversation.append(
                    {
                        "speaker": "agent",
                        "message": agent_response,
                        "timestamp": datetime.now(UTC).isoformat(),
                    }
                )

        return conversation

    async def _evaluate_conversation(
        self,
        agent: Agent,
        scenario: TestScenario,
        conversation: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Evaluate the conversation against expected behaviors.

        Args:
            agent: The agent being tested
            scenario: The test scenario
            conversation: The actual conversation

        Returns:
            Evaluation results
        """
        import json
        import re
        from typing import cast

        client = await self._get_client()

        # Format conversation for evaluation
        conv_text = "\n".join(
            [f"{turn['speaker'].upper()}: {turn['message']}" for turn in conversation]
        )

        # Format expected behaviors
        behaviors_text = "\n".join([f"- {behavior}" for behavior in scenario.expected_behaviors])

        # Build evaluation prompt
        prompt = TEST_EVALUATION_PROMPT.format(
            scenario_name=scenario.name,
            category=scenario.category,
            caller_persona=json.dumps(scenario.caller_persona, indent=2),
            expected_behaviors=behaviors_text,
            success_criteria=json.dumps(scenario.success_criteria, indent=2),
            system_prompt=agent.system_prompt[:1000],  # Truncate if too long
            conversation=conv_text,
        )

        response = await call_claude_with_resilience(
            client=client,
            model=settings.QA_EVALUATION_MODEL,
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )

        response_text = response.content[0].text

        # Parse JSON response
        try:
            result = json.loads(response_text)
            if isinstance(result, dict):
                return cast("dict[str, Any]", result)
        except json.JSONDecodeError:
            pass

        # Try to extract JSON from markdown
        json_match = re.search(r"\{[\s\S]*\}", response_text)
        if json_match:
            try:
                result = json.loads(json_match.group(0))
                if isinstance(result, dict):
                    return cast("dict[str, Any]", result)
            except json.JSONDecodeError:
                pass

        # Default response if parsing fails
        return {
            "overall_score": 50,
            "passed": False,
            "behavior_matches": {},
            "criteria_results": {},
            "issues_found": ["Failed to parse evaluation response"],
            "recommendations": ["Re-run the test"],
        }

    async def run_all_scenarios(
        self,
        agent_id: uuid.UUID,
        user_id: int,
        workspace_id: uuid.UUID | None = None,
        category: str | None = None,
    ) -> list[TestRun]:
        """Run all active scenarios against an agent.

        Args:
            agent_id: ID of the agent to test
            user_id: ID of the user running the tests
            workspace_id: Optional workspace ID
            category: Optional category filter

        Returns:
            List of TestRun results
        """
        log = self.logger.bind(agent_id=str(agent_id))

        # Get all active scenarios
        query = select(TestScenario).where(TestScenario.is_active == True)  # noqa: E712
        if category:
            query = query.where(TestScenario.category == category)

        result = await self.db.execute(query)
        scenarios = result.scalars().all()

        log.info("running_all_scenarios", count=len(scenarios))

        results: list[TestRun] = []
        for scenario in scenarios:
            try:
                test_run = await self.run_scenario(
                    scenario_id=scenario.id,
                    agent_id=agent_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                )
                results.append(test_run)
            except Exception:
                log.exception("scenario_failed", scenario_id=str(scenario.id))

        return results


async def seed_scenarios_background() -> None:
    """Background task to seed built-in scenarios.

    Creates its own database session.
    """
    log = logger.bind(component="scenario_seeder")
    log.info("seeding_scenarios")

    try:
        async with AsyncSessionLocal() as db:
            runner = TestRunner(db)
            count = await runner.seed_built_in_scenarios()
            log.info("scenarios_seeded", count=count)
    except Exception:
        log.exception("seeding_failed")
