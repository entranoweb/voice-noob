"""The clip bank: recorded caller answers, loaded as PCM at the rate a leg wants.

A bank is a directory of WAV files with a ``manifest.csv`` beside them. Each row
names a clip, the call it was cut from, the kind of question it answers, and
what a careful transcript says was said.

**Overrides.** The manifest is generated and the audio is evidence, so neither
is edited by hand. When a person finds a clip that does not say what its row
claims, they record it in ``overrides.json`` beside the manifest:

    {"clips": {
      "<clip id>": {"exclude": true, "reason": "plays 'Thank you. Bye.'", "date": "2026-11-25"},
      "<clip id>": {"label": "Neck pain.", "reason": "mislabelled", "date": "2026-11-25"}
    }}

Every entry needs a ``reason`` and a ``date`` (``YYYY-MM-DD``). ``label``
replaces what the manifest says was said; ``exclude`` takes the clip out of the
bank: asking for it raises ``ClipExcluded``, and a batch does not run a scenario
that names it.
"""

from __future__ import annotations

import csv
import json
import subprocess
import wave
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

OVERRIDES_FILE = "overrides.json"


class ClipExcluded(FileNotFoundError):
    """The clip is in the directory, and a person has ruled it out."""


@dataclass(frozen=True)
class Override:
    clip: str
    reason: str
    date: str
    exclude: bool = False
    # What the clip really says, when the manifest has it wrong.
    label: str = ""


def load_overrides(directory: Path) -> dict[str, Override]:
    """The bank's ``overrides.json``, checked. No file means no overrides."""
    path = directory / OVERRIDES_FILE
    if not path.exists():
        return {}
    data: Any = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("clips") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        raise ValueError(f"{path}: expected an object with a 'clips' object")
    found: dict[str, Override] = {}
    for name, raw in entries.items():
        clip = Path(str(name)).stem
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: {name}: expected an object")
        reason, when = str(raw.get("reason", "")).strip(), str(raw.get("date", "")).strip()
        if not reason:
            raise ValueError(f"{path}: {name}: an override needs a reason")
        try:
            date.fromisoformat(when)
        except ValueError as error:
            raise ValueError(f"{path}: {name}: an override needs a date, YYYY-MM-DD") from error
        exclude, label = raw.get("exclude", False) is True, str(raw.get("label", "")).strip()
        if exclude == bool(label):
            raise ValueError(f"{path}: {name}: give either 'exclude': true or a 'label'")
        found[clip] = Override(clip, reason, when, exclude, label)
    return found


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
        self.overrides = load_overrides(self.directory)
        for override in self.overrides.values():
            known = self._info.get(override.clip)
            if known is not None and override.label:
                # A person listened and wrote down what it says.
                self._info[override.clip] = replace(
                    known, said=override.label, checked_by_human=True
                )

    def excluded(self, name: str) -> Override | None:
        override = self.overrides.get(Path(name).stem)
        return override if override is not None and override.exclude else None

    def excluded_in(self, names: list[str]) -> list[str]:
        """Those of ``names`` a person has ruled out, in order, once each."""
        return list(dict.fromkeys(name for name in names if self.excluded(name)))

    def info(self, name: str) -> ClipInfo | None:
        return self._info.get(Path(name).stem)

    def path(self, name: str) -> Path:
        ruled_out = self.excluded(name)
        if ruled_out is not None:
            raise ClipExcluded(
                f"clip excluded from bank: {name} ({ruled_out.reason}, {ruled_out.date})"
            )
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
