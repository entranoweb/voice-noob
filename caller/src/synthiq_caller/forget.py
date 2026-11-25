"""``synthiq-caller forget``: remove a clip, or a speaker's clips, everywhere this tool reaches.

A clip is a recording of a person. When that person withdraws consent, asks for
erasure, or the clip passes its retention date, this command:

1. deletes the clip's audio from every bank directory;
2. removes the clip's row from each manifest, and its entry from ``overrides.json``
   (both hold what the person said, as a transcript);
3. rewrites every scenario and chain that plays it, in the directories given, so
   the step names ``missing/<clip>`` and the scenario lists the clip under
   ``missing_clips``: ``validate`` says "MISSING CLIP" and a batch does not run it;
4. marks every stored run, chain and batch file, in the results directories given,
   that played it, with a top-level ``clips_removed`` list (the grade is not changed);
5. appends one record to ``removals.jsonl`` beside the manifest (``removals.py``).

``--dry-run`` lists all of it and changes nothing, not even the log. Running it
again changes nothing more: a clip already gone, a scenario already rewritten and a
result already marked are left alone, and no second record is written for no work.
The same command with a further results directory catches up on that directory.

What it does not reach, and says so every time it runs: runs uploaded to a
dashboard (they hold the clip names and must be deleted or uploaded again), the
carrier's and the engine's own recordings, backups, and call recordings in a
results directory (listed, never deleted).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from synthiq_caller import removals
from synthiq_caller.clips import OVERRIDES_FILE
from synthiq_caller.consent import (
    OPERATOR_PREFIX,
    SPEAKER_PREFIX,
    Consent,
    opaque_id_problem,
    reference_problem,
)
from synthiq_caller.jsonio import write_json

MARK = "clips_removed"

UPLOAD_REMINDER = (
    "NOT DONE: runs uploaded to a dashboard (upload.json marks a directory that was uploaded) "
    "still hold these clip names and what the caller said. Delete those runs on the server, "
    "or upload the directory again with 'upload --replace'. Carrier and engine recordings, "
    "and backups, are outside this tool."
)


class ForgetError(Exception):
    """The request cannot be carried out; nothing was changed."""


def _pattern(stem: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(stem)}(?![A-Za-z0-9_-])")


def _strings(node: Any) -> list[str]:
    """Every string value in a JSON document (not its keys)."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [text for value in node.values() for text in _strings(value)]
    if isinstance(node, list):
        return [text for value in node for text in _strings(value)]
    return []


def _mentioned(texts: list[str], stems: list[str]) -> list[str]:
    patterns = {stem: _pattern(stem) for stem in stems}
    return [stem for stem, found in patterns.items() if any(found.search(t) for t in texts)]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".forget-tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _indent(text: str) -> int:
    found = re.search(r"\n( +)\S", text)
    return len(found.group(1)) if found else 2


@dataclass
class Plan:
    target: str
    stems: list[str]
    speakers: list[str]
    # Audio files to delete.
    audio: list[Path] = field(default_factory=list)
    # manifest.csv -> the clips whose rows go.
    rows: dict[Path, list[str]] = field(default_factory=dict)
    # overrides.json -> the clips whose entries go.
    overrides: dict[Path, list[str]] = field(default_factory=dict)
    # scenario or chain file -> the clips it plays that are not yet missing.
    scenarios: dict[Path, list[str]] = field(default_factory=dict)
    # stored run, chain or batch file -> the clips it used that it is not yet marked with.
    results: dict[Path, list[str]] = field(default_factory=dict)
    # Files that mention a clip and are not changed (uploads, scorecards, acceptance pages).
    mentioned: list[Path] = field(default_factory=list)
    # Sound files of calls that played a clip, in the results directories.
    recordings: list[Path] = field(default_factory=list)
    uploaded: list[Path] = field(default_factory=list)

    @property
    def changes(self) -> int:
        return (
            len(self.audio)
            + sum(map(len, self.rows.values()))
            + sum(map(len, self.overrides.values()))
            + len(self.scenarios)
            + len(self.results)
        )


def _manifest_rows(directory: Path) -> list[dict[str, str]]:
    manifest = directory / "manifest.csv"
    if not manifest.exists():
        return []
    with manifest.open(encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def resolve(directories: list[Path], target: str) -> tuple[list[str], list[str]]:
    """The clips ``target`` names, and the speaker refs they carry.

    ``target`` is a speaker ref (``spk_...``): every clip with that ref in any
    manifest or earlier removal record; or one clip id, with or without ``.wav``.
    """
    speakers: set[str] = set()
    stems: list[str] = []
    logged = [record for d in directories for record in removals.read_log(d / removals.LOG_FILE)]
    if target.startswith(f"{SPEAKER_PREFIX}_"):
        problem = opaque_id_problem(target, SPEAKER_PREFIX, "speaker ref")
        if problem:
            raise ForgetError(problem)
        for directory in directories:
            for row in _manifest_rows(directory):
                if Consent.from_row(row).speaker_ref == target:
                    stems.append(Path(row.get("clip") or "").stem)
        for record in logged:
            if target in record.get("speakers", []):
                stems += [Path(str(c)).stem for c in record["clips"]]
        speakers.add(target)
        found = sorted(dict.fromkeys(s for s in stems if s))
    else:
        stem = Path(target).stem
        known = any(
            Path(row.get("clip") or "").stem == stem
            for directory in directories
            for row in _manifest_rows(directory)
        )
        known = (
            known
            or any(stem in [Path(str(c)).stem for c in record["clips"]] for record in logged)
            or any(next(d.glob(f"{stem}.*"), None) is not None for d in directories)
        )
        found = [stem] if known else []
    if not found:
        raise ForgetError(
            f"{target!r} is not a clip or a speaker ref in any manifest, removal record or "
            "bank directory given; nothing was changed"
        )
    for directory in directories:
        for row in _manifest_rows(directory):
            if Path(row.get("clip") or "").stem in found:
                ref = Consent.from_row(row).speaker_ref
                if ref:
                    speakers.add(ref)
    for record in logged:
        if set(found) & {Path(str(c)).stem for c in record["clips"]}:
            speakers.update(str(s) for s in record.get("speakers", []))
    return found, sorted(speakers)


def _missing_text(stem: str, removal: str) -> str:
    return f"{stem}: removed by forget ({removal}); record a new clip, then restore the step"


def _rewrite_scenario(node: Any, stems: list[str], removal: str) -> list[str]:
    """Point every step that plays a removed clip at ``missing/<clip>``. Returns the clips hit.

    A scenario has ``steps`` and perhaps a ``fallback``; a chain has ``chain``, a
    list of calls each shaped like a scenario. A clip chosen at run time (``{{variable}}``) is
    not named here and is left alone.
    """
    if not isinstance(node, dict):
        return []
    hit: list[str] = []
    answers = [*(node.get("steps") or []), node.get("fallback")]
    for answer in answers:
        clips = answer.get("clips") if isinstance(answer, dict) else None
        if not isinstance(clips, list):
            continue
        for index, clip in enumerate(clips):
            if not isinstance(clip, str) or clip.startswith("missing/"):
                continue
            if Path(clip).stem in stems and "{{" not in clip:
                clips[index] = f"missing/{Path(clip).stem}"
                hit.append(Path(clip).stem)
    if hit:
        declared = node.setdefault("missing_clips", [])
        for stem in dict.fromkeys(hit):
            if not any(str(d).startswith(f"{stem}: removed") for d in declared):
                declared.append(_missing_text(stem, removal))
    for call in [*(node.get("chain") or []), *(node.get("calls") or [])]:
        hit += _rewrite_scenario(call, stems, removal)
    return hit


def _scenario_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*.json") if p.is_file())


def _plan_scenarios(plan: Plan, directories: list[Path]) -> None:
    for directory in directories:
        for path in _scenario_files(directory):
            data = _read_json(path)
            if not isinstance(data, dict) or not ("steps" in data or "chain" in data):
                continue
            hit = _rewrite_scenario(data, plan.stems, "dry-run")
            if hit:
                plan.scenarios[path] = sorted(dict.fromkeys(hit))


def _is_result(path: Path) -> bool:
    name = path.name
    return name == "batch.json" or (
        name.endswith(".json") and name.startswith(("run-", "chain-")) and name[:-5].count("-") == 1
    )


def _plan_results(plan: Plan, directories: list[Path]) -> None:
    stems = plan.stems
    for directory in directories:
        marked_runs: dict[str, list[str]] = {}
        files = sorted(p for p in directory.rglob("*.json") if p.is_file())
        for path in sorted(f for f in files if _is_result(f) and f.name.startswith("run-")):
            data = _read_json(path)
            if data is None:
                continue
            used = _mentioned(_strings(data), stems)
            if used:
                marked_runs[path.name] = used
                _sound_files(plan, path, data)
            done = {str(m.get("clip")) for m in data.get(MARK, []) if isinstance(m, dict)}
            todo = [s for s in used if s not in done]
            if todo:
                plan.results[path] = todo
        for path in files:
            if not _is_result(path) or path.name.startswith("run-"):
                continue
            data = _read_json(path)
            if data is None:
                continue
            used = _mentioned(_strings(data), stems)
            if path.name.startswith("chain-") and isinstance(data, dict):
                for row in data.get("calls") or []:
                    run = row.get("run") if isinstance(row, dict) else None
                    used += marked_runs.get(str(run), [])
            used = list(dict.fromkeys(used))
            done = {str(m.get("clip")) for m in data.get(MARK, []) if isinstance(m, dict)}
            todo = [s for s in used if s not in done]
            if todo:
                plan.results[path] = todo
        for path in files:
            if not _is_result(path) and _mentioned(_strings(_read_json(path)), stems):
                plan.mentioned.append(path)
        for html in sorted(directory.rglob("*.html")):
            try:
                text = html.read_text(encoding="utf-8")
            except OSError:
                continue
            if _mentioned([text], stems):
                plan.mentioned.append(html)
        if (directory / "upload.json").exists():
            plan.uploaded.append(directory / "upload.json")


def _sound_files(plan: Plan, run: Path, data: Any) -> None:
    for text in _strings(data):
        if text.lower().endswith((".wav", ".mp3")) and "/" not in text:
            for found in run.parent.rglob(text):
                if found not in plan.recordings:
                    plan.recordings.append(found)


def make_plan(target: str, banks: list[Path], scenarios: list[Path], results: list[Path]) -> Plan:
    stems, speakers = resolve(banks, target)
    plan = Plan(target=target, stems=stems, speakers=speakers)
    for bank in banks:
        for stem in stems:
            plan.audio += sorted(
                p for p in bank.glob(f"{stem}.*") if p.suffix.lower() in (".wav", ".mp3", ".flac")
            )
        rows = [
            Path(row.get("clip") or "").stem
            for row in _manifest_rows(bank)
            if Path(row.get("clip") or "").stem in stems
        ]
        if rows:
            plan.rows[bank / "manifest.csv"] = rows
        overrides = _read_json(bank / OVERRIDES_FILE)
        entries = overrides.get("clips") if isinstance(overrides, dict) else None
        if isinstance(entries, dict):
            held = [k for k in entries if Path(str(k)).stem in stems]
            if held:
                plan.overrides[bank / OVERRIDES_FILE] = [Path(k).stem for k in held]
    _plan_scenarios(plan, scenarios)
    _plan_results(plan, results)
    return plan


def describe(plan: Plan, *, dry: bool) -> list[str]:
    verb = "would" if dry else "did"
    lines = [
        f"{'DRY RUN: nothing is changed. ' if dry else ''}forget {plan.target}: "
        f"{len(plan.stems)} clip(s)" + (f", speaker(s) {plan.speakers}" if plan.speakers else "")
    ]
    lines += [f"  clip {s}" for s in plan.stems]
    lines += [f"  {verb} delete audio: {p}" for p in plan.audio]
    lines += [f"  {verb} remove manifest row: {p} ({', '.join(c)})" for p, c in plan.rows.items()]
    lines += [f"  {verb} remove override: {p} ({', '.join(c)})" for p, c in plan.overrides.items()]
    lines += [
        f"  {verb} make a scenario MISSING CLIP: {p} ({', '.join(c)})"
        for p, c in plan.scenarios.items()
    ]
    lines += [
        f"  {verb} mark result with {MARK}: {p} ({', '.join(c)})" for p, c in plan.results.items()
    ]
    lines += [f"  left alone, mentions a clip: {p}" for p in plan.mentioned]
    if plan.recordings:
        lines.append(
            f"  call recordings of calls that played these clips (not deleted; they hold the "
            f"voice, delete them if the request covers them): {len(plan.recordings)}"
        )
        lines += [f"    {p}" for p in plan.recordings]
    if not plan.changes:
        lines.append("  nothing to change: already removed, or no stored file uses these clips")
    lines.append(UPLOAD_REMINDER)
    if plan.uploaded:
        lines.append("  uploaded directories: " + ", ".join(str(p.parent) for p in plan.uploaded))
    return lines


def _remove_rows(path: Path, stems: list[str]) -> None:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        names = list(reader.fieldnames or [])
        kept = [r for r in reader if Path(r.get("clip") or "").stem not in stems]
    temporary = path.with_name(path.name + ".forget-tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=names)
        writer.writeheader()
        writer.writerows(kept)
    os.replace(temporary, path)


def execute(plan: Plan, banks: list[Path], *, operator: str, why: str, request_ref: str) -> str:
    """Carry the plan out and return the id of the record written.

    Order matters if this is interrupted: audio first, the log record after the
    stored files are changed, the manifest rows last. Run again, it finishes.
    """
    removal = removals.new_id()
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    for audio in plan.audio:
        audio.unlink(missing_ok=True)
    for path in plan.scenarios:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        _rewrite_scenario(data, plan.stems, removal)
        _write_atomic(path, json.dumps(data, indent=_indent(text), ensure_ascii=False) + "\n")
    for path, todo in plan.results.items():
        data = json.loads(path.read_text(encoding="utf-8"))
        held = [m for m in data.get(MARK, []) if isinstance(m, dict)]
        held += [{"clip": s, "removal": removal, "removed_on": day} for s in todo]
        data[MARK] = sorted(held, key=lambda m: str(m.get("clip")))
        write_json(path, data)
    log_directory = banks[0]
    removals.append(
        log_directory / removals.LOG_FILE,
        {
            "removal": removal,
            "removed_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "target": plan.target,
            "clips": plan.stems,
            "speakers": plan.speakers,
            "why": why,
            "request_ref": request_ref,
            "operator": operator,
            "effects": {
                "audio_files": len(plan.audio),
                "manifest_rows": sum(map(len, plan.rows.values())),
                "overrides": sum(map(len, plan.overrides.values())),
                "scenarios": sorted(p.name for p in plan.scenarios),
                "results": sorted(p.name for p in plan.results),
            },
        },
    )
    for path, _ in plan.rows.items():
        _remove_rows(path, plan.stems)
    for path in plan.overrides:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["clips"] = {k: v for k, v in data["clips"].items() if Path(k).stem not in plan.stems}
        _write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return removal


def run(args: argparse.Namespace) -> int:
    banks = [Path(args.clips), *(Path(p) for p in args.clips_extra)]
    for bank in banks:
        if not bank.is_dir():
            raise SystemExit(f"forget: clip bank directory not found: {bank}")
    for flag, value in (("--scenarios", args.scenarios), ("--results", args.results)):
        for directory in value:
            if not Path(directory).is_dir():
                raise SystemExit(f"forget: {flag} directory not found: {directory}")
    problems = [
        opaque_id_problem(args.operator, OPERATOR_PREFIX, "--operator") if args.operator else "",
        reference_problem(args.request_ref, "--request-ref") if args.request_ref else "",
    ]
    if not args.dry_run and not args.operator:
        problems.append("--operator is required: an opaque id (op_...), never a name")
    if any(problems):
        raise SystemExit("forget: " + "; ".join(p for p in problems if p))
    try:
        plan = make_plan(
            args.target,
            banks,
            [Path(p) for p in args.scenarios],
            [Path(p) for p in args.results],
        )
    except ForgetError as error:
        print(f"forget: {error}")
        return 2
    for line in describe(plan, dry=args.dry_run):
        print(line)
    if args.dry_run:
        return 0
    if not plan.changes:
        return 0
    removal = execute(
        plan, banks, operator=args.operator, why=args.reason, request_ref=args.request_ref
    )
    print(f"removal record {removal} appended to {banks[0] / removals.LOG_FILE}")
    return 0


def register(sub: Any) -> None:
    """Add the ``forget`` command to the command line's subcommands."""
    parser = sub.add_parser(
        "forget",
        help="remove a clip, or all of a speaker's clips: audio, scenarios, results, and a record",
    )
    parser.add_argument("target", help="a clip id, or a speaker ref (spk_...)")
    parser.add_argument("--clips", required=True, help="the clip bank; its removals.jsonl is kept")
    parser.add_argument("--clips-extra", action="append", default=[], help="a further bank")
    parser.add_argument(
        "--scenarios",
        action="append",
        default=[],
        help="a directory of scenarios and chains to rewrite (repeat for several)",
    )
    parser.add_argument(
        "--results",
        action="append",
        default=[],
        help="a directory of stored runs, chains and batches to mark (repeat for several)",
    )
    parser.add_argument("--operator", default="", help="who is doing this: an opaque id, op_...")
    parser.add_argument("--reason", choices=removals.REASONS, default="erasure_request")
    parser.add_argument("--request-ref", default="", help="id of the request, held elsewhere")
    parser.add_argument(
        "--dry-run", action="store_true", help="list everything it would touch; change nothing"
    )
    parser.set_defaults(handler=run)
