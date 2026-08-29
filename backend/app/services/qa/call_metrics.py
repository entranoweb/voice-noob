"""Compute the metric suite for a call that actually happened.

Everything else in ``qa/`` measures a *simulated* run: a scripted caller, a
scenario with declared expectations, a database fixture that rolls back. This
module is the other direction — the metrics computed from a real call, after the
fact, from what the media bridge recorded.

Only some metrics can say anything about a real call, and the ones that cannot
must say so rather than guess:

* ``time_to_first_audio`` and ``interruption_handling`` are measurable, because
  the bridge timed them off the live stream.
* ``transcription_accuracy`` is not. Word error rate needs to know what the
  caller *meant* to say, and a real human caller comes with no script. The
  recorder leaves ``text_intended`` empty for them, so the metric reports itself
  unmeasurable. It becomes measurable the moment the caller is a simulation with
  a known line to read — which is the point of the harness.
* ``task_completion`` and ``state_restored`` need a scenario's declared
  expectations and a fixture ledger. A real call has neither, so they report
  unmeasurable too.

That asymmetry is the honest result, not a gap to paper over. A metric that
returned 0.0 for the things it could not see would make a real call look worse
than a simulated one for no reason other than the absence of a script.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.models.call_record import CallStatus
from app.monitoring.call_trace import TerminationReason
from app.services.qa.metrics.context import build_context
from app.services.qa.metrics.runner import MetricResults, evaluate

if TYPE_CHECKING:
    from app.models.call_record import CallRecord

# How a call's stored disposition maps onto why the conversation ended. Only a
# completed call tells us anything: the rest never got far enough to have a
# conversational reason for stopping.
_TERMINATION_BY_STATUS = {
    CallStatus.COMPLETED.value: TerminationReason.CALLER_HANGUP,
    CallStatus.FAILED.value: TerminationReason.PIPELINE_ERROR,
}


def context_for_call(call_record: CallRecord) -> Any:
    """Build the metric context for one real call.

    ``has_audio`` follows whether turns were actually recorded — ``is not None``
    rather than truthiness, because a null column means the websocket carried no
    audio while an empty list means it carried audio that produced no turns.
    Only the first makes the audio metrics ``not_measurable``; the second leaves
    them measurable and finding nothing, which is a different fact.

    Either way they never score a zero, which would be a claim that the call was
    measured and was terrible.
    """
    turns = call_record.turns
    duration_ms = (
        float(call_record.duration_seconds) * 1000.0 if call_record.duration_seconds else None
    )

    return build_context(
        run_id=str(call_record.id),
        conversation=list(turns or []),
        termination_reason=_TERMINATION_BY_STATUS.get(
            call_record.status,
            TerminationReason.UNKNOWN,
        ),
        duration_ms=duration_ms,
        has_audio=turns is not None,
    )


def metrics_for_call(call_record: CallRecord) -> MetricResults:
    """Run every registered metric against a real call's recorded turns."""
    return evaluate(context_for_call(call_record))


__all__ = ["context_for_call", "metrics_for_call"]
