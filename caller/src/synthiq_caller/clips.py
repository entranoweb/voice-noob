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

A bank can be given further directories. They hold clips the first does not,
each with its own manifest. A manifest's ``provenance`` column says where a clip
came from: a recording of a real caller or a synthetic voice. A clip whose row
does not say (an older manifest, or no row) has *unknown* provenance, which is
not "real": a batch will not play it unless told to, and every result it touches
says so. A synthetic clip is not evidence of how the agent hears real people,
and every result it touches says so too. The other consent and provenance
columns are described in ``consent.py``.

**Removal.** ``synthiq-caller forget`` deletes a clip's audio and writes a record
to ``removals.jsonl`` beside the manifest (``removals.py``). A bank reads that
log, so a clip removed there is ruled out the way an excluded one is, even if a
copy of its file is put back.
"""

from __future__ import annotations

import json
import subprocess
import wave
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

from synthiq_caller import consent as consent_rules
from synthiq_caller.clipid import clip_id
from synthiq_caller.consent import UNKNOWN, Consent
from synthiq_caller.manifestcsv import read_rows
from synthiq_caller.removals import read_removed

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
        clip = clip_id(str(name))
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
    # Doubts the builder raised about this clip (empty when it raised none).
    review: str = ""
    # Where the clip came from and what may be done with it (``consent.py``).
    consent: Consent = field(default_factory=Consent)
    # Which manifest's directory the row was read from.
    folder: str = ""

    @property
    def provenance(self) -> str:
        """``real``, ``synthetic`` or ``unknown`` (a manifest that does not say)."""
        return self.consent.kind

    @property
    def synthetic(self) -> bool:
        return self.provenance == "synthetic"

    @property
    def special_category(self) -> bool:
        """True for a real caller's recording (UK GDPR Article 9 data)."""
        return self.consent.special_category

    @property
    def confirmed(self) -> bool:
        return self.checked_by_human or bool(self.consent.label_confirmed_by)


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
    def __init__(
        self,
        directory: str | Path,
        extra: tuple[str | Path, ...] = (),
        *,
        allow_unknown_provenance: bool = False,
        allow_missing_consent: bool = False,
    ) -> None:
        # Every playback goes through ``pcm``, which refuses a clip past its retention
        # date, always, and a clip whose provenance is unknown unless this is set.
        self.allow_unknown_provenance = allow_unknown_provenance
        # A real clip is special-category data: it needs a lawful basis and a consent record
        # to be played. This is its own switch; allowing unknown provenance never lifts it.
        self.allow_missing_consent = allow_missing_consent
        self.directory = Path(directory)
        self.directories = [self.directory, *(Path(d) for d in extra)]
        self._info: dict[str, ClipInfo] = {}
        # The first bank wins a name both hold, so read the others first.
        for folder in reversed(self.directories):
            self._read_manifest(folder)

    def _read_manifest(self, folder: Path) -> None:
        manifest = folder / "manifest.csv"
        if manifest.exists():
            for row in read_rows(manifest)[1]:
                info = ClipInfo(
                    clip=clip_id(row.get("clip") or ""),
                    call=row.get("call", ""),
                    category=row.get("category", ""),
                    secs=float(row.get("secs") or 0),
                    said=row.get("said", ""),
                    question=row.get("question", ""),
                    checked_by_human=bool((row.get("checked_by_human") or "").strip()),
                    review=(row.get("review") or "").strip(),
                    consent=Consent.from_row(row),
                    folder=str(folder),
                )
                self._info[info.clip] = info
        self.overrides = load_overrides(self.directory)
        # What ``forget`` removed, from every directory: a clip removed from any is gone.
        self.removed = read_removed(self.directories)
        for override in self.overrides.values():
            known = self._info.get(override.clip)
            if known is not None and override.label:
                # A person listened and wrote down what it says.
                self._info[override.clip] = replace(
                    known, said=override.label, checked_by_human=True
                )

    def excluded(self, name: str) -> Override | None:
        stem = clip_id(name)
        gone = self.removed.get(stem)
        if gone is not None:
            return Override(stem, f"removed, see {gone.removal}", gone.date, exclude=True)
        override = self.overrides.get(stem)
        return override if override is not None and override.exclude else None

    def excluded_in(self, names: list[str]) -> list[str]:
        """Those of ``names`` a person has ruled out, in order, once each."""
        return list(dict.fromkeys(name for name in names if self.excluded(name)))

    def info(self, name: str) -> ClipInfo | None:
        return self._info.get(clip_id(name))

    def verified(self, name: str) -> bool:
        """True when a person has confirmed what this clip says.

        A clip's label starts life as a recogniser's transcript of it. Until a
        person has listened and filled ``checked_by_human`` in the manifest,
        the label is a guess, and a scenario fact written from it may be wrong.
        """
        info = self.info(name)
        # A synthetic clip was made from its text, so its label is known.
        return info is not None and (info.confirmed or info.synthetic)

    def synthetic(self, name: str) -> bool:
        """True when the clip is a synthetic voice, not a recording of a caller."""
        info = self.info(name)
        return info is not None and info.synthetic

    def label_doubts(self, names: list[str]) -> list[str]:
        """Each clip in ``names`` whose label nobody has confirmed, with why."""
        doubts: list[str] = []
        for name in names:
            info = self.info(name)
            if info is None:
                doubts.append(f"{name}: not in the manifest")
            elif not (info.confirmed or info.synthetic):
                why = f"flagged {info.review}, " if info.review else ""
                doubts.append(f"{name}: {why}not checked by a person")
        return doubts

    def provenance_unknown(self, names: list[str]) -> list[str]:
        """Each clip in ``names`` whose manifest row does not say where it came from, once."""
        unknown: list[str] = []
        for name in dict.fromkeys(names):
            info = self.info(name)
            if info is None:
                unknown.append(f"{name}: not in the manifest")
            elif info.provenance == UNKNOWN:
                said = f"provenance {info.consent.provenance!r}" if info.consent.provenance else ""
                unknown.append(f"{name}: {said or 'no provenance in the manifest'}")
        return unknown

    def consent_missing(self, names: list[str]) -> list[str]:
        """Each real clip in ``names`` with no lawful basis or consent record, once."""
        missing: list[str] = []
        for name in dict.fromkeys(names):
            info = self.info(name)
            # A clip with no manifest row says nothing of itself: it lacks both.
            lacks = (
                info.consent.record_missing()
                if info is not None
                else ["lawful_basis", "consent_ref"]
            )
            if lacks:
                missing.append(f"{name}: no {' or '.join(lacks)}")
        return missing

    def expired_in(self, names: list[str], today: date | None = None) -> list[str]:
        """Those of ``names`` past their ``retain_until`` date, with the date, once each."""
        day = today or consent_rules.today_utc()
        late: list[str] = []
        for name in dict.fromkeys(names):
            info = self.info(name)
            if info is not None and info.consent.expired(day):
                late.append(f"{name}: retain_until {info.consent.retain_until}")
        return late

    def infos(self) -> list[ClipInfo]:
        """Every clip the manifests describe."""
        return list(self._info.values())

    def path(self, name: str) -> Path:
        ruled_out = self.excluded(name)
        if ruled_out is not None:
            raise ClipExcluded(
                f"clip excluded from bank: {name} ({ruled_out.reason}, {ruled_out.date})"
            )
        for folder in self.directories:
            candidate = folder / name
            if candidate.suffix == "":
                candidate = candidate.with_suffix(".wav")
            if candidate.exists():
                return candidate
        raise FileNotFoundError(f"clip not found in bank: {name}")

    def unusable(self, name: str) -> str:
        """Why the file for ``name`` cannot be played; empty when it can (or is not a WAV).

        Looks inside the file: one a few bytes long, with no audio, or cut short is
        refused here, before a call, instead of playing silence and being blamed on the
        agent. A WAV in a format this module does not decode itself (mu-law, say) is left
        to ffmpeg and passes.
        """
        try:
            path = self.path(name)
        except (FileNotFoundError, ClipExcluded):
            return ""  # missing or excluded is said elsewhere, in its own words
        return wav_problem(path)

    def refusal(self, name: str) -> str:
        """Why ``name`` may not be played; empty when it may. The one rule every playback obeys."""
        info = self.info(name)
        if info is not None and info.consent.expired(consent_rules.today_utc()):
            return f"{name}: past its retention date (retain_until {info.consent.retain_until!r})"
        if not self.allow_unknown_provenance and self.provenance_unknown([name]):
            return f"{name}: provenance not recorded; allow unknown provenance to play it"
        if not self.allow_missing_consent and self.consent_missing([name]):
            return f"{name}: consent record missing; allow missing consent to play it"
        return ""

    def pcm(self, name: str, rate: int) -> bytes:
        """16-bit mono PCM at ``rate``. ffmpeg is used only when the file differs."""
        refused = self.refusal(name)
        if refused:
            raise ClipExcluded(f"clip not played: {refused}")
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


def wav_problem(path: Path) -> str:
    """Why a WAV file holds no usable audio; empty when it does."""
    try:
        size = path.stat().st_size
    except OSError as error:
        return f"cannot be read ({error.__class__.__name__})"
    if size < 44:
        return f"only {size} byte(s): too short to be a WAV file"
    try:
        with wave.open(str(path), "rb") as wav:
            frames = wav.getnframes()
            if frames == 0:
                return "no audio in it (zero frames)"
            wanted = frames * wav.getsampwidth() * wav.getnchannels()
            if len(wav.readframes(frames)) < wanted:
                return "the audio is cut short (the file is truncated)"
    except wave.Error as error:
        if "unknown format" in str(error):
            return ""  # not plain PCM: ffmpeg decodes it at play time
        return f"not a readable WAV ({error})"
    except EOFError:
        return "not a readable WAV (ends early)"
    return ""
