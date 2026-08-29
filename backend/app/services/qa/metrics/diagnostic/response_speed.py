"""Response latency, as the caller experiences it.

The number that decides whether a call feels alive is the gap between the caller
finishing a sentence and the agent starting to speak. It is not the model's
inference time, and it is not the round-trip to any one vendor — it is the whole
pipeline, measured at the ear.

Reported as p50 and p95 rather than a mean: a conversation with nine snappy
turns and one four-second stall averages fine and feels broken, and the stall is
what a caller remembers.
"""

from __future__ import annotations

from app.services.qa.metrics.base import (
    BaseMetric,
    MetricCategory,
    MetricContext,
    MetricKind,
    MetricScore,
)
from app.services.qa.metrics.registry import register

# Above this, a caller starts to think the line has dropped. Used only to set
# `passed`; the raw percentiles are the number that matters, and a scenario can
# override with response_time_limit_seconds in its success criteria.
DEFAULT_LIMIT_MS = 2000.0


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile over an unsorted list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), round(pct / 100 * len(ordered))))
    return ordered[rank - 1]


@register
class ResponseSpeed(BaseMetric):
    """Caller-perceived latency before the agent replies, p95 in milliseconds."""

    name = "response_speed"
    version = "v1"
    category = MetricCategory.DIAGNOSTIC
    kind = MetricKind.DETERMINISTIC
    unit = "ms"

    def compute(self, context: MetricContext) -> MetricScore:
        latencies = [
            turn.response_ms for turn in context.agent_turns() if turn.response_ms is not None
        ]
        if not latencies:
            return self.not_measurable(
                "no per-turn response timings were recorded for this run",
            )

        limit_seconds = context.success_criteria.get("response_time_limit_seconds")
        limit_ms = float(limit_seconds) * 1000 if limit_seconds else DEFAULT_LIMIT_MS

        p95 = percentile(latencies, 95)

        return self.score(
            p95,
            passed=p95 <= limit_ms,
            p50_ms=round(percentile(latencies, 50), 1),
            p95_ms=round(p95, 1),
            max_ms=round(max(latencies), 1),
            limit_ms=limit_ms,
            turns_measured=len(latencies),
        )
