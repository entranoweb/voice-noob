"""How much of what was said actually arrived.

This is the voice-specific failure nothing in a text harness can see. The agent
reasons over what STT *heard*, not over what the caller *said*, so a booking for
"Tuesday" that was transcribed as "tuna day" produces an agent that behaves
perfectly on the wrong input. Every text-driven metric in this codebase would
call that run a pass.

Word error rate is the standard measure and it is worth being precise about what
it counts. WER is edit distance over words divided by the length of the
reference — substitutions, deletions and insertions all count, and it can exceed
1.0 when the transcript is longer than the truth. That is not a bug to clamp
away: a system that hallucinates twenty words onto a five-word utterance is
worse than one that drops all five, and a capped score would hide the
difference.

Reports itself unmeasurable on a text-only run, where intended and transcribed
are the same string by construction and a perfect score would be meaningless.
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

# Above this, the caller is effectively talking to a system that cannot hear
# them. Chosen as a failure threshold rather than a quality target: production
# ASR on clean speech sits far below it, so crossing it means something is
# actually broken - wrong codec, wrong sample rate, wrong language model.
DEFAULT_MAX_WER = 0.25


def normalise(text: str) -> list[str]:
    """Words, lowercased, stripped of punctuation that carries no sound.

    Casing and punctuation are decisions made by the transcriber, not things the
    speaker said, so scoring them would report formatting differences as
    mishearings.
    """
    cleaned = "".join(char if char.isalnum() or char.isspace() else " " for char in text.lower())
    return cleaned.split()


def word_error_rate(reference: str, hypothesis: str) -> float | None:
    """Levenshtein distance over words, divided by the reference length.

    Returns None when the reference is empty: there is no denominator, and
    calling that a perfect score would flatter every silent turn.
    """
    ref = normalise(reference)
    hyp = normalise(hypothesis)

    if not ref:
        return None

    # Standard dynamic-programming edit distance, one row at a time. The full
    # matrix is not needed and a long call would make it large.
    previous = list(range(len(hyp) + 1))
    for i, ref_word in enumerate(ref, start=1):
        current = [i]
        for j, hyp_word in enumerate(hyp, start=1):
            cost = 0 if ref_word == hyp_word else 1
            current.append(
                min(
                    previous[j] + 1,  # deletion
                    current[j - 1] + 1,  # insertion
                    previous[j - 1] + cost,  # substitution
                ),
            )
        previous = current

    return previous[-1] / len(ref)


@register
class TranscriptionAccuracy(BaseMetric):
    """Word error rate between what was said and what was heard."""

    name = "transcription_accuracy"
    version = "v1"
    category = MetricCategory.EXPERIENCE
    kind = MetricKind.DETERMINISTIC
    unit = "wer"

    def compute(self, context: MetricContext) -> MetricScore:
        if not context.has_audio:
            return self.not_measurable("run had no audio, so nothing was transcribed")

        pairs = [
            (turn.text_intended, turn.text_transcribed)
            for turn in context.turns
            if turn.text_intended and turn.text_transcribed
        ]
        rates = [
            rate
            for intended, transcribed in pairs
            if (rate := word_error_rate(intended or "", transcribed or "")) is not None
        ]

        if not rates:
            return self.not_measurable("no turn carried both intended and transcribed text")

        limit = _as_float(context.success_criteria.get("max_wer")) or DEFAULT_MAX_WER
        # Weighted by nothing: every turn counts once. A long turn is not more
        # important than a short one when the short one was the phone number.
        overall = sum(rates) / len(rates)
        worst = max(rates)

        return self.score(
            overall,
            passed=overall <= limit,
            turns_compared=len(rates),
            worst_turn_wer=worst,
            limit=limit,
        )


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
