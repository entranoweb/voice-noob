"""Whether the call felt like a conversation or like a fight over the line.

Latency to *anything* is what a caller experiences, not latency to a complete
answer, so time-to-first-audio-byte is the number that matters and it is not the
same as `response_speed`, which measures the whole turn. An agent that starts
speaking in 400ms and finishes in 4s feels responsive. One that thinks silently
for 3s and then delivers the same sentence in 1s does not, and both have
identical turn latency.

Barge-in is the other half. A caller interrupting is normal and an agent that
handles it is good; the failure is an agent that keeps talking over someone who
has started speaking. Those are different events and this module keeps them
apart: `barge_in` is the caller starting to speak, `interrupted` is a turn that
was cut off.

All three report unmeasurable on a text-only run rather than scoring a perfect
zero, because a conversation with no audio has no turn-taking to get wrong.
"""

from __future__ import annotations

from app.services.qa.metrics.base import (
    BaseMetric,
    MetricCategory,
    MetricContext,
    MetricKind,
    MetricScore,
)
from app.services.qa.metrics.diagnostic.response_speed import percentile
from app.services.qa.metrics.registry import register

# Roughly where a pause stops reading as thinking and starts reading as a dead
# line. Callers begin repeating themselves shortly after this.
DEFAULT_MAX_TTFB_MS = 1200.0

# Some talk-over is human. A quarter of the agent's turns being cut off is a
# turn-detection problem, not a conversational style.
DEFAULT_MAX_INTERRUPTION_RATE = 0.25


@register
class TimeToFirstAudio(BaseMetric):
    """How long the caller waits before hearing anything at all."""

    name = "time_to_first_audio"
    version = "v1"
    category = MetricCategory.EXPERIENCE
    kind = MetricKind.DETERMINISTIC
    unit = "ms"

    def compute(self, context: MetricContext) -> MetricScore:
        if not context.has_audio:
            return self.not_measurable("run had no audio")

        samples = [turn.ttfb_ms for turn in context.agent_turns() if turn.ttfb_ms is not None]
        if not samples:
            return self.not_measurable("no agent turn recorded a time to first audio byte")

        limit = _as_float(context.success_criteria.get("max_ttfb_ms")) or DEFAULT_MAX_TTFB_MS
        # p95 rather than the mean: the caller remembers the worst pause, not
        # the average one, and a mean hides a single ten-second silence inside
        # twenty fast turns.
        p95 = percentile(samples, 95)

        return self.score(
            p95,
            passed=p95 <= limit,
            p50_ms=percentile(samples, 50),
            p95_ms=p95,
            max_ms=max(samples),
            turns_measured=len(samples),
            limit_ms=limit,
        )


@register
class InterruptionHandling(BaseMetric):
    """How often the agent was talked over, and whether it stopped.

    A caller interrupting is normal. The failure this catches is the agent
    continuing through it — which on a real line means both parties talking and
    neither being understood.
    """

    name = "interruption_handling"
    version = "v1"
    category = MetricCategory.EXPERIENCE
    kind = MetricKind.DETERMINISTIC
    unit = "rate"

    def compute(self, context: MetricContext) -> MetricScore:
        if not context.has_audio:
            return self.not_measurable("run had no audio")

        agent_turns = context.agent_turns()
        if not agent_turns:
            return self.not_measurable("no agent turns to measure")

        barge_ins = sum(1 for turn in agent_turns if turn.barge_in)
        if not barge_ins:
            # Nothing interrupted the agent, so nothing was mishandled. That is
            # a real result rather than an absent one, unlike the cases above.
            return self.score(
                0.0,
                passed=True,
                barge_ins=0,
                talked_over=0,
                agent_turns=len(agent_turns),
            )

        # A turn the caller barged into that did *not* end early is a turn the
        # agent talked through.
        talked_over = sum(1 for turn in agent_turns if turn.barge_in and not turn.interrupted)
        rate = talked_over / barge_ins
        limit = (
            _as_float(context.success_criteria.get("max_interruption_rate"))
            or DEFAULT_MAX_INTERRUPTION_RATE
        )

        return self.score(
            rate,
            passed=rate <= limit,
            barge_ins=barge_ins,
            talked_over=talked_over,
            agent_turns=len(agent_turns),
            limit=limit,
        )


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
