"""Reconstruct conversational turns from a live audio bridge.

The three experience metrics — ``transcription_accuracy``, ``time_to_first_audio``
and ``interruption_handling`` — are written against ``TurnData`` fields that
nothing populated, because until now no audio ever reached them. This module is
what populates them, from the events a media-stream bridge already sees.

It is deliberately transport-agnostic. The bridge calls the methods below as
events arrive; the recorder owns the state machine and the clock. That keeps the
timing rules in one testable place instead of scattered through a websocket loop,
and means a second provider only has to call the same six methods.

What each field is measured from, and what it means when it is absent:

``ttfb_ms`` and ``response_ms``
    Both are the gap between the caller finishing a sentence and the first byte
    of agent audio going out to the carrier, for the turn that answers it. The
    schema keeps them apart — ``response_ms`` is the whole pipeline measured at
    the ear, ``ttfb_ms`` isolates the model's share of it — and on this bridge
    they coincide, because the bytes leaving toward the carrier are the only
    observation point there is. Recording one number under both names is honest;
    subtracting an invented constant to make them differ would not be.

    An agent turn with no preceding caller turn — the opening greeting — has
    nothing to measure from, so both record ``None``. A greeting timed from the
    start of the call would report a latency the caller never experienced as a
    wait.

``barge_in``
    The caller started speaking while the agent was still producing audio. This
    is a property of the *agent's* turn: it is the turn that was spoken over.

``interrupted``
    The agent's turn stopped early. Only the bridge knows this — it is the side
    that cancels the response and flushes the provider's playback buffer — so it
    is reported explicitly rather than inferred from timing.

``text_intended`` / ``text_transcribed`` (only when ``retain_text``)
    For the agent, ``intended`` is the text handed to speech synthesis. Nothing
    transcribes the agent's own audio back off the line, so ``transcribed`` stays
    ``None``. For the caller it is the reverse: ``transcribed`` is what STT heard,
    and ``intended`` is only known when something scripted the caller — a
    simulated run. On a call from a real human there is no ground truth, so WER
    is genuinely not measurable, and the recorder leaves the field empty rather
    than copying the transcript into it. Copying it would score every real call a
    flawless 0.0 and make the metric worthless precisely where it matters.

``audio_duration_ms``
    Derived from the bytes actually sent. G.711 is one byte per sample, so at
    8 kHz a byte is an eighth of a millisecond regardless of codec details.

An agent configured not to store transcripts sets ``retain_text=False``. Turns
are still recorded and still timed — that is telemetry about the call, not its
content — but no recognised speech is kept, so none reaches the metrics, the
database, or the trace. Word error rate is then unmeasurable, which is the
correct consequence of having chosen not to keep the words.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.monitoring.call_trace import Speaker, ToolOutcome

# G.711 (both µ-law and A-law) is one byte per sample. The sample rate is the
# only thing that varies, and Telnyx and Twilio both stream 8 kHz by default.
DEFAULT_SAMPLE_RATE_HZ = 8000


def _now() -> float:
    """Monotonic seconds. Wall clock can jump; a latency measured across a jump
    is worse than no latency at all."""
    return time.monotonic()


@dataclass
class _Turn:
    """A turn under construction. Becomes a ``TurnData`` dict when the call ends."""

    speaker: Speaker
    started_at: float
    ended_at: float | None = None
    text_intended: str | None = None
    text_transcribed: str | None = None
    ttfb_ms: float | None = None
    audio_bytes: int = 0
    interrupted: bool = False
    barge_in: bool = False
    # Set while the turn is still producing audio, so a caller speech-start can
    # tell "talked over the agent" from "spoke after the agent finished".
    speaking: bool = True


@dataclass
class _ToolCall:
    """One tool invocation, with when it happened relative to the call."""

    name: str
    outcome: ToolOutcome
    arguments: dict[str, Any]
    started_at: float
    duration_ms: float | None = None
    error: str | None = None


@dataclass
class AudioTurnRecorder:
    """Turns a stream of bridge events into turns the metrics can read.

    One recorder per call. Not thread-safe and not meant to be: a media bridge
    runs both directions on one event loop.
    """

    sample_rate_hz: int = DEFAULT_SAMPLE_RATE_HZ
    # What the caller was scripted to say, in order, when the run is simulated.
    # Empty on a real call, which is what keeps WER honest there.
    intended_caller_script: list[str] = field(default_factory=list)
    # Whether recognised speech may be kept. An agent with transcripts switched
    # off has had its owner decline to store what was said, and that decision
    # covers the metrics and the trace as much as the transcript column: the
    # turns are still recorded and still timed, they just carry no words.
    retain_text: bool = True

    _turns: list[_Turn] = field(default_factory=list, init=False)
    _tool_calls: list[_ToolCall] = field(default_factory=list, init=False)
    # When the first thing happened, so every turn can record how far into the
    # call it began. The trace needs those offsets to keep the silences between
    # turns, which is most of what a call trace is read for.
    _origin: float | None = field(default=None, init=False)
    _caller_speech_ended_at: float | None = field(default=None, init=False)
    _audio_observed: bool = field(default=False, init=False)
    _scripted_index: int = field(default=0, init=False)

    # -- caller side ------------------------------------------------------

    def caller_speech_started(self, *, at: float | None = None) -> None:
        """The caller began speaking.

        If an agent turn is still producing audio, this is a barge-in and is
        recorded against that agent turn.
        """
        moment = _now() if at is None else at
        self._audio_observed = True
        self._mark_origin(moment)

        talked_over = self._unfinished_turn(Speaker.AGENT)
        if talked_over is not None:
            talked_over.barge_in = True

        self._turns.append(_Turn(speaker=Speaker.CALLER, started_at=moment))

    def caller_speech_stopped(self, *, at: float | None = None) -> None:
        """The caller stopped speaking. Starts the clock for time-to-first-audio."""
        moment = _now() if at is None else at
        self._caller_speech_ended_at = moment

        turn = self._open_turn(Speaker.CALLER)
        if turn is not None:
            turn.ended_at = moment
            turn.speaking = False

    def caller_transcript(self, text: str, *, at: float | None = None) -> None:
        """STT finished transcribing a caller turn.

        Transcription completes after the turn has closed, so this attaches to the
        most recent caller turn rather than an open one. If none exists — some
        providers transcribe without ever emitting speech-start — one is opened
        so the transcript is not dropped.
        """
        moment = _now() if at is None else at
        self._mark_origin(moment)
        turn = self._last_turn(Speaker.CALLER)
        if turn is None:
            turn = _Turn(speaker=Speaker.CALLER, started_at=moment, ended_at=moment, speaking=False)
            self._turns.append(turn)
            self._audio_observed = True

        if self.retain_text:
            turn.text_transcribed = text
            # Only a scripted caller has an intention distinct from what was heard.
            if self._scripted_index < len(self.intended_caller_script):
                turn.text_intended = self.intended_caller_script[self._scripted_index]
                self._scripted_index += 1

    # -- agent side -------------------------------------------------------

    def agent_audio_delta(self, *, byte_count: int, at: float | None = None) -> None:
        """A chunk of agent audio went out to the caller.

        The first chunk of a turn is what time-to-first-audio measures to, so the
        turn is opened here rather than when the model started generating: bytes
        on the wire are the only thing the caller can hear.
        """
        moment = _now() if at is None else at
        self._audio_observed = True
        self._mark_origin(moment)

        turn = self._open_turn(Speaker.AGENT)
        if turn is None:
            turn = _Turn(speaker=Speaker.AGENT, started_at=moment)
            self._turns.append(turn)

        # Measured on the first chunk that actually carries audio, not on the
        # turn's creation: a transcript fragment can arrive before any audio and open
        # the turn itself, and timing from there would credit the agent with
        # bytes the caller could not yet hear — and an empty chunk is not audio
        # either. An opening greeting answers no caller turn, so there is nothing
        # to measure from and this stays None — not zero: the caller never
        # waited.
        if byte_count > 0 and turn.audio_bytes == 0 and self._caller_speech_ended_at is not None:
            turn.ttfb_ms = (moment - self._caller_speech_ended_at) * 1000.0
            self._caller_speech_ended_at = None

        turn.audio_bytes += max(byte_count, 0)

    def agent_transcript_delta(self, delta: str, *, at: float | None = None) -> None:
        """A fragment of the text the agent is speaking."""
        turn = self._open_turn(Speaker.AGENT)
        if turn is None:
            # Text can arrive marginally before the first audio chunk. Open the
            # turn without setting ttfb: no audio has reached the caller yet.
            turn = _Turn(speaker=Speaker.AGENT, started_at=_now() if at is None else at)
            self._turns.append(turn)
        if self.retain_text:
            turn.text_intended = (turn.text_intended or "") + delta

    def agent_turn_ended(self, *, interrupted: bool = False, at: float | None = None) -> None:
        """The agent's turn finished, either naturally or because it was cut off."""
        moment = _now() if at is None else at
        # Not the trailing turn: a barge-in opens the caller's turn *before* the
        # agent's response is reported done, so by the time this arrives the
        # agent's turn is no longer last. Looking only at the tail would drop the
        # interruption flag on exactly the turns that were interrupted.
        turn = self._unfinished_turn(Speaker.AGENT)
        if turn is None:
            return
        turn.ended_at = moment
        turn.speaking = False
        turn.interrupted = turn.interrupted or interrupted

    def tool_called(
        self,
        name: str,
        *,
        outcome: ToolOutcome = ToolOutcome.OK,
        arguments: dict[str, Any] | None = None,
        duration_ms: float | None = None,
        error: str | None = None,
        at: float | None = None,
    ) -> None:
        """The agent invoked a tool.

        Held here rather than emitted immediately because the trace tree is built
        once the call ends, and a span written mid-call would have no parent to
        attach to.
        """
        moment = _now() if at is None else at
        self._mark_origin(moment)
        self._tool_calls.append(
            _ToolCall(
                name=name,
                outcome=outcome,
                arguments=arguments or {},
                started_at=moment,
                duration_ms=duration_ms,
                error=error,
            ),
        )

    # -- results ----------------------------------------------------------

    @property
    def agent_is_speaking(self) -> bool:
        """Whether an agent turn is still producing audio.

        The bridge asks this before forwarding a caller speech-start, because it
        decides whether to flush the carrier's playout buffer. Answering it from
        here rather than from a second flag in the websocket loop keeps one
        definition of "the agent is talking".
        """
        return self._unfinished_turn(Speaker.AGENT) is not None

    @property
    def has_audio(self) -> bool:
        """Whether audio actually moved.

        Drives ``MetricContext.has_audio``, which is what keeps the three audio
        metrics reporting ``not_measurable`` instead of a fabricated zero on a run
        where nothing was ever heard.
        """
        return self._audio_observed

    def turn_count(self) -> int:
        return len(self._turns)

    def tool_calls(self) -> list[dict[str, Any]]:
        """Recorded tool invocations, with offsets from the start of the call."""
        origin = self._origin
        return [
            {
                "name": call.name,
                "outcome": call.outcome,
                "arguments": call.arguments,
                "duration_ms": call.duration_ms,
                "error": call.error,
                "offset_ms": (call.started_at - origin) * 1000.0 if origin is not None else 0.0,
            }
            for call in self._tool_calls
        ]

    def conversation(self) -> list[dict[str, Any]]:
        """The turns, in the shape ``build_context`` reads.

        Keys are omitted rather than set to ``None`` where a value was never
        measured, so nothing downstream can mistake an absent measurement for a
        recorded zero.
        """
        origin = self._origin
        records: list[dict[str, Any]] = []
        for turn in self._turns:
            record: dict[str, Any] = {"speaker": turn.speaker.value}
            # How far into the call this turn began. Without it the trace has to
            # pack turns end to end and the silences vanish.
            if origin is not None:
                record["offset_ms"] = (turn.started_at - origin) * 1000.0

            # Always present, even when empty, so the context builder knows this
            # turn came off a real line and must not fall back to copying one
            # side's text onto the other.
            record["text_intended"] = turn.text_intended
            record["text_transcribed"] = turn.text_transcribed

            if turn.ttfb_ms is not None:
                record["ttfb_ms"] = turn.ttfb_ms
                # The same measurement under the schema's other name. See the
                # module docstring: this bridge has one observation point.
                record["response_ms"] = turn.ttfb_ms
            if turn.ended_at is not None:
                # Wall-clock length of the turn. Not a latency — it is what the
                # trace uses to give the turn span a duration.
                record["duration_ms"] = (turn.ended_at - turn.started_at) * 1000.0
            if turn.audio_bytes:
                record["audio_duration_ms"] = turn.audio_bytes * 1000.0 / self.sample_rate_hz
            record["interrupted"] = turn.interrupted
            record["barge_in"] = turn.barge_in

            records.append(record)
        return records

    # -- internals --------------------------------------------------------

    def _mark_origin(self, moment: float) -> None:
        """Remember when the call's first recorded event happened."""
        if self._origin is None:
            self._origin = moment

    def _open_turn(self, speaker: Speaker) -> _Turn | None:
        """The trailing turn for this speaker, if it is still in progress."""
        if not self._turns:
            return None
        last = self._turns[-1]
        if last.speaker is speaker and last.speaking:
            return last
        return None

    def _unfinished_turn(self, speaker: Speaker) -> _Turn | None:
        """The most recent turn for this speaker that has not ended yet."""
        for turn in reversed(self._turns):
            if turn.speaker is speaker and turn.speaking:
                return turn
        return None

    def _last_turn(self, speaker: Speaker) -> _Turn | None:
        for turn in reversed(self._turns):
            if turn.speaker is speaker:
                return turn
        return None


__all__ = ["DEFAULT_SAMPLE_RATE_HZ", "AudioTurnRecorder"]
