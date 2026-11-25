"""Turn a captured call into clips, a manifest and a scenario skeleton.

    synthiq-caller ingest cases/<name>/            [--stt words.json] [--pad 0.5] [--force]

Reads what ``capture`` wrote and adds, in the same directory:

* ``clips/``         one WAV per caller turn, and ``clips/manifest.csv``
* ``scenarios/scenario.json``  the agent's questions in order as steps, **no expectations**
  (its own directory, so ``validate cases/<name>/scenarios`` sees only scenarios)

**Where the cuts come from.** No speech-to-text service is called. By default a
caller turn is cut from the platform's own turn times (``time_in_call_secs``),
which are whole seconds, so each cut is padded (``--pad``, default 0.5 s each
side) and every such clip is flagged ``coarse_cut mixed_audio``: the recording
is one mixed track, so a clip can carry the tail of the agent's voice or lose a
syllable of the caller's. A person must listen before a clip is trusted. With
``--stt <file>`` (word times and speakers from a service the user configured and
ran, the same file ``clipbank build`` reads) the cuts are the ordinary
word-level ones instead.

**What is claimed.** Every clip is ``label_status=unverified`` and
``provenance=real-call``: it is a recording of a person, and what it says is a
machine's reading. Consent and retention fields are left out; they are P8's.
The scenario skeleton states no expectations, so ``validate`` calls it
ungradeable and a batch holds it back until a person says what the call must do.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from synthiq_caller.capture import CAPTURE_FILE, CONVERSATION_FILE
from synthiq_caller.clipbank.build import (
    MANIFEST_COLUMNS,
    build,
    clip_energy,
    cut,
    manifest_row,
)
from synthiq_caller.clipbank.segments import ClipPlan, label
from synthiq_caller.jsonio import write_json

CLIPS_DIR = "clips"
SCENARIO_DIR = "scenarios"
SCENARIO_FILE = "scenarios/scenario.json"
PAD_S = 0.5
EXTRA_COLUMNS = ("label_status", "provenance", "lawful_basis", "consent_ref")
LOW_ENERGY_RMS = 150.0


class IngestError(Exception):
    """The case could not be ingested; the message is fit to print."""


def _turns(conversation: dict[str, Any]) -> list[dict[str, Any]]:
    """Spoken turns only (a turn with no words is a tool step), in the platform's order."""
    out: list[dict[str, Any]] = []
    for turn in conversation.get("transcript") or []:
        text = str(turn.get("message") or "").strip()
        at = turn.get("time_in_call_secs")
        if text and isinstance(at, int | float):
            out.append(
                {
                    "role": str(turn.get("role", "")),
                    "t": float(at),
                    "text": text,
                    "interrupted": bool(turn.get("interrupted")),
                }
            )
    return out


def plan_turn_clips(
    turns: list[dict[str, Any]], *, pad_s: float, duration_s: float | None
) -> list[tuple[int, ClipPlan]]:
    """One plan per caller turn, with the index of the turn it answers in ``turns``.

    The turn's start is its platform time; its end is the next turn's start.
    Both are whole seconds, so both are padded, and each plan says so.
    """
    plans: list[tuple[int, ClipPlan]] = []
    question = ""
    for index, turn in enumerate(turns):
        if turn["role"] == "agent":
            question = turn["text"]
            continue
        following = turns[index + 1] if index + 1 < len(turns) else None
        start = turn["t"]
        # A start is somewhere in [t, t+1): a turn can be a second long and no less.
        end = max(following["t"] if following else (duration_s or start + 1.0), start + 1.0)
        review = ["coarse_cut", "mixed_audio"]
        if following is not None and following["t"] <= start:
            review.append("overlaps_agent")
        previous = turns[index - 1] if index else None
        if previous is not None and previous["role"] == "agent" and previous["interrupted"]:
            review.append("overlaps_agent")  # the caller cut the agent off: both were speaking
        plan = ClipPlan(
            max(0.0, start - pad_s),
            end + pad_s,
            turn["text"],
            question,
            label(question, turn["text"]),
            list(dict.fromkeys(review)),
        )
        plans.append((index, plan))
    return plans


def _question_pattern(agent_text: str) -> str:
    """A pattern for what the agent asked: its last question, or last sentence, in its own words."""
    sentences = [s.strip() for s in re.split(r"(?<=[.?!])\s+", agent_text) if s.strip()]
    asked = [s for s in sentences if s.endswith("?")]
    chosen = (asked or sentences or [agent_text])[-1]
    words = re.findall(r"[\w'’]+", chosen)[-12:]
    return r"\s+".join(re.escape(w) for w in words) if words else re.escape(chosen)


def skeleton(
    case: str,
    turns: list[dict[str, Any]],
    placed: dict[int, str],
    source: dict[str, Any],
) -> dict[str, Any]:
    """The scenario: each agent line a caller turn answered, in order. Nothing is expected."""
    steps: list[dict[str, Any]] = []
    not_placed: list[str] = []
    for index, turn in enumerate(turns):
        if turn["role"] == "agent":
            continue
        clip = placed.get(index)
        question = turns[index - 1] if index and turns[index - 1]["role"] == "agent" else None
        if clip is None:
            not_placed.append(f"turn {index} (no clip was cut)")
            continue
        if question is None:
            not_placed.append(f"{clip} (the caller spoke without an agent question before it)")
            continue
        steps.append(
            {
                "when": _question_pattern(question["text"]),
                "clips": [clip],
                "intended": turn["text"],
                "max_uses": 1,
            }
        )
    if not steps:
        raise IngestError("no caller turn follows an agent question; there is no scenario to write")
    return {
        "name": f"replay of {case}",
        "description": (
            f"Skeleton from captured call {source.get('conversation_id', '')}. It states no "
            "expectations, so it is UNGRADEABLE until a person adds them. The agent questions "
            "are the platform's machine transcript of one call, not the words the agent will say "
            "on a new call: check every `when` pattern. `intended` is also a machine reading."
        ),
        "mutates": "unknown",
        "persona": {"facts": {}},
        "max_seconds": 360,
        "hangup_after": "have a lovely day|take care, bye|goodbye",
        "steps": steps,
        "expect": {"tools": [], "fields": []},
        "notes": {
            "captured_from": source.get("conversation_id", ""),
            "labels": "unverified: a machine's transcript, not what was said",
            "clips_not_placed": not_placed,
        },
    }


def _write_manifest(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*MANIFEST_COLUMNS, *EXTRA_COLUMNS])
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **row,
                    "checked_by_human": "",  # nobody has listened
                    "label_status": "unverified",
                    "provenance": "real-call",
                    # Special-category data: a person fills these in; nothing is guessed.
                    "lawful_basis": "",
                    "consent_ref": "",
                }
            )


def _find_audio(case: Path) -> Path:
    found = sorted(p for p in case.glob("audio.*") if p.is_file())
    if not found:
        raise IngestError(
            f"{case} has no audio file; capture did not get a recording, so there is nothing to cut"
        )
    return found[0]


def _duration(audio: Path) -> float | None:
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(audio),
            ],  # fmt: skip
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return float(out)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def _prepare(case: Path, force: bool) -> None:
    clips, scenario = case / CLIPS_DIR, case / SCENARIO_FILE
    if (clips.exists() and any(clips.iterdir())) or scenario.exists():
        if not force:
            raise IngestError(
                f"{case} already has clips or a scenario; nothing was changed. "
                "Use --force to cut them again (edits to scenario.json are lost)."
            )
        if clips.exists():
            shutil.rmtree(clips)
        scenario.unlink(missing_ok=True)


def ingest(
    case: Path, *, stt: Path | None = None, pad_s: float = PAD_S, force: bool = False
) -> dict[str, Any]:
    """Cut the clips and write the manifest and scenario. Returns counts for a summary."""
    for needed in (CAPTURE_FILE, CONVERSATION_FILE):
        if not (case / needed).exists():
            raise IngestError(f"{case} is not a capture: {needed} is missing")
    source = json.loads((case / CAPTURE_FILE).read_text(encoding="utf-8"))
    conversation = json.loads((case / CONVERSATION_FILE).read_text(encoding="utf-8"))
    audio = _find_audio(case)
    call = re.sub(r"[^\w.-]", "_", str(source.get("conversation_id") or case.name))
    turns = _turns(conversation)
    _prepare(case, force)
    clips = case / CLIPS_DIR
    clips.mkdir(parents=True)

    placed: dict[int, str] = {}
    try:
        if stt is not None:
            rows = _cut_with_stt(audio, stt, clips, call, pad_s)
            _align_to_turns(rows, turns, placed)
            cut_by = "stt"
        else:
            rows = _cut_by_turn(audio, turns, clips, call, pad_s, conversation, placed)
            cut_by = "turn_times"
        if not rows:
            raise IngestError("no caller clips could be cut from this call")
        _write_manifest(clips / "manifest.csv", rows)
        scenario = skeleton(case.name, turns, placed, source)
        (case / SCENARIO_DIR).mkdir(exist_ok=True)
        write_json(case / SCENARIO_FILE, scenario)
    except Exception:
        shutil.rmtree(clips, ignore_errors=True)  # no half-cut bank left to block a retry
        raise
    return {
        "clips": len(rows),
        "steps": len(scenario["steps"]),
        "cut_by": cut_by,
        "pad_s": pad_s,
        "not_placed": scenario["notes"]["clips_not_placed"],
    }


def _cut_by_turn(
    audio: Path,
    turns: list[dict[str, Any]],
    clips: Path,
    call: str,
    pad_s: float,
    conversation: dict[str, Any],
    placed: dict[int, str],
) -> list[dict[str, str]]:
    duration = _duration(audio)
    if duration is None:
        meta = (conversation.get("metadata") or {}).get("call_duration_secs")
        duration = float(meta) if isinstance(meta, int | float) else None
    rows: list[dict[str, str]] = []
    for number, (index, plan) in enumerate(
        plan_turn_clips(turns, pad_s=pad_s, duration_s=duration), start=1
    ):
        if duration is not None and plan.start >= duration:
            continue  # the platform's time lies past the end of the recording
        row = manifest_row(call, number, plan, 0.0)  # the plan is already padded
        target = clips / f"{row['clip']}.wav"
        cut(audio, plan.start, plan.end, target)
        flags = row["review"].split()
        if clip_energy(target) < LOW_ENERGY_RMS:
            flags.append("low_energy")
        row["review"] = " ".join(flags)
        rows.append(row)
        placed[index] = row["clip"]
    return rows


def _cut_with_stt(
    audio: Path, stt: Path, clips: Path, call: str, pad_s: float
) -> list[dict[str, str]]:
    """The ordinary word-level build, over a scratch copy of the recording and its words."""
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch)
        shutil.copy(audio, work / f"{call}{audio.suffix}")
        shutil.copy(stt, work / f"{call}.stt.json")
        build(work, clips, pad_s=pad_s)
    with (clips / "manifest.csv").open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _align_to_turns(
    rows: list[dict[str, str]], turns: list[dict[str, Any]], placed: dict[int, str]
) -> None:
    """Pair word-level clips with the platform's caller turns, in order, when the counts agree.

    When they do not, no pairing is guessed: the steps are left for a person.
    """
    caller_turns = [i for i, t in enumerate(turns) if t["role"] != "agent"]
    if len(caller_turns) == len(rows):
        for index, row in zip(caller_turns, rows, strict=True):
            placed[index] = row["clip"]


def run(args: argparse.Namespace) -> int:
    case = Path(args.case)
    try:
        result = ingest(
            case,
            stt=Path(args.stt) if args.stt else None,
            pad_s=args.pad,
            force=args.force,
        )
    except IngestError as error:
        print(f"ingest: {error}")
        return 2
    except subprocess.CalledProcessError as error:
        print(f"ingest: ffmpeg could not cut the recording (exit {error.returncode})")
        return 2
    print(
        f"cut {result['clips']} clip(s) into {case / CLIPS_DIR}; {result['steps']} step(s) written"
    )
    if result["cut_by"] == "turn_times":
        print(
            f"  cut by the platform's whole-second turn times, padded {result['pad_s']}s each "
            "side: coarse. Listen to every clip; a clip can hold the agent's voice or miss "
            "the caller's first or last syllable."
        )
    print("  every clip: label unverified, provenance real-call. Labels are a machine's reading.")
    print(
        f"  Fill in lawful_basis and consent_ref for every clip in {case / CLIPS_DIR}: "
        "these are recordings of a person (special-category data) and a batch, run or phone "
        "call will refuse them until both are filled."
    )
    if result["not_placed"]:
        print(f"  not placed: {result['not_placed']}")
    print(f"  {case / SCENARIO_FILE}: UNGRADEABLE until expectations are stated.")
    return 0


def register(sub: Any) -> None:
    """Add the ``ingest`` command to the command line's subcommands."""
    parser = sub.add_parser("ingest", help="cut a captured call into clips and a scenario skeleton")
    parser.add_argument("case", help="a directory written by `capture`")
    parser.add_argument(
        "--stt", default=None, help="word-level transcript from a service you configured"
    )
    parser.add_argument("--pad", type=float, default=PAD_S, help="seconds added each side of a cut")
    parser.add_argument("--force", action="store_true", help="cut again over existing clips")
    parser.set_defaults(handler=run)
