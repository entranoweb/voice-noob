"""Build a clip bank from call recordings and their word-level transcripts.

For each ``<call>.mp3`` (or ``.wav``) with a ``<call>.stt.json`` beside it, cut
every caller answer into its own clip and write one manifest row per clip.

The transcript is ``{"words": [{"text", "start", "end", "type", "speaker_id"}]}``
with times in seconds: the shape batch speech-to-text services return when asked
for word timings and speakers.

The clips are a real person's recorded voice. This tool writes them wherever it
is told to; never point ``out`` inside a repository.
"""

from __future__ import annotations

import csv
import json
import subprocess
import wave
from pathlib import Path
from typing import Any

from synthiq_caller.clipbank.segments import PAD_S, ClipPlan, plan_clips
from synthiq_caller.clipbank.values import clip_value
from synthiq_caller.consent import FIELDS as CONSENT_FIELDS
from synthiq_caller.consent import Consent
from synthiq_caller.turns import rms

MANIFEST_COLUMNS = (
    "clip",
    "call",
    "category",
    "start",
    "end",
    "secs",
    "said",
    "intended",
    "value",
    "question",
    "review",
    "checked_by_human",
    # Where each clip came from and what may be done with it (``consent.py``).
    *CONSENT_FIELDS,
)
LOW_ENERGY_RMS = 150.0
RATE = 16000


def read_words(transcript: Path) -> list[dict[str, Any]]:
    data = json.loads(transcript.read_text(encoding="utf-8"))
    return [w for w in data.get("words", []) if w.get("type", "word") == "word"]


def cut(source: Path, start: float, end: float, target: Path, rate: int = RATE) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-to",
            f"{end:.3f}",
            "-i",
            str(source),
            "-ac",
            "1",
            "-ar",
            str(rate),
            "-c:a",
            "pcm_s16le",
            str(target),
        ],  # fmt: skip
        check=True,
    )


def clip_energy(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        return rms(wav.readframes(wav.getnframes()))


def manifest_row(call: str, index: int, plan: ClipPlan, pad_s: float) -> dict[str, str]:
    start, end = max(0.0, plan.start - pad_s), plan.end + pad_s
    return {
        "clip": f"{call}_{index:02d}_{plan.category}",
        "call": call,
        "category": plan.category,
        "start": f"{start:.2f}",
        "end": f"{end:.2f}",
        "secs": f"{end - start:.2f}",
        "said": plan.text,
        "intended": plan.text,
        "value": clip_value(plan.category, plan.text),
        "question": plan.question[-160:],
        "review": " ".join(plan.review),
        "checked_by_human": "",
    }


def consent_for(
    call: str, defaults: dict[str, str], per_call: dict[str, dict[str, str]]
) -> dict[str, str]:
    """The provenance columns for one call's clips, checked.

    These are given by the person running the build, never inferred: the audio
    says nothing about who spoke or on what basis they were recorded. A call is a
    ``real`` recording from a ``call`` unless told otherwise.
    """
    fields = {"source_kind": "call", "source_ref": call, "provenance": "real"}
    fields.update(defaults)
    fields.update(per_call.get(call, {}))
    unknown = sorted(set(fields) - set(CONSENT_FIELDS))
    if unknown:
        raise ValueError(f"{call}: not a provenance column: {unknown}")
    problems = Consent.from_row(dict(fields)).problems()
    if problems:
        raise ValueError(f"{call}: " + "; ".join(problems))
    return {name: fields.get(name, "") for name in CONSENT_FIELDS}


def build(
    recordings: Path,
    out: Path,
    *,
    pad_s: float = PAD_S,
    write_audio: bool = True,
    consent: dict[str, str] | None = None,
    consent_by_call: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Cut every recording under ``recordings``. Returns counts for a summary line.

    ``consent`` gives the provenance columns every clip gets; ``consent_by_call``
    gives them per call id, over the first.
    """
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []
    calls = 0
    for transcript in sorted(recordings.glob("*.stt.json")):
        call = transcript.name[: -len(".stt.json")]
        source = next(
            (p for p in (recordings / f"{call}.mp3", recordings / f"{call}.wav") if p.exists()),
            None,
        )
        if source is None:
            continue
        plans = plan_clips(read_words(transcript), pad_s=pad_s)
        if not plans:
            continue
        fields = consent_for(call, consent or {}, consent_by_call or {})
        calls += 1
        for index, plan in enumerate(plans, start=1):
            row = {**manifest_row(call, index, plan, pad_s), **fields}
            if write_audio:
                target = out / f"{row['clip']}.wav"
                cut(source, float(row["start"]), float(row["end"]), target)
                if clip_energy(target) < LOW_ENERGY_RMS:
                    row["review"] = f"{row['review']} low_energy".strip()
            rows.append(row)

    with (out / "manifest.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(MANIFEST_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)

    by_category: dict[str, int] = {}
    flags: dict[str, int] = {}
    for row in rows:
        by_category[row["category"]] = by_category.get(row["category"], 0) + 1
        for flag in row["review"].split():
            flags[flag] = flags.get(flag, 0) + 1
    return {
        "calls": calls,
        "clips": len(rows),
        "flagged": sum(1 for r in rows if r["review"]),
        "by_category": dict(sorted(by_category.items(), key=lambda kv: -kv[1])),
        "flags": flags,
    }
