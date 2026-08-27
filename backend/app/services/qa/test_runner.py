"""Test Runner service for executing test scenarios against voice agents.

Simulates conversations and evaluates agent responses against expected behaviors.
"""

import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import anthropic
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.db.session import engine as default_engine
from app.models.agent import Agent
from app.models.test_scenario import TestRun, TestRunStatus, TestScenario
from app.monitoring.call_trace import TerminationReason
from app.services.qa.fixtures import FixtureLedger, fixture_scope
from app.services.qa.metrics.context import build_context
from app.services.qa.metrics.runner import MetricResults, RunOutcome, evaluate
from app.services.qa.metrics.snapshot import capture_crm_state
from app.services.qa.resilience import (
    call_claude_with_resilience,
    get_anthropic_client,
)
from app.services.qa.scenarios import get_built_in_scenarios
from app.services.qa.tool_binding import BoundTools, bind_agent_tools

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


# Metric outcome -> stored status. RunOutcome.ERROR maps to ERROR rather than
# FAILED so a broken harness never shows up as a failing agent.
_STATUS_BY_OUTCOME = {
    RunOutcome.PASSED: TestRunStatus.PASSED.value,
    RunOutcome.FAILED: TestRunStatus.FAILED.value,
    RunOutcome.ERROR: TestRunStatus.ERROR.value,
}


def _serialise_scores(results: MetricResults) -> dict[str, Any]:
    """Store every metric score, not just the verdict.

    Keeping the per-metric detail makes a failure diagnosable after the fact -
    which tool was missing, what the database diff was - instead of leaving a
    bare pass/fail nobody can act on.
    """
    return {
        "outcome": str(results.outcome),
        "trustworthy": results.trustworthy,
        "accuracy_score": results.accuracy_score(),
        "metrics": {
            score.metric: {
                "version": score.version,
                "category": str(score.category),
                "kind": str(score.kind),
                "value": score.value,
                "passed": score.passed,
                "unit": score.unit,
                "detail": score.detail,
            }
            for score in results.scores
        },
    }


# How many times an agent turn may go round the tool loop before we stop it. A
# model that keeps calling tools without ever answering would otherwise run
# until the token budget or the API bill did it for us.
MAX_TOOL_ITERATIONS = 6

# Tool results can be large. Truncated before going back to the model because a
# single verbose listing should not crowd out the conversation; the untruncated
# result is still recorded for the metrics.
MAX_TOOL_RESULT_CHARS = 2000


def _tool_result_text(result: Any) -> str:
    """Render a tool result for the model.

    Truncated, and never allowed to raise: a result the runtime cannot serialise
    would otherwise abort a run that had already done real work.
    """
    try:
        rendered = json.dumps(result, default=str)
    except (TypeError, ValueError):
        rendered = str(result)
    if len(rendered) > MAX_TOOL_RESULT_CHARS:
        return rendered[:MAX_TOOL_RESULT_CHARS] + "... (truncated)"
    return rendered


def _tool_calls_from_conversation(
    conversation: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Extract recorded tool invocations from a conversation.

    Populated by the simulation, which executes the agent's real tools against
    the real database rather than mocking their responses. Asserting on what a
    mocked tool was asked to do only tests the model; asserting on what the
    database looks like afterwards tests the thing the customer cares about.
    """
    calls: list[dict[str, Any]] = []
    for entry in conversation:
        if isinstance(entry, dict):
            calls.extend(c for c in entry.get("tool_calls", []) if isinstance(c, dict))
    return calls


def _store_results(
    test_run: TestRun,
    *,
    conversation: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    evaluation: dict[str, Any],
    metric_results: MetricResults,
    duration_ms: int,
) -> None:
    """Write one run's outcome onto its TestRun record."""
    test_run.status = _STATUS_BY_OUTCOME[metric_results.outcome]
    test_run.completed_at = datetime.now(UTC)
    test_run.duration_ms = duration_ms
    test_run.overall_score = evaluation.get("overall_score", 0)
    # None rather than False when the run could not be measured: an unmeasured
    # run is not a failed one, and recording it as failed is how harness
    # problems became agent failure alerts.
    test_run.passed = (
        metric_results.outcome is RunOutcome.PASSED if metric_results.trustworthy else None
    )
    test_run.actual_transcript = conversation
    test_run.actual_tool_calls = tool_calls
    test_run.behavior_matches = evaluation.get("behavior_matches")
    test_run.criteria_results = _serialise_scores(metric_results)
    test_run.issues_found = evaluation.get("issues_found")
    test_run.recommendations = evaluation.get("recommendations")
    if not metric_results.trustworthy:
        test_run.error_message = "; ".join(metric_results.invalid_reasons)


def _engine_of(session: AsyncSession) -> AsyncEngine:
    """The engine a session is bound to, falling back to the application one.

    Taken from the session rather than imported directly so that an isolated run
    opens its connection on the same database the caller is already talking to.
    Importing the module-level engine would have a test, or anything pointed at
    a different database, quietly run its fixtures somewhere else.
    """
    bind = getattr(session, "bind", None)
    if isinstance(bind, AsyncEngine):
        return bind
    return default_engine


class TestRunner:
    """Executes test scenarios against voice agents."""

    def __init__(self, db: AsyncSession, engine: AsyncEngine | None = None):
        """Initialize the test runner.

        Args:
            db: Database session, used for the run's own bookkeeping.
            engine: Engine the isolated run opens its own connection on.
                Defaults to whatever the session is bound to, so an isolated run
                always reaches the same database the caller is already using.
        """
        self.db = db
        self.engine = engine if engine is not None else _engine_of(db)
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
                fixture=scenario_data.get("fixture"),
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
        isolated: bool = True,
    ) -> TestRun:
        """Execute a test scenario against an agent.

        Args:
            scenario_id: ID of the scenario to run
            agent_id: ID of the agent to test
            user_id: ID of the user running the test
            workspace_id: Optional workspace ID
            isolated: Run inside a transaction that is rolled back afterwards,
                so the agent's real tool calls leave nothing behind. On by
                default: a test that quietly writes into someone's live CRM is
                the surprising behaviour, not the safe one.

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

            conversation, evaluation, final_db_state, ledger = await self._execute(
                agent=agent,
                scenario=scenario,
                user_id=user_id,
                workspace_id=workspace_id,
                isolated=isolated,
            )

            duration_ms = int((time.monotonic() - start_time) * 1000)

            # Deterministic metrics decide the verdict. Previously the judge
            # returned its own `passed` and the runner stored it unchallenged,
            # so a scenario's min_score and must_invoke_tools were serialised
            # into the prompt as prose and never actually enforced.
            tool_calls = _tool_calls_from_conversation(conversation)
            metric_results = evaluate(
                build_context(
                    run_id=str(test_run.id),
                    conversation=conversation,
                    tool_calls=tool_calls,
                    expected_tool_calls=scenario.expected_tool_calls,
                    success_criteria=scenario.success_criteria,
                    termination_reason=TerminationReason.AGENT_ENDED,
                    duration_ms=duration_ms,
                    final_db_state=final_db_state,
                    fixture_ledger=ledger.as_dict() if ledger else None,
                ),
            )

            _store_results(
                test_run,
                conversation=conversation,
                tool_calls=tool_calls,
                evaluation=evaluation,
                metric_results=metric_results,
                duration_ms=duration_ms,
            )

            await self.db.commit()
            await self.db.refresh(test_run)

            log.info(
                "test_run_completed",
                test_run_id=str(test_run.id),
                outcome=str(metric_results.outcome),
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

    async def _execute(
        self,
        *,
        agent: Agent,
        scenario: TestScenario,
        user_id: int,
        workspace_id: uuid.UUID | None,
        isolated: bool,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], FixtureLedger | None]:
        """Run the conversation and capture the state it produced.

        The snapshot has to be taken *inside* the scope, while the agent's
        writes are still visible; the ledger is only complete once the scope has
        exited and rollback has been verified. Hence the ordering here.
        """
        if not isolated:
            # Unscoped: whatever the agent's tools did stays in the database.
            # Callers opt into this deliberately.
            conversation, evaluation = await self._converse(
                agent=agent,
                scenario=scenario,
                session=self.db,
                user_id=user_id,
                workspace_id=workspace_id,
            )
            return conversation, evaluation, await capture_crm_state(self.db, user_id), None

        async with fixture_scope(
            self.engine,
            user_id=user_id,
            spec=scenario.fixture,
        ) as scoped:
            conversation, evaluation = await self._converse(
                agent=agent,
                scenario=scenario,
                session=scoped.session,
                user_id=user_id,
                workspace_id=workspace_id,
            )
            final_db_state = await capture_crm_state(scoped.session, user_id)

        return conversation, evaluation, final_db_state, scoped.ledger

    async def _converse(
        self,
        *,
        agent: Agent,
        scenario: TestScenario,
        session: AsyncSession,
        user_id: int,
        workspace_id: uuid.UUID | None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Bind the agent's tools to one session, talk to it, then judge it."""
        # The agent's real tools, executed against this run's own data. No
        # mocked tool responses: a scenario that says an appointment should
        # exist is checked against the database, not against a transcript.
        bound_tools = await bind_agent_tools(
            db=session,
            agent=agent,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        try:
            conversation = await self._simulate_conversation(
                agent=agent,
                scenario=scenario,
                bound_tools=bound_tools,
            )
        finally:
            await bound_tools.close()

        evaluation = await self._evaluate_conversation(
            agent=agent,
            scenario=scenario,
            conversation=conversation,
        )
        return conversation, evaluation

    async def _simulate_conversation(
        self,
        agent: Agent,
        scenario: TestScenario,
        bound_tools: BoundTools | None = None,
    ) -> list[dict[str, Any]]:
        """Simulate a conversation using the scenario's conversation flow.

        Uses Claude to generate agent responses from the agent's system prompt,
        with the agent's real tools bound. When the model calls a tool it is
        executed for real against the test's own database, so the state left
        behind is the agent's, not a fixture's.

        Args:
            agent: The agent being tested
            scenario: The test scenario
            bound_tools: The agent's executable tools. None runs text-only,
                which is still valid - the tool metrics then report themselves
                unmeasurable rather than failing.

        Returns:
            List of conversation turns with actual agent responses
        """
        client = await self._get_client()
        conversation: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = []

        for turn in scenario.conversation_flow:
            if turn.get("speaker") != "user":
                continue

            user_message = turn["message"]
            messages.append({"role": "user", "content": user_message})
            conversation.append(
                {
                    "speaker": "user",
                    "message": user_message,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )

            started = time.monotonic()
            text, tool_calls = await self._agent_turn(
                client=client,
                agent=agent,
                messages=messages,
                bound_tools=bound_tools,
            )
            conversation.append(
                {
                    "speaker": "agent",
                    "message": text,
                    "tool_calls": tool_calls,
                    # Wall clock for the whole turn, tool execution included,
                    # because that is what a caller actually waits through.
                    "response_ms": (time.monotonic() - started) * 1000.0,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )

        return conversation

    async def _agent_turn(
        self,
        client: Any,
        agent: Agent,
        messages: list[dict[str, Any]],
        bound_tools: BoundTools | None,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Run one agent turn to completion, executing any tools it calls.

        Appends everything it produces to ``messages`` so the next caller turn
        sees the same history the agent did, tool results included.

        Returns:
            The agent's spoken text and a record of every tool it invoked.
        """
        tools = bound_tools.tools if bound_tools else []
        spoken: list[str] = []
        invocations: list[dict[str, Any]] = []

        for _ in range(MAX_TOOL_ITERATIONS):
            response = await call_claude_with_resilience(
                client=client,
                model=settings.QA_EVALUATION_MODEL,
                max_tokens=1000,
                messages=messages,
                system=agent.system_prompt,
                tools=tools or None,
            )

            blocks = list(response.content)
            spoken.extend(
                str(block.text) for block in blocks if getattr(block, "type", "text") == "text"
            )

            tool_uses = [block for block in blocks if getattr(block, "type", None) == "tool_use"]
            if not tool_uses or bound_tools is None:
                break

            messages.append({"role": "assistant", "content": blocks})
            results: list[dict[str, Any]] = []
            for use in tool_uses:
                record = await bound_tools.execute(use.name, dict(use.input or {}))
                invocations.append(record)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": use.id,
                        "content": _tool_result_text(record["result"]),
                        "is_error": record["outcome"] != "ok",
                    }
                )
            # Tool results go back as a user turn: that is where the Messages
            # API expects them, not as a third role.
            messages.append({"role": "user", "content": results})
        else:
            self.logger.warning("tool_loop_exhausted", agent_id=str(agent.id))

        text = " ".join(part for part in spoken if part).strip()
        messages.append({"role": "assistant", "content": text or "(no response)"})
        return text, invocations

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
