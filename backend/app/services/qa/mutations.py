"""Compare two agent configurations on the same scenario, honestly.

The question a voice developer asks every day is whether a prompt edit made
things better or worse. A mutation is that edit expressed as a deep-merged
override, so the two configurations differ by exactly what you changed and
nothing else.

The hard part is not applying the override, it is not lying about the result.
A voice agent is stochastic: run the same scenario twice and it can pass once
and fail once, with no change to the prompt at all. Comparing one run against
one run therefore measures noise and calls it a finding, which is worse than
not measuring — it manufactures confidence.

So a comparison here runs each variant ``repeats`` times, reports a pass rate
with a Wilson score interval, and declares a winner only when the intervals do
not overlap. When they do, it says the run was inconclusive and how many more
samples would be needed to separate them, rather than pointing at the higher
number. Two numbers side by side is how A/B tooling invites you to fool
yourself.

Wilson rather than the normal approximation because the counts here are small
and the rates sit near 0 or 1, exactly where the normal interval breaks — it
happily produces bounds below zero for a variant that passed every run.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.models.agent import Agent

if TYPE_CHECKING:
    from app.services.qa.testing import RunResult

# Fields a mutation may override. Restricted on purpose: a mutation is a
# behaviour experiment, not a way to reassign an agent to another user or
# silently point it at a different phone number mid-comparison.
MUTABLE_FIELDS = frozenset(
    {
        "system_prompt",
        "initial_greeting",
        "temperature",
        "max_tokens",
        "voice",
        "language",
        "turn_detection_mode",
        "turn_detection_threshold",
        "turn_detection_prefix_padding_ms",
        "turn_detection_silence_duration_ms",
        "enabled_tools",
        "enabled_tool_ids",
        "provider_config",
    },
)

# 95% two-sided.
Z_95 = 1.959963984540054

# A comparison needs a base and at least one variant to compare it against.
MIN_VARIANTS = 2


class MutationError(ValueError):
    """Raised when a mutation names something it may not change."""


@dataclass(frozen=True)
class Mutation:
    """One named variant of an agent's configuration."""

    name: str
    overrides: dict[str, Any]
    description: str | None = None

    def __post_init__(self) -> None:
        unknown = set(self.overrides) - MUTABLE_FIELDS
        if unknown:
            raise MutationError(
                f"mutation {self.name!r} cannot change {sorted(unknown)}; "
                f"mutable fields are {sorted(MUTABLE_FIELDS)}",
            )


def deep_merge(base: Any, override: Any) -> Any:
    """Merge ``override`` onto ``base``, recursing into dicts.

    Lists replace rather than concatenate. A mutation that sets
    ``enabled_tools`` means *these tools*, not *these as well as the existing
    ones* — appending would make it impossible to express removing one.
    """
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = deep_merge(merged.get(key), value) if key in merged else value
        return merged
    return override


def apply_mutation(agent: Agent, mutation: Mutation) -> Agent:
    """A transient Agent carrying the mutation's overrides.

    Never added to a session: a variant exists for the length of a comparison
    and must not become a row someone later has to clean up. The id is
    preserved so logs and traces still point at the agent under test.
    """
    fields: dict[str, Any] = {
        "id": agent.id,
        "user_id": agent.user_id,
        "name": agent.name,
        "pricing_tier": agent.pricing_tier,
    }
    for name in MUTABLE_FIELDS:
        value = getattr(agent, name, None)
        if value is not None:
            fields[name] = copy.deepcopy(value)

    for name, value in mutation.overrides.items():
        fields[name] = deep_merge(fields.get(name), value)

    return Agent(**fields)


def wilson_interval(successes: int, trials: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for a pass rate.

    Returns (0.0, 1.0) for zero trials: no information is the honest answer,
    not a point estimate.
    """
    if trials <= 0:
        return (0.0, 1.0)

    p = successes / trials
    denominator = 1 + z**2 / trials
    centre = (p + z**2 / (2 * trials)) / denominator
    margin = (z / denominator) * math.sqrt(p * (1 - p) / trials + z**2 / (4 * trials**2))
    return (max(0.0, centre - margin), min(1.0, centre + margin))


@dataclass
class VariantResult:
    """How one configuration did over repeated runs."""

    name: str
    runs: list[RunResult] = field(default_factory=list)

    @property
    def trials(self) -> int:
        """Runs that produced a measurable verdict.

        Errored runs are excluded rather than counted as failures: a harness
        that broke says nothing about the configuration, and counting it against
        the variant would let an outage decide a prompt comparison.
        """
        return sum(1 for run in self.runs if not run.errored)

    @property
    def errors(self) -> int:
        return sum(1 for run in self.runs if run.errored)

    @property
    def passes(self) -> int:
        return sum(1 for run in self.runs if run.passed)

    @property
    def pass_rate(self) -> float | None:
        return self.passes / self.trials if self.trials else None

    @property
    def interval(self) -> tuple[float, float]:
        return wilson_interval(self.passes, self.trials)

    def summary(self) -> str:
        if not self.trials:
            return f"{self.name}: no measurable runs ({self.errors} errored)"
        low, high = self.interval
        rate = self.pass_rate or 0.0
        errors = f", {self.errors} errored" if self.errors else ""
        return (
            f"{self.name}: {self.passes}/{self.trials} passed "
            f"({rate:.0%}, 95% CI {low:.0%}-{high:.0%}){errors}"
        )


@dataclass
class Comparison:
    """A base configuration against one or more mutations."""

    base: VariantResult
    variants: list[VariantResult] = field(default_factory=list)

    def all_variants(self) -> list[VariantResult]:
        return [self.base, *self.variants]

    def conclusive(self) -> bool:
        """True when some variant's interval clears the base's entirely."""
        return self.winner() is not None

    def winner(self) -> VariantResult | None:
        """The best variant, only if the evidence separates it from the base.

        Returns None when the intervals overlap. That is the common outcome at
        realistic sample sizes and it is the correct answer: pointing at the
        higher of two indistinguishable numbers is how a comparison becomes
        superstition.
        """
        measurable = [v for v in self.all_variants() if v.trials]
        if len(measurable) < MIN_VARIANTS:
            return None

        best = max(measurable, key=lambda v: v.pass_rate or 0.0)
        others = [v for v in measurable if v is not best]
        best_low = best.interval[0]
        if all(best_low > other.interval[1] for other in others):
            return best
        return None

    def explain(self) -> str:
        """A readable verdict, including when there isn't one."""
        lines = [variant.summary() for variant in self.all_variants()]

        champion = self.winner()
        if champion is not None:
            lines.append(f"→ {champion.name} wins; its interval clears the others")
            return "\n".join(lines)

        measurable = [v for v in self.all_variants() if v.trials]
        if len(measurable) < MIN_VARIANTS:
            lines.append("→ inconclusive: fewer than two variants produced a measurable run")
        else:
            trials = max(v.trials for v in measurable)
            lines.append(
                f"→ inconclusive at {trials} runs each: the intervals overlap, so any "
                f"difference here is indistinguishable from run-to-run variation. "
                f"Roughly {trials * 4} runs would be needed to halve the interval.",
            )
        return "\n".join(lines)

    def __repr__(self) -> str:
        return self.explain()


def named_variants(mutations: dict[str, dict[str, Any]]) -> list[Mutation]:
    """Build mutations from a plain mapping, for the common inline case."""
    return [Mutation(name=name, overrides=overrides) for name, overrides in mutations.items()]


__all__ = [
    "MUTABLE_FIELDS",
    "Comparison",
    "Mutation",
    "MutationError",
    "VariantResult",
    "apply_mutation",
    "deep_merge",
    "named_variants",
    "wilson_interval",
]
