"""Write a voice-agent test the way you would write any other test.

The lesson from every open-source eval tool that found an audience is that the
familiar interface beats the novel dashboard: DeepEval's whole pitch is "pytest,
but for LLMs." This module is that surface for voice agents.

    async def test_books_an_appointment(runner, agent, user):
        result = await runner.check(
            agent=agent,
            user_id=user.id,
            says=["Hi, I'm Jane on 5551234567. Can I book for tomorrow?"],
            invokes=["book_appointment"],
            leaves={"appointments": [{"status": "scheduled"}]},
        )
        assert result

No scenario row, no seeding step, no run record — a scenario is an argument, not
a database object. The run is isolated and rolled back by default, and the judge
is off, because deterministic assertions decide the verdict and a CI run should
not pay for a model call to narrate a conclusion the assertions already reached.

What this module is really for is the failure message. ``assert result`` on a
bare boolean tells you a voice agent did something wrong somewhere, which is
worthless at 2am. ``RunResult.__bool__`` is paired with an ``explain()`` that
names the metric, the expectation and the actual state, and pytest shows it
because the object is falsey and has a ``__repr__``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.models.test_scenario import TestScenario
from app.monitoring.call_trace import TerminationReason
from app.services.qa.metrics.context import build_context
from app.services.qa.metrics.runner import MetricResults, RunOutcome, evaluate
from app.services.qa.mutations import (
    Comparison,
    Mutation,
    VariantResult,
    apply_mutation,
    named_variants,
)
from app.services.qa.test_runner import TestRunner, _tool_calls_from_conversation

if TYPE_CHECKING:
    from app.models.agent import Agent

# What a scenario needs when nobody wrote one down. Kept here rather than
# defaulted in the signature so the values are visible to a reader.
_DEFAULT_PERSONA: dict[str, Any] = {"name": "Test caller"}


@dataclass(frozen=True)
class ScenarioSpec:
    """A scenario as an argument rather than a database row."""

    says: tuple[str, ...]
    invokes: tuple[str, ...] = ()
    leaves: dict[str, Any] | None = None
    given: dict[str, Any] | None = None
    persona: dict[str, Any] = field(default_factory=lambda: dict(_DEFAULT_PERSONA))
    name: str = "inline scenario"
    max_response_ms: float | None = None

    def to_scenario(self) -> TestScenario:
        """A transient TestScenario the runner can execute.

        Never added to a session: the point is to run without persisting
        anything, so a test suite does not accumulate scenario rows.
        """
        criteria: dict[str, Any] = {}
        if self.leaves is not None:
            criteria["expected_db_state"] = self.leaves
        if self.invokes:
            criteria["must_invoke_tools"] = list(self.invokes)
        if self.max_response_ms is not None:
            criteria["max_response_ms"] = self.max_response_ms

        return TestScenario(
            id=uuid.uuid4(),
            name=self.name,
            description="Defined inline by a test",
            category="inline",
            difficulty="medium",
            caller_persona=dict(self.persona),
            conversation_flow=[{"speaker": "user", "message": text} for text in self.says],
            expected_behaviors=[],
            expected_tool_calls=[{"tool": name} for name in self.invokes] or None,
            success_criteria=criteria,
            fixture=self.given,
            is_active=True,
            is_built_in=False,
        )


@dataclass(frozen=True)
class RunResult:
    """Everything one run produced, and why it passed or failed."""

    outcome: RunOutcome
    metrics: MetricResults
    transcript: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    final_state: dict[str, Any]
    ledger: dict[str, Any] | None
    judgement: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        """True only for a trustworthy run that passed.

        An ERROR is deliberately falsey here even though it is not the agent's
        fault: a test that cannot measure anything must not report success.
        ``explain()`` says which of the two happened.
        """
        return self.outcome is RunOutcome.PASSED

    @property
    def passed(self) -> bool:
        return bool(self)

    @property
    def errored(self) -> bool:
        """The run could not be measured — a harness problem, not an agent one."""
        return self.outcome is RunOutcome.ERROR

    def tools_invoked(self) -> list[str]:
        return [str(call.get("name")) for call in self.tool_calls if call.get("name")]

    def said(self) -> list[str]:
        return [
            str(turn.get("message", ""))
            for turn in self.transcript
            if turn.get("speaker") == "agent"
        ]

    def explain(self) -> str:
        """A failure message someone can act on without opening the database."""
        if self:
            return f"{self.outcome}: all assertions met"

        lines = [f"{self.outcome}"]
        if self.errored:
            lines.append("  the run could not be measured, so this is not an agent failure:")
            lines.extend(f"    - {reason}" for reason in self.metrics.invalid_reasons)

        for score in self.metrics.scores:
            if score.passed is not False:
                continue
            lines.append(f"  {score.metric} failed")
            for key, value in (score.detail or {}).items():
                lines.append(f"    {key}: {value!r}")

        lines.append(f"  tools invoked: {self.tools_invoked() or 'none'}")
        for utterance in self.said():
            lines.append(f"  agent said: {utterance[:200]!r}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        # pytest prints the repr of a falsey object, so this is where the
        # explanation actually reaches whoever is reading the failure.
        return self.explain()


class ScenarioChecker:
    """Runs inline scenarios. The object a pytest fixture hands to a test."""

    def __init__(self, runner: TestRunner) -> None:
        self._runner = runner

    async def run(
        self,
        *,
        agent: Agent,
        user_id: int,
        spec: ScenarioSpec,
        workspace_id: uuid.UUID | None = None,
        isolated: bool = True,
        judge: bool = False,
    ) -> RunResult:
        """Execute one inline scenario and score it."""
        scenario = spec.to_scenario()
        conversation, judgement, final_state, ledger = await self._runner._execute(  # noqa: SLF001
            agent=agent,
            scenario=scenario,
            user_id=user_id,
            workspace_id=workspace_id,
            isolated=isolated,
            judge=judge,
        )

        tool_calls = _tool_calls_from_conversation(conversation)
        metrics = evaluate(
            build_context(
                run_id=str(scenario.id),
                conversation=conversation,
                tool_calls=tool_calls,
                expected_tool_calls=scenario.expected_tool_calls,
                success_criteria=scenario.success_criteria,
                termination_reason=TerminationReason.AGENT_ENDED,
                final_db_state=final_state,
                fixture_ledger=ledger.as_dict() if ledger else None,
            ),
        )

        return RunResult(
            outcome=metrics.outcome,
            metrics=metrics,
            transcript=conversation,
            tool_calls=tool_calls,
            final_state=final_state,
            ledger=ledger.as_dict() if ledger else None,
            judgement=judgement,
        )

    async def compare(
        self,
        *,
        agent: Agent,
        user_id: int,
        spec: ScenarioSpec,
        variants: dict[str, dict[str, Any]] | list[Mutation],
        repeats: int = 5,
        workspace_id: uuid.UUID | None = None,
        judge: bool = False,
    ) -> Comparison:
        """Run one scenario against the agent and each variant, several times.

        ``repeats`` defaults to 5 rather than 1 because a single run per variant
        measures the model's variance and reports it as a difference between
        configurations. Five is not many - the comparison will often come back
        inconclusive - but an inconclusive answer is the true one at that sample
        size, and saying so is the point.

        Errored runs still count toward ``repeats`` but not toward the pass rate:
        a broken harness says nothing about the configuration either way.
        """
        mutations = variants if isinstance(variants, list) else named_variants(variants)

        base = VariantResult(name="base")
        for _ in range(repeats):
            base.runs.append(
                await self.run(
                    agent=agent,
                    user_id=user_id,
                    spec=spec,
                    workspace_id=workspace_id,
                    judge=judge,
                ),
            )

        results: list[VariantResult] = []
        for mutation in mutations:
            mutated = apply_mutation(agent, mutation)
            variant = VariantResult(name=mutation.name)
            for _ in range(repeats):
                variant.runs.append(
                    await self.run(
                        agent=mutated,
                        user_id=user_id,
                        spec=spec,
                        workspace_id=workspace_id,
                        judge=judge,
                    ),
                )
            results.append(variant)

        return Comparison(base=base, variants=results)

    async def check(
        self,
        *,
        agent: Agent,
        user_id: int,
        says: list[str] | tuple[str, ...],
        invokes: list[str] | tuple[str, ...] = (),
        leaves: dict[str, Any] | None = None,
        given: dict[str, Any] | None = None,
        persona: dict[str, Any] | None = None,
        max_response_ms: float | None = None,
        workspace_id: uuid.UUID | None = None,
        isolated: bool = True,
        judge: bool = False,
    ) -> RunResult:
        """Define and run a scenario in one call — the common case."""
        spec = ScenarioSpec(
            says=tuple(says),
            invokes=tuple(invokes),
            leaves=leaves,
            given=given,
            persona=persona if persona is not None else dict(_DEFAULT_PERSONA),
            max_response_ms=max_response_ms,
        )
        return await self.run(
            agent=agent,
            user_id=user_id,
            spec=spec,
            workspace_id=workspace_id,
            isolated=isolated,
            judge=judge,
        )


def checker(runner: TestRunner) -> ScenarioChecker:
    """Wrap a runner for use from a test."""
    return ScenarioChecker(runner)


__all__ = [
    "RunResult",
    "ScenarioChecker",
    "ScenarioSpec",
    "checker",
]
