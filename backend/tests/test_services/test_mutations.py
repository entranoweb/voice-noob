"""Tests for A/B comparison between agent configurations.

The behaviour that matters here is refusal: a comparison must not name a winner
when the difference is indistinguishable from run-to-run variation. A voice
agent is stochastic, so tooling that shows two numbers side by side invites you
to read noise as a finding.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.models.agent import Agent
from app.services.qa.metrics.runner import RunOutcome
from app.services.qa.mutations import (
    Comparison,
    Mutation,
    MutationError,
    VariantResult,
    apply_mutation,
    deep_merge,
    wilson_interval,
)
from app.services.qa.testing import RunResult


def _run(outcome: RunOutcome) -> RunResult:
    return RunResult(
        outcome=outcome,
        metrics=type("M", (), {"scores": (), "invalid_reasons": ()})(),  # type: ignore[arg-type]
        transcript=[],
        tool_calls=[],
        final_state={},
        ledger=None,
    )


def _variant(name: str, passed: int, failed: int, errored: int = 0) -> VariantResult:
    variant = VariantResult(name=name)
    variant.runs = (
        [_run(RunOutcome.PASSED)] * passed
        + [_run(RunOutcome.FAILED)] * failed
        + [_run(RunOutcome.ERROR)] * errored
    )
    return variant


class TestDeepMerge:
    def test_merges_nested_dicts(self) -> None:
        assert deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"b": 9}}) == {"a": {"b": 9, "c": 2}}

    def test_lists_replace_rather_than_concatenate(self) -> None:
        """Appending would make removing a tool impossible to express."""
        assert deep_merge({"tools": ["crm", "sms"]}, {"tools": ["crm"]}) == {"tools": ["crm"]}

    def test_override_wins_on_scalars(self) -> None:
        assert deep_merge(0.7, 0.2) == 0.2

    def test_new_keys_are_added(self) -> None:
        assert deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}


class TestMutation:
    def test_rejects_a_field_it_may_not_change(self) -> None:
        """A mutation is a behaviour experiment, not a way to reassign an agent
        to another user mid-comparison."""
        with pytest.raises(MutationError, match="user_id"):
            Mutation(name="sneaky", overrides={"user_id": 99})

    def test_names_the_mutable_fields_in_the_error(self) -> None:
        with pytest.raises(MutationError, match="system_prompt"):
            Mutation(name="sneaky", overrides={"is_published": True})

    def test_accepts_a_prompt_override(self) -> None:
        assert Mutation(name="terse", overrides={"system_prompt": "Be brief."}).name == "terse"


class TestApplyMutation:
    def _agent(self, **kwargs: Any) -> Agent:
        base: dict[str, Any] = {
            "user_id": 1,
            "name": "Booking agent",
            "pricing_tier": "balanced",
            "system_prompt": "You book appointments.",
            "temperature": 0.7,
            "enabled_tools": ["crm", "bookings"],
            "provider_config": {"stt": {"model": "nova-2"}, "tts": {"voice": "amy"}},
        }
        base.update(kwargs)
        return Agent(**base)

    def test_overrides_the_prompt(self) -> None:
        agent = self._agent()
        mutated = apply_mutation(agent, Mutation(name="terse", overrides={"system_prompt": "Hi."}))
        assert mutated.system_prompt == "Hi."

    def test_leaves_the_original_untouched(self) -> None:
        """The base configuration is the control; mutating it in place would
        make every later comparison test the wrong thing."""
        agent = self._agent()
        apply_mutation(agent, Mutation(name="terse", overrides={"system_prompt": "Hi."}))
        assert agent.system_prompt == "You book appointments."

    def test_nested_provider_config_merges_rather_than_replaces(self) -> None:
        agent = self._agent()
        mutated = apply_mutation(
            agent,
            Mutation(name="fast-stt", overrides={"provider_config": {"stt": {"model": "nova-3"}}}),
        )
        assert mutated.provider_config == {
            "stt": {"model": "nova-3"},
            "tts": {"voice": "amy"},
        }

    def test_mutating_a_nested_dict_does_not_touch_the_original(self) -> None:
        agent = self._agent()
        apply_mutation(
            agent,
            Mutation(name="fast-stt", overrides={"provider_config": {"stt": {"model": "nova-3"}}}),
        )
        assert agent.provider_config["stt"]["model"] == "nova-2"

    def test_keeps_the_agent_id(self) -> None:
        """Logs and traces still have to point at the agent under test."""
        agent = self._agent()
        mutated = apply_mutation(agent, Mutation(name="terse", overrides={"system_prompt": "Hi."}))
        assert mutated.id == agent.id


class TestWilsonInterval:
    def test_no_trials_means_no_information(self) -> None:
        assert wilson_interval(0, 0) == (0.0, 1.0)

    def test_bounds_stay_inside_zero_and_one(self) -> None:
        """The normal approximation happily returns a lower bound below zero for
        a variant that passed every run. This is why Wilson."""
        low, high = wilson_interval(5, 5)
        assert 0.0 <= low <= high <= 1.0

    def test_a_perfect_small_sample_is_still_uncertain(self) -> None:
        low, _high = wilson_interval(5, 5)
        assert low < 1.0

    def test_more_samples_narrow_the_interval(self) -> None:
        narrow = wilson_interval(80, 100)
        wide = wilson_interval(8, 10)
        assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])


class TestComparison:
    def test_refuses_a_winner_when_intervals_overlap(self) -> None:
        """The flagship behaviour. 3/5 against 4/5 looks like an improvement and
        is nothing of the sort."""
        comparison = Comparison(base=_variant("base", 3, 2), variants=[_variant("v1", 4, 1)])

        assert comparison.winner() is None
        assert comparison.conclusive() is False
        assert "inconclusive" in comparison.explain()

    def test_names_a_winner_when_the_evidence_separates_them(self) -> None:
        comparison = Comparison(
            base=_variant("base", 2, 98),
            variants=[_variant("v1", 97, 3)],
        )

        winner = comparison.winner()
        assert winner is not None
        assert winner.name == "v1"
        assert "v1 wins" in comparison.explain()

    def test_the_base_can_win(self) -> None:
        """A regression has to be reportable, not just an improvement."""
        comparison = Comparison(
            base=_variant("base", 97, 3),
            variants=[_variant("v1", 2, 98)],
        )
        winner = comparison.winner()
        assert winner is not None
        assert winner.name == "base"

    def test_errored_runs_do_not_count_against_a_variant(self) -> None:
        """An outage must not decide a prompt comparison."""
        variant = _variant("v1", 5, 0, errored=3)
        assert variant.trials == 5
        assert variant.errors == 3
        assert variant.pass_rate == 1.0

    def test_a_variant_with_no_measurable_run_reports_so(self) -> None:
        variant = _variant("v1", 0, 0, errored=4)
        assert variant.pass_rate is None
        assert "no measurable runs" in variant.summary()

    def test_inconclusive_when_only_one_variant_was_measurable(self) -> None:
        comparison = Comparison(
            base=_variant("base", 5, 0),
            variants=[_variant("v1", 0, 0, errored=5)],
        )
        assert comparison.winner() is None
        assert "fewer than two variants" in comparison.explain()

    def test_the_explanation_carries_the_numbers(self) -> None:
        comparison = Comparison(base=_variant("base", 3, 2), variants=[_variant("v1", 4, 1)])
        text = comparison.explain()
        assert "3/5 passed" in text
        assert "4/5 passed" in text
        assert "95% CI" in text

    def test_repr_is_the_explanation(self) -> None:
        comparison = Comparison(base=_variant("base", 3, 2), variants=[_variant("v1", 4, 1)])
        assert repr(comparison) == comparison.explain()
