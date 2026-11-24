"""Keypad tones, generated as audio.

A Twilio media stream accepts only ``media``, ``mark`` and ``clear`` from our
side: there is no message that presses a key. So digits go down the line the way
a handset sends them, as two sine waves per key (the standard keypad frequency
pairs), in the same audio channel as speech. ``goertzel`` is here so tests, and
anyone debugging a real call, can check which two frequencies a stretch of audio
actually carries.
"""

from __future__ import annotations

import array
import math

LOW = (697, 770, 852, 941)
HIGH = (1209, 1336, 1477, 1633)
_KEYS = ("123A", "456B", "789C", "*0#D")

FREQUENCIES: dict[str, tuple[int, int]] = {
    key: (LOW[row], HIGH[column])
    for row, keys in enumerate(_KEYS)
    for column, key in enumerate(keys)
}


class UnknownDigit(ValueError):
    pass


def tone(digit: str, rate: int, seconds: float, level: int = 9000) -> bytes:
    """One key as 16-bit mono PCM. ``level`` is the peak of each of the two sines."""
    try:
        low, high = FREQUENCIES[digit.upper()]
    except KeyError as error:
        raise UnknownDigit(f"not a keypad key: {digit!r}") from error
    count = int(rate * seconds)
    samples = array.array(
        "h",
        (
            int(
                level * math.sin(2 * math.pi * low * i / rate)
                + level * math.sin(2 * math.pi * high * i / rate)
            )
            for i in range(count)
        ),
    )
    return samples.tobytes()


def tones(digits: str, rate: int, *, tone_s: float = 0.16, gap_s: float = 0.1) -> bytes:
    """A run of keys with silence between them. ``w`` pauses half a second, ``W`` one."""
    quiet = b"\0" * (int(rate * gap_s) * 2)
    parts: list[bytes] = []
    for digit in digits:
        if digit == "w":
            parts.append(b"\0" * (int(rate * 0.5) * 2))
        elif digit == "W":
            parts.append(b"\0" * (rate * 2))
        elif digit.isspace() or digit == "-":
            continue
        else:
            parts.append(tone(digit, rate, tone_s))
            parts.append(quiet)
    return b"".join(parts)


def goertzel(pcm: bytes, rate: int, frequency: float) -> float:
    """Power of one frequency in 16-bit mono PCM (the Goertzel algorithm)."""
    samples = array.array("h", pcm[: len(pcm) // 2 * 2])
    if not samples:
        return 0.0
    coefficient = 2 * math.cos(2 * math.pi * frequency / rate)
    previous = 0.0
    before = 0.0
    for sample in samples:
        current = sample + coefficient * previous - before
        before, previous = previous, current
    power = previous * previous + before * before - coefficient * previous * before
    return power / len(samples)


def detect(pcm: bytes, rate: int) -> str | None:
    """The key a stretch of audio carries, or None when no pair stands out."""
    low = {f: goertzel(pcm, rate, f) for f in LOW}
    high = {f: goertzel(pcm, rate, f) for f in HIGH}
    best_low = max(low, key=lambda f: low[f])
    best_high = max(high, key=lambda f: high[f])
    for best, group in ((best_low, low), (best_high, high)):
        others = [power for f, power in group.items() if f != best]
        if group[best] <= 0 or group[best] < 8 * max(others):
            return None
    for key, pair in FREQUENCIES.items():
        if pair == (best_low, best_high):
            return key
    return None
