"""Deciding when the agent has finished speaking, from its audio alone.

An agent's stream commonly keeps sending frames while the agent is silent, so
"frames are arriving" says nothing. This tracker looks at the energy of each
frame instead. It is fed frames as they arrive and asked, at any moment, whether
the agent is speaking and when it last was.

Frames usually arrive faster than they would be heard: a two-second sentence may
land in a few hundred milliseconds. The tracker therefore keeps its own playhead
on the caller's clock, advancing it by each frame's duration, so "the agent
stopped" means "the listener would have stopped hearing it".
"""

from __future__ import annotations

import array
import math
from dataclasses import dataclass


def rms(pcm: bytes) -> float:
    """Root-mean-square level of 16-bit mono PCM; 0.0 for an empty frame."""
    usable = len(pcm) // 2 * 2
    if usable == 0:
        return 0.0
    samples = array.array("h", pcm[:usable])
    return math.sqrt(sum(s * s for s in samples) / len(samples))


@dataclass
class TurnSettings:
    # Frames quieter than this are silence. Telephone-grade speech sits in the
    # thousands; comfort noise and codec hiss sit well under two hundred.
    speech_floor: float = 250.0
    # Speech shorter than this is a click, not a turn.
    min_speech_s: float = 0.12
    # Silence this long after speech ends the agent's turn.
    hangover_s: float = 0.7
    # After agent text arrives, wait this long for its audio to start before
    # treating the line as already spoken (or text-only).
    audio_start_wait_s: float = 2.5
    # Never wait longer than this for one agent turn to end.
    max_turn_s: float = 45.0


class AgentTurnTracker:
    """Tracks one side's speech from its audio frames."""

    def __init__(self, settings: TurnSettings | None = None) -> None:
        self.settings = settings or TurnSettings()
        self._playhead = 0.0  # when already-received audio finishes playing
        self._speech_run_s = 0.0  # length of the current unbroken run of speech
        self._speech_end: float | None = None  # playhead time speech last ended
        self._spoke_since_mark = False
        self._mark_time = 0.0

    def mark(self, now: float) -> None:
        """Start watching for a new agent turn (call when agent text arrives)."""
        self._spoke_since_mark = False
        self._mark_time = now

    def feed(self, pcm: bytes, rate: int, now: float) -> None:
        """Account for one frame of agent audio received at ``now``."""
        duration = len(pcm) / 2 / rate if rate > 0 else 0.0
        start = max(self._playhead, now)
        self._playhead = start + duration
        if rms(pcm) > self.settings.speech_floor:
            self._speech_run_s += duration
            if self._speech_run_s >= self.settings.min_speech_s:
                self._speech_end = self._playhead
                self._spoke_since_mark = True
        else:
            self._speech_run_s = 0.0

    def interrupted(self, now: float) -> None:
        """The agent was cut off: whatever was queued will not be heard."""
        self._playhead = now
        if self._speech_end is not None:
            self._speech_end = min(self._speech_end, now)

    def speaking(self, now: float) -> bool:
        """True while received speech would still be audible."""
        return self._speech_end is not None and now < self._speech_end

    def turn_finished(self, now: float) -> bool:
        """True when the turn marked by ``mark`` has been spoken and has ended.

        Three ways to be finished: speech was heard and then ``hangover_s`` of
        quiet followed; no audio ever started within ``audio_start_wait_s``; or
        the turn ran past ``max_turn_s``.
        """
        s = self.settings
        if now - self._mark_time >= s.max_turn_s:
            return True
        if self._spoke_since_mark and self._speech_end is not None:
            return now >= self._speech_end + s.hangover_s
        return now - self._mark_time >= s.audio_start_wait_s
