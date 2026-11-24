"""The clip bank: recorded caller answers, loaded as PCM at the rate a leg wants.

A bank is a directory of WAV files with a ``manifest.csv`` beside them. Each row
names a clip, the call it was cut from, the kind of question it answers, and
what a careful transcript says was said.
"""

from __future__ import annotations

import csv
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ClipInfo:
    clip: str
    call: str
    category: str
    secs: float
    said: str
    question: str
    checked_by_human: bool


def silence(seconds: float, rate: int) -> bytes:
    return b"\0" * (int(rate * seconds) * 2)


def resample_with_ffmpeg(path: Path, rate: int) -> bytes:
    """Convert any audio file to 16-bit mono PCM at ``rate`` using ffmpeg."""
    out = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            str(rate),
            "-",
        ],
        check=True,
        capture_output=True,
    )
    return out.stdout


class ClipBank:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self._info: dict[str, ClipInfo] = {}
        manifest = self.directory / "manifest.csv"
        if manifest.exists():
            with manifest.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    info = ClipInfo(
                        clip=row.get("clip", ""),
                        call=row.get("call", ""),
                        category=row.get("category", ""),
                        secs=float(row.get("secs") or 0),
                        said=row.get("said", ""),
                        question=row.get("question", ""),
                        checked_by_human=bool((row.get("checked_by_human") or "").strip()),
                    )
                    self._info[info.clip] = info

    def info(self, name: str) -> ClipInfo | None:
        return self._info.get(Path(name).stem)

    def path(self, name: str) -> Path:
        candidate = self.directory / name
        if candidate.suffix == "":
            candidate = candidate.with_suffix(".wav")
        if not candidate.exists():
            raise FileNotFoundError(f"clip not found in bank: {name}")
        return candidate

    def pcm(self, name: str, rate: int) -> bytes:
        """16-bit mono PCM at ``rate``. ffmpeg is used only when the file differs."""
        path = self.path(name)
        try:
            with wave.open(str(path), "rb") as wav:
                if (
                    wav.getframerate() == rate
                    and wav.getnchannels() == 1
                    and wav.getsampwidth() == 2
                    and wav.getcomptype() == "NONE"
                ):
                    return wav.readframes(wav.getnframes())
        except wave.Error:
            pass  # not plain PCM (for example mu-law): let ffmpeg decode it
        return resample_with_ffmpeg(path, rate)

    def joined(self, names: list[str], rate: int, gap_s: float = 0.25) -> bytes:
        """Several clips as one utterance, with ``gap_s`` of silence between them."""
        parts: list[bytes] = []
        for index, name in enumerate(names):
            if index:
                parts.append(silence(gap_s, rate))
            parts.append(self.pcm(name, rate))
        return b"".join(parts)
