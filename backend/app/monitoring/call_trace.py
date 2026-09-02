"""OpenTelemetry semantic conventions for voice calls.

This module is the single schema that both products read from:

* the **observability dashboard** ingests these attributes from any provider,
  via per-provider adapters that map their payloads onto these keys;
* the **evaluation framework** computes metrics from the same span tree, so a
  metric is provider-agnostic by construction.

Defining it as OpenTelemetry rather than a bespoke format is deliberate. ElevenLabs
already exports conversations as OTLP, so that adapter is close to pass-through;
customers can fan the same traces into observability they already run; and a
platform engineer can evaluate an OTLP exporter far faster than a private schema.

Span tree for one call::

    voice.call                      one span per call, the root
    ├── voice.turn                  one per conversational turn
    │   └── voice.tool_call         one per tool invocation within the turn
    └── voice.turn ...

Attribute names follow OTel conventions: lowercase dotted paths, units in the
suffix (``.duration_ms``), no provider-specific keys at the top level. Anything a
provider exposes that has no home here belongs under ``voice.provider.raw`` on
the call span, so nothing is silently dropped.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

# Instrumentation scope reported on every span this module produces.
INSTRUMENTATION_NAME: Final = "synthiq.voice"
INSTRUMENTATION_VERSION: Final = "0.1.0"

# ---------------------------------------------------------------------------
# Span names
# ---------------------------------------------------------------------------

SPAN_CALL: Final = "voice.call"
SPAN_TURN: Final = "voice.turn"
SPAN_TOOL_CALL: Final = "voice.tool_call"


# ---------------------------------------------------------------------------
# Enumerations
#
# Closed vocabularies, so a dashboard can group across providers without
# per-provider special-casing. Adapters map provider values onto these and put
# the original in the corresponding `*.provider_value` attribute.
# ---------------------------------------------------------------------------


class Direction(StrEnum):
    """Which side originated the call."""

    INBOUND = "inbound"
    OUTBOUND = "outbound"


class CallStatus(StrEnum):
    """Terminal disposition of a call."""

    COMPLETED = "completed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    FAILED = "failed"
    CANCELED = "canceled"


class TerminationReason(StrEnum):
    """Why the conversation ended.

    Distinguishing a caller hangup from an agent error from a pipeline failure is
    the first question asked of any voice dashboard, and every provider models it
    differently. This is the common denominator.
    """

    CALLER_HANGUP = "caller_hangup"
    AGENT_ENDED = "agent_ended"
    TRANSFERRED = "transferred"
    SILENCE_TIMEOUT = "silence_timeout"
    MAX_DURATION = "max_duration"
    PIPELINE_ERROR = "pipeline_error"
    UNKNOWN = "unknown"


class Speaker(StrEnum):
    """Who produced a turn."""

    CALLER = "caller"
    AGENT = "agent"


class ToolOutcome(StrEnum):
    """Result of a tool invocation.

    ``INVALID_ARGS`` is kept separate from ``ERROR`` on purpose: a malformed tool
    call is a model failure, while an error is usually a downstream one, and
    conflating them makes tool-call validity unmeasurable.
    """

    OK = "ok"
    ERROR = "error"
    INVALID_ARGS = "invalid_args"
    TIMEOUT = "timeout"


# ---------------------------------------------------------------------------
# voice.call attributes
# ---------------------------------------------------------------------------

# Identity
CALL_ID: Final = "voice.call.id"
CALL_PROVIDER: Final = "voice.call.provider"  # synthiq | vapi | retell | elevenlabs …
CALL_PROVIDER_CALL_ID: Final = "voice.call.provider_call_id"
CALL_AGENT_ID: Final = "voice.call.agent_id"
CALL_WORKSPACE_ID: Final = "voice.call.workspace_id"

# Shape
CALL_DIRECTION: Final = "voice.call.direction"
CALL_STATUS: Final = "voice.call.status"
CALL_FROM: Final = "voice.call.from"
CALL_TO: Final = "voice.call.to"

# Timing. Span start/end carry the wall clock; these are the derived figures a
# dashboard renders without having to walk the tree.
CALL_DURATION_MS: Final = "voice.call.duration_ms"
CALL_ANSWER_LATENCY_MS: Final = "voice.call.answer_latency_ms"
CALL_TURN_COUNT: Final = "voice.call.turn_count"

# Cost, in a fixed minor unit so no float currency arithmetic is needed.
CALL_COST_MICROS: Final = "voice.call.cost_micros"
CALL_COST_CURRENCY: Final = "voice.call.cost_currency"
CALL_TOKENS_INPUT: Final = "voice.call.tokens_input"
CALL_TOKENS_OUTPUT: Final = "voice.call.tokens_output"

# Outcome
CALL_TERMINATION_REASON: Final = "voice.call.termination_reason"
CALL_TERMINATION_PROVIDER_VALUE: Final = "voice.call.termination_reason.provider_value"
CALL_RECORDING_URL: Final = "voice.call.recording_url"

# Stack in use, so the dashboard can group by configuration and the compliance
# layer can derive the subprocessor list for a given call.
CALL_ENGINE: Final = "voice.call.engine"  # openai_realtime | speech_to_speech …
CALL_ENGINE_MODEL: Final = "voice.call.engine.model"
CALL_STT_VENDOR: Final = "voice.call.stt.vendor"
CALL_TTS_VENDOR: Final = "voice.call.tts.vendor"
CALL_CARRIER: Final = "voice.call.carrier"

# Escape hatch: provider payload fields with no mapping, as a JSON string.
# Better a lossless dump than silent truncation on ingest.
CALL_PROVIDER_RAW: Final = "voice.call.provider.raw"


# ---------------------------------------------------------------------------
# voice.turn attributes
# ---------------------------------------------------------------------------

TURN_INDEX: Final = "voice.turn.index"  # 0-based, ordered within the call
TURN_SPEAKER: Final = "voice.turn.speaker"

# The intended/transcribed split is what makes voice-specific failure measurable.
# `intended` is what the speaker meant to say — for the agent, the text handed to
# TTS; for a simulated caller, the text it was told to speak. `transcribed` is
# what the other side's STT actually heard. Word error rate and entity-level
# transcription accuracy are the delta between them, computed without a human
# ever listening. If only one is captured, that measurement is impossible.
TURN_TEXT_INTENDED: Final = "voice.turn.text.intended"
TURN_TEXT_TRANSCRIBED: Final = "voice.turn.text.transcribed"

# Latency. `response_ms` is the caller-perceived gap — end of caller speech to
# start of agent audio — which is the number that decides whether a call feels
# alive. `ttfb_ms` isolates the model's contribution to it.
TURN_RESPONSE_MS: Final = "voice.turn.response_ms"
TURN_TTFB_MS: Final = "voice.turn.ttfb_ms"
TURN_AUDIO_DURATION_MS: Final = "voice.turn.audio_duration_ms"

# Interruption handling — the failure mode transcript-only evaluation cannot see.
TURN_INTERRUPTED: Final = "voice.turn.interrupted"
TURN_BARGE_IN: Final = "voice.turn.barge_in"


# ---------------------------------------------------------------------------
# voice.tool_call attributes
# ---------------------------------------------------------------------------

TOOL_NAME: Final = "voice.tool_call.name"
TOOL_OUTCOME: Final = "voice.tool_call.outcome"
TOOL_DURATION_MS: Final = "voice.tool_call.duration_ms"
TOOL_ARGUMENTS: Final = "voice.tool_call.arguments"  # JSON string
TOOL_ERROR: Final = "voice.tool_call.error"


__all__ = [
    "CALL_AGENT_ID",
    "CALL_ANSWER_LATENCY_MS",
    "CALL_CARRIER",
    "CALL_COST_CURRENCY",
    "CALL_COST_MICROS",
    "CALL_DIRECTION",
    "CALL_DURATION_MS",
    "CALL_ENGINE",
    "CALL_ENGINE_MODEL",
    "CALL_FROM",
    "CALL_ID",
    "CALL_PROVIDER",
    "CALL_PROVIDER_CALL_ID",
    "CALL_PROVIDER_RAW",
    "CALL_RECORDING_URL",
    "CALL_STATUS",
    "CALL_STT_VENDOR",
    "CALL_TERMINATION_PROVIDER_VALUE",
    "CALL_TERMINATION_REASON",
    "CALL_TO",
    "CALL_TOKENS_INPUT",
    "CALL_TOKENS_OUTPUT",
    "CALL_TTS_VENDOR",
    "CALL_TURN_COUNT",
    "CALL_WORKSPACE_ID",
    "INSTRUMENTATION_NAME",
    "INSTRUMENTATION_VERSION",
    "SPAN_CALL",
    "SPAN_TOOL_CALL",
    "SPAN_TURN",
    "TOOL_ARGUMENTS",
    "TOOL_DURATION_MS",
    "TOOL_ERROR",
    "TOOL_NAME",
    "TOOL_OUTCOME",
    "TURN_AUDIO_DURATION_MS",
    "TURN_BARGE_IN",
    "TURN_INDEX",
    "TURN_INTERRUPTED",
    "TURN_RESPONSE_MS",
    "TURN_SPEAKER",
    "TURN_TEXT_INTENDED",
    "TURN_TEXT_TRANSCRIBED",
    "TURN_TTFB_MS",
    "CallStatus",
    "Direction",
    "Speaker",
    "TerminationReason",
    "ToolOutcome",
]
