"""AI Test Caller Service for simulating conversations with voice agents.

Uses Claude API to simulate user conversations based on test scenario personas.
"""

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import anthropic
import structlog

from app.core.config import settings
from app.models.agent import Agent
from app.models.test_scenario import TestScenario
from app.services.qa.resilience import (
    call_claude_with_resilience,
    get_anthropic_client,
)

logger = structlog.get_logger()

# Prompt for generating user responses based on persona
USER_SIMULATION_PROMPT = """You are simulating a caller in a test scenario for a voice AI agent.

## Your Persona
{persona_description}

## Scenario Context
- Name: {scenario_name}
- Category: {category}
- Goal: {scenario_goal}

## Conversation So Far
{conversation_history}

## Your Task
Generate the next user message based on your persona and the conversation flow.
- Stay in character
- Be realistic and natural
- Follow the scenario goals
- If the agent has addressed your needs, you can indicate satisfaction or end the call

Respond with ONLY the user's next message (no quotes, no "User:" prefix).
"""

# Prompt for checking scenario completion
COMPLETION_CHECK_PROMPT = """Analyze this conversation between a user and an AI agent to determine if the test scenario is complete.

## Scenario Information
- Name: {scenario_name}
- Expected Behaviors: {expected_behaviors}
- Success Criteria: {success_criteria}
- Max Turns: {max_turns}

## Conversation
{conversation}

## Current Turn Count: {turn_count}

Determine:
1. Is the scenario complete? (user goal achieved or failed)
2. Has the agent exhibited the expected behaviors?
3. Are there any issues?

Respond with JSON (no markdown):
{{
    "is_complete": true/false,
    "completion_reason": "success" | "failure" | "timeout" | "error",
    "behaviors_observed": ["behavior1", "behavior2"],
    "issues_found": ["issue1"] or [],
    "notes": "Any relevant notes"
}}
"""


@dataclass
class ConversationTurn:
    """A single turn in the conversation."""

    speaker: str  # "user" or "agent"
    message: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class TestResult:
    """Result of executing a test scenario."""

    scenario_id: str
    scenario_name: str
    passed: bool
    completion_reason: str
    conversation: list[ConversationTurn]
    behaviors_observed: list[str]
    issues_found: list[str]
    duration_ms: int
    turn_count: int
    notes: str = ""


class AITestCaller:
    """Simulates a caller for testing voice agents."""

    def __init__(self, scenario: TestScenario, agent: Agent):
        """Initialize the test caller.

        Args:
            scenario: Test scenario to execute
            agent: Agent to test
        """
        self.scenario = scenario
        self.agent = agent
        self.conversation: list[ConversationTurn] = []
        self.turn_count = 0
        self.max_turns = 20  # Default max turns
        self.logger = logger.bind(
            scenario_id=str(scenario.id),
            agent_id=str(agent.id),
            component="ai_test_caller",
        )
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

    async def execute_scenario(self) -> TestResult:
        """Execute the full test scenario.

        Returns:
            TestResult with conversation and evaluation
        """
        self.logger.info("starting_scenario_execution")
        start_time = time.monotonic()

        try:
            # Get initial message from scenario flow
            initial_message = self._get_initial_message()
            if initial_message:
                self.conversation.append(ConversationTurn(speaker="user", message=initial_message))
                self.turn_count += 1

            # Main conversation loop
            while self.turn_count < self.max_turns:
                # Get agent response
                agent_response = await self._get_agent_response()
                self.conversation.append(ConversationTurn(speaker="agent", message=agent_response))

                # Check if scenario is complete
                completion = await self._check_completion()
                if completion["is_complete"]:
                    duration_ms = int((time.monotonic() - start_time) * 1000)
                    return TestResult(
                        scenario_id=str(self.scenario.id),
                        scenario_name=self.scenario.name,
                        passed=completion["completion_reason"] == "success",
                        completion_reason=completion["completion_reason"],
                        conversation=self.conversation,
                        behaviors_observed=completion.get("behaviors_observed", []),
                        issues_found=completion.get("issues_found", []),
                        duration_ms=duration_ms,
                        turn_count=self.turn_count,
                        notes=completion.get("notes", ""),
                    )

                # Generate next user message
                user_message = await self._generate_next_message()
                self.conversation.append(ConversationTurn(speaker="user", message=user_message))
                self.turn_count += 1

            # Timeout - exceeded max turns
            duration_ms = int((time.monotonic() - start_time) * 1000)
            return TestResult(
                scenario_id=str(self.scenario.id),
                scenario_name=self.scenario.name,
                passed=False,
                completion_reason="timeout",
                conversation=self.conversation,
                behaviors_observed=[],
                issues_found=["Exceeded maximum turn count"],
                duration_ms=duration_ms,
                turn_count=self.turn_count,
                notes="Scenario did not complete within turn limit",
            )

        except Exception as e:
            self.logger.exception("scenario_execution_failed")
            duration_ms = int((time.monotonic() - start_time) * 1000)
            return TestResult(
                scenario_id=str(self.scenario.id),
                scenario_name=self.scenario.name,
                passed=False,
                completion_reason="error",
                conversation=self.conversation,
                behaviors_observed=[],
                issues_found=[str(e)],
                duration_ms=duration_ms,
                turn_count=self.turn_count,
                notes=f"Error during execution: {e}",
            )

    def _get_initial_message(self) -> str | None:
        """Get initial message from scenario conversation flow."""
        if self.scenario.conversation_flow:
            for turn in self.scenario.conversation_flow:
                if turn.get("speaker") == "user":
                    msg = turn.get("message", "")
                    return str(msg) if msg else None
        return None

    async def _get_agent_response(self) -> str:
        """Get agent response using Claude with agent's system prompt.

        Returns:
            Agent's response message
        """
        client = await self._get_client()

        # Build message history
        messages = []
        for turn in self.conversation:
            role = "user" if turn.speaker == "user" else "assistant"
            messages.append({"role": role, "content": turn.message})

        response = await call_claude_with_resilience(
            client=client,
            model=settings.QA_EVALUATION_MODEL,
            max_tokens=500,
            messages=messages,
            system=self.agent.system_prompt,
        )

        return str(response.content[0].text)

    async def _generate_next_message(self) -> str:
        """Generate next user message based on persona.

        Returns:
            Next user message
        """
        client = await self._get_client()

        # Format conversation history
        history = "\n".join(
            f"[{turn.speaker.upper()}]: {turn.message}" for turn in self.conversation
        )

        # Get persona description
        persona = self.scenario.caller_persona or {}
        persona_desc = f"""
        - Name: {persona.get("name", "Test Caller")}
        - Mood: {persona.get("mood", "neutral")}
        - Communication Style: {persona.get("communication_style", "standard")}
        - Background: {persona.get("background", "General caller")}
        """

        prompt = USER_SIMULATION_PROMPT.format(
            persona_description=persona_desc,
            scenario_name=self.scenario.name,
            category=self.scenario.category,
            scenario_goal=self.scenario.description or "Complete the interaction",
            conversation_history=history,
        )

        response = await call_claude_with_resilience(
            client=client,
            model=settings.QA_EVALUATION_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )

        return str(response.content[0].text).strip()

    async def _check_completion(self) -> dict[str, Any]:
        """Check if the scenario is complete.

        Returns:
            Completion status dict
        """
        import json
        import re

        client = await self._get_client()

        # Format conversation
        conv_text = "\n".join(
            f"[{turn.speaker.upper()}]: {turn.message}" for turn in self.conversation
        )

        prompt = COMPLETION_CHECK_PROMPT.format(
            scenario_name=self.scenario.name,
            expected_behaviors=", ".join(self.scenario.expected_behaviors or []),
            success_criteria=json.dumps(self.scenario.success_criteria or {}),
            max_turns=self.max_turns,
            conversation=conv_text,
            turn_count=self.turn_count,
        )

        response = await call_claude_with_resilience(
            client=client,
            model=settings.QA_EVALUATION_MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )

        response_text = response.content[0].text

        # Parse JSON response
        try:
            result = json.loads(response_text)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

        # Try to extract JSON
        json_match = re.search(r"\{[\s\S]*\}", response_text)
        if json_match:
            try:
                result = json.loads(json_match.group(0))
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass

        # Return default when parsing fails
        return {
            "is_complete": False,
            "completion_reason": "unknown",
            "behaviors_observed": [],
            "issues_found": [],
            "notes": "Could not parse completion check response",
        }

    def compile_results(self) -> dict[str, Any]:
        """Compile test results into a dictionary.

        Returns:
            Results dictionary
        """
        return {
            "scenario_id": str(self.scenario.id),
            "scenario_name": self.scenario.name,
            "agent_id": str(self.agent.id),
            "turn_count": self.turn_count,
            "conversation": [
                {
                    "speaker": turn.speaker,
                    "message": turn.message,
                    "timestamp": turn.timestamp.isoformat(),
                }
                for turn in self.conversation
            ],
        }
