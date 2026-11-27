import pytest

from synthiq_caller.legs.fake import tone
from synthiq_caller.mulaw import lin2ulaw, ulaw2lin
from synthiq_caller.stats import describe, wilson_interval
from synthiq_caller.turns import AgentTurnTracker, TurnSettings, rms

RATE = 16000
SILENCE = b"\0" * (RATE // 50 * 2)  # 20 ms


def feed(tracker: AgentTurnTracker, pcm: bytes, start: float, frame_s: float = 0.02) -> float:
    """Feed ``pcm`` as real-time frames starting at ``start``; return the end time."""
    size = int(RATE * frame_s) * 2
    t = start
    for offset in range(0, len(pcm), size):
        tracker.feed(pcm[offset : offset + size], RATE, t)
        t += frame_s
    return t


def test_rms_separates_tone_from_silence() -> None:
    assert rms(SILENCE) == 0.0
    assert rms(tone(0.02)) > 1000
    assert rms(b"") == 0.0


def test_turn_is_not_finished_while_speech_is_still_playing() -> None:
    tracker = AgentTurnTracker(TurnSettings(hangover_s=0.5))
    tracker.mark(0.0)
    end = feed(tracker, tone(1.0), 0.0)
    assert tracker.speaking(0.5)
    assert not tracker.turn_finished(end + 0.2)
    assert tracker.turn_finished(end + 0.6)


def test_burst_delivery_still_waits_for_the_listener() -> None:
    # Two seconds of speech arriving all at once must still take two seconds to hear.
    tracker = AgentTurnTracker(TurnSettings(hangover_s=0.5))
    tracker.mark(0.0)
    tracker.feed(tone(2.0), RATE, 0.0)
    assert not tracker.turn_finished(1.9)
    assert not tracker.turn_finished(2.3)
    assert tracker.turn_finished(2.6)


def test_silent_frames_do_not_keep_the_turn_open() -> None:
    # Agents keep streaming frames while silent; those must not count as speech.
    tracker = AgentTurnTracker(TurnSettings(hangover_s=0.5))
    tracker.mark(0.0)
    end = feed(tracker, tone(0.4), 0.0)
    feed(tracker, SILENCE * 100, end)  # two more seconds of silence frames
    assert tracker.turn_finished(end + 0.6)


def test_a_click_is_not_speech() -> None:
    tracker = AgentTurnTracker(TurnSettings(min_speech_s=0.12, audio_start_wait_s=2.0))
    tracker.mark(0.0)
    feed(tracker, tone(0.04), 0.0)
    feed(tracker, SILENCE * 10, 0.04)
    assert not tracker.turn_finished(1.0)  # still waiting for real audio to start
    assert tracker.turn_finished(2.1)  # gave up waiting: treated as spoken


def test_text_without_audio_times_out_rather_than_hanging() -> None:
    tracker = AgentTurnTracker(TurnSettings(audio_start_wait_s=1.0))
    tracker.mark(5.0)
    assert not tracker.turn_finished(5.5)
    assert tracker.turn_finished(6.1)


def test_interruption_ends_what_was_queued() -> None:
    tracker = AgentTurnTracker(TurnSettings(hangover_s=0.3))
    tracker.mark(0.0)
    tracker.feed(tone(3.0), RATE, 0.0)
    tracker.interrupted(1.0)
    assert not tracker.speaking(1.1)
    assert tracker.turn_finished(1.4)


def test_wilson_interval_behaves_at_the_edges() -> None:
    low, high = wilson_interval(5, 5)
    assert high == 1.0
    assert 0.5 < low < 0.6  # five of five is not proof of 100%
    low, high = wilson_interval(58, 60)
    assert 0.88 < low < 0.90 and 0.99 < high <= 1.0
    assert wilson_interval(0, 0) == (0.0, 1.0)
    with pytest.raises(ValueError):
        wilson_interval(6, 5)
    assert describe(0, 0) == "no runs"
    assert describe(19, 20).startswith("19 of 20 (95%")


def test_mulaw_round_trip_is_close() -> None:
    pcm = tone(0.05)
    back = ulaw2lin(lin2ulaw(pcm))
    assert len(back) == len(pcm)
    # Mu-law is lossy; the level must survive within a few percent.
    assert abs(rms(back) - rms(pcm)) / rms(pcm) < 0.05
    assert lin2ulaw(b"\0\0") == b"\xff"


def noise(seconds: float, level: int) -> bytes:
    """Steady line noise at roughly ``level`` RMS (a tone is a stand-in for hiss)."""
    return tone(seconds, RATE, level=int(level * 1.414))


def test_a_noisy_line_does_not_hold_the_turn_open() -> None:
    # Line noise at rms ~400 is above the old fixed floor of 250, so every frame
    # counted as speech and the turn never ended. The floor follows the call's noise.
    tracker = AgentTurnTracker(TurnSettings(hangover_s=0.5))
    tracker.mark(0.0)
    t = feed(tracker, noise(1.0, 400), 0.0)
    t = feed(tracker, tone(0.6), t)
    speech_end = t
    feed(tracker, noise(3.0, 400), t)
    assert tracker.floor() > 400
    assert not tracker.turn_finished(speech_end + 0.3)
    assert tracker.turn_finished(speech_end + 0.6)


def test_speech_quieter_than_the_old_floor_is_heard_on_a_quiet_line() -> None:
    tracker = AgentTurnTracker(TurnSettings(hangover_s=0.5, audio_start_wait_s=30.0))
    tracker.mark(0.0)
    t = feed(tracker, SILENCE * 50, 0.0)
    t = feed(tracker, noise(0.5, 150), t)  # soft speech, rms 150
    assert tracker.speaking(t - 0.1)
    assert tracker.floor() < 150


def test_extra_quiet_holds_a_turn_but_never_past_the_hard_wait() -> None:
    tracker = AgentTurnTracker(TurnSettings(hangover_s=0.5, max_turn_s=5.0))
    tracker.mark(0.0)
    end = feed(tracker, tone(1.0), 0.0)
    assert tracker.turn_finished(end + 0.6)
    assert not tracker.turn_finished(end + 0.6, extra_quiet_s=0.5)
    assert tracker.turn_finished(end + 1.1, extra_quiet_s=0.5)
    # Text with no audio, and a hold: still bounded.
    text_only = AgentTurnTracker(TurnSettings(audio_start_wait_s=1.0, max_turn_s=5.0))
    text_only.mark(0.0)
    assert not text_only.turn_finished(1.2, extra_quiet_s=0.5)
    assert text_only.turn_finished(5.0, extra_quiet_s=100.0)
