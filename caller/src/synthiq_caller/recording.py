"""Each side of a call as its own sound file.

A mixed recording cannot show who was speaking when the two overlap, and it is
the overlap that matters when a caller is cut off or talks over the agent. The
recorder keeps the two sides apart and on one clock: both files start at the
moment the media stream started, so a time in one is the same time in the other.

``agent`` is the audio that arrived from the far end, placed by the carrier's
own timestamp. ``caller`` is the audio this process released, placed by when it
was released. Neither is what the other side actually heard: the network and
the carrier sit in between. For that, use the carrier's own recording.
"""

from __future__ import annotations

import wave
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TRACKS = ("caller", "agent")


def resample(pcm: bytes, source: int, target: int) -> bytes:
    """16-bit mono PCM moved from ``source`` to ``target`` samples a second (linear).

    Good enough to listen to and to line two tracks up; it is not a measurement
    instrument, and nothing is graded on it.
    """
    if source == target or not pcm:
        return pcm
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    count = max(1, round(len(samples) * target / source))
    out = array("h")
    last = len(samples) - 1
    for i in range(count):
        position = i * source / target
        low = min(int(position), last)
        high = min(low + 1, last)
        fraction = position - low
        out.append(int(samples[low] * (1 - fraction) + samples[high] * fraction))
    return out.tobytes()


@dataclass
class CallRecorder:
    rate: int = 8000
    # A gap shorter than this is timer jitter, not silence: the audio is joined
    # up rather than padded, so no clicks are put into a continuous sentence.
    join_gaps_under_s: float = 0.04
    _tracks: dict[str, bytearray] = field(
        default_factory=lambda: {name: bytearray() for name in TRACKS}
    )

    def put(self, track: str, at_s: float, pcm: bytes, *, source_rate: int | None = None) -> None:
        """Place 16-bit mono PCM on ``track`` at ``at_s`` seconds after the stream started.

        ``source_rate`` is the rate ``pcm`` is in, when it is not the recorder's own.
        """
        if source_rate is not None:
            pcm = resample(pcm, source_rate, self.rate)
        if not pcm:
            return
        buffer = self._tracks[track]
        offset = max(0, int(at_s * self.rate)) * 2
        gap = offset - len(buffer)
        if gap >= int(self.join_gaps_under_s * self.rate) * 2:
            buffer.extend(b"\0" * gap)
        buffer.extend(pcm)

    def seconds(self, track: str) -> float:
        return len(self._tracks[track]) / 2 / self.rate

    @property
    def empty(self) -> bool:
        return not any(self._tracks.values())

    def save(self, directory: Path, stem: str) -> dict[str, Any]:
        """Write ``<stem>.caller.wav`` and ``<stem>.agent.wav``, the same length."""
        directory.mkdir(parents=True, exist_ok=True)
        length = max(len(buffer) for buffer in self._tracks.values())
        written: dict[str, Any] = {}
        for name in TRACKS:
            path = directory / f"{stem}.{name}.wav"
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(self.rate)
                wav.writeframes(bytes(self._tracks[name]).ljust(length, b"\0"))
            written[name] = path.name
        written["seconds"] = round(length / 2 / self.rate, 2)
        written["rate"] = self.rate
        written["starts_at"] = "media stream start"
        return written
