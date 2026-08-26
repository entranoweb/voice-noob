"""Core types for the metric system.

Metrics are pure functions over a ``MetricContext``. They do no IO, touch no
database, and call no external service — everything a metric needs is assembled
into the context first. That is what makes a metric cheap to test, deterministic
to re-run, and computable from a stored trace long after the call ended.

The four categories are ordered, and the order carries meaning:

1. **Validation** — *is this run trustworthy?* Gates that must pass before any
   other number can be interpreted. If the harness misbehaved, the agent was
   graded against a corrupted run and its scores are noise.
2. **Accuracy** — *did the agent do the right thing?* The pass/fail that counts.
3. **Experience** — *was it a good conversation?* Real, but it does not change
   whether the agent succeeded.
4. **Diagnostic** — *why did it fail?* Not scored; exists to make a failure
   actionable rather than merely visible.

Taxonomy adapted from ServiceNow's EVA (MIT), which is worth reading in full.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Protocol

from app.monitoring.call_trace import Speaker, TerminationReason, ToolOutcome


class MetricCategory(StrEnum):
    """Which question a metric answers. See module docstring for the ordering."""

    VALIDATION = "validation"
    ACCURACY = "accuracy"
    EXPERIENCE = "experience"
    DIAGNOSTIC = "diagnostic"


class MetricKind(StrEnum):
    """How a metric arrives at its number.

    Recorded on every score so a consumer can tell which results carry judge
    variance and which are exact. A deterministic metric needs no calibration and
    no repeat sampling; a judged one is meaningless without both.
    """

    DETERMINISTIC = "deterministic"
    JUDGE = "judge"


@dataclass(frozen=True, slots=True)
class TurnData:
    """One conversational turn.

    ``text_intended`` is what the speaker meant to say — for the agent, the text
    handed to TTS. ``text_transcribed`` is what the other side's STT heard. The
    delta between them is the voice-specific signal; when a run is text-only they
    are equal, and metrics that depend on the difference report unavailable
    rather than pretending to measure it.
    """

    index: int
    speaker: Speaker
    text_intended: str | None = None
    text_transcribed: str | None = None
    response_ms: float | None = None
    ttfb_ms: float | None = None
    audio_duration_ms: float | None = None
    interrupted: bool = False
    barge_in: bool = False

    @property
    def text(self) -> str:
        """Best available text for this turn, preferring what was heard."""
        return self.text_transcribed or self.text_intended or ""


@dataclass(frozen=True, slots=True)
class ToolCallData:
    """One tool invocation, with the outcome the runtime actually observed."""

    name: str
    outcome: ToolOutcome
    arguments: dict[str, Any] = field(default_factory=dict)
    duration_ms: float | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class MetricContext:
    """Everything the metrics get to see.

    Assembled once per run from the call trace and the scenario definition. Keep
    it serialisable: a stored context is what lets a new metric be computed
    against old runs without re-running them.
    """

    run_id: str

    # What happened
    turns: tuple[TurnData, ...] = ()
    tool_calls: tuple[ToolCallData, ...] = ()
    termination_reason: TerminationReason = TerminationReason.UNKNOWN
    duration_ms: float | None = None

    # What was supposed to happen. Both come from the scenario definition.
    expected_tool_calls: tuple[dict[str, Any], ...] = ()
    success_criteria: dict[str, Any] = field(default_factory=dict)

    # Database state for deterministic task completion. `expected` is ground
    # truth from the scenario; `final` is a snapshot taken after the run.
    expected_db_state: dict[str, Any] | None = None
    final_db_state: dict[str, Any] | None = None

    # Whether audio was in the loop. Metrics that require it check this rather
    # than inferring from empty fields, so "not measured" never reads as "zero".
    has_audio: bool = False

    def agent_turns(self) -> tuple[TurnData, ...]:
        """Turns spoken by the agent, in order."""
        return tuple(t for t in self.turns if t.speaker == Speaker.AGENT)

    def caller_turns(self) -> tuple[TurnData, ...]:
        """Turns spoken by the caller, in order."""
        return tuple(t for t in self.turns if t.speaker == Speaker.CALLER)


@dataclass(frozen=True, slots=True)
class MetricScore:
    """The result of one metric over one run.

    ``value`` of ``None`` means *not measurable for this run* — audio was absent,
    or the scenario declared no expectation. That is deliberately distinct from a
    value of ``0.0``, which means measured and bad. Collapsing the two is how an
    unmeasured dimension quietly becomes a failing one.
    """

    metric: str
    version: str
    category: MetricCategory
    kind: MetricKind
    value: float | None
    passed: bool | None = None
    unit: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def measured(self) -> bool:
        """Whether this metric produced a number for this run."""
        return self.value is not None


class Metric(Protocol):
    """What every metric implements.

    ``version`` is part of the contract: a scoring change is a new version, and
    scores from different versions are not comparable. Storing it per score is
    what makes a rubric change visible instead of silently shifting a trend line.
    """

    name: ClassVar[str]
    version: ClassVar[str]
    category: ClassVar[MetricCategory]
    kind: ClassVar[MetricKind]

    def compute(self, context: MetricContext) -> MetricScore: ...


class BaseMetric:
    """Convenience base handling the boilerplate of building a score."""

    name: ClassVar[str] = "unnamed"
    version: ClassVar[str] = "v1"
    category: ClassVar[MetricCategory] = MetricCategory.DIAGNOSTIC
    kind: ClassVar[MetricKind] = MetricKind.DETERMINISTIC
    unit: ClassVar[str | None] = None

    def compute(self, context: MetricContext) -> MetricScore:  # pragma: no cover
        raise NotImplementedError

    def score(
        self,
        value: float | None,
        *,
        passed: bool | None = None,
        **detail: Any,
    ) -> MetricScore:
        """Build a score carrying this metric's identity and version."""
        return MetricScore(
            metric=self.name,
            version=self.version,
            category=self.category,
            kind=self.kind,
            value=value,
            passed=passed,
            unit=self.unit,
            detail=detail,
        )

    def not_measurable(self, reason: str, **detail: Any) -> MetricScore:
        """Report that this run cannot support the metric, and why.

        Never returns 0.0 for a missing measurement — see MetricScore.value.
        """
        return self.score(None, passed=None, reason=reason, **detail)
