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

Stored results also lose what was said: in every result that played a removed clip,
each free-text string (the timeline's ``heard`` and ``agent`` text, ``said``, the
trace and its tool-call arguments, ``read_back``, the grade's ``why`` and the values
it quotes) becomes ``[redacted <removal id>]``. The grade outcome, fault class,
pass/fail of each check, timings, tool and step names and clip ids stay. The mark
lists exactly which fields were redacted. ``--keep-text`` keeps the text, and is
allowed only when the reason is ``retention_expired``.

The record is written before anything is changed (outcome ``started``) and again
when the work ends (``complete``, or ``incomplete`` with what failed). A failure on
one file does not stop the rest; the command says so and exits non-zero.

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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from synthiq_caller import removals
from synthiq_caller.clipid import AUDIO_SUFFIXES, clip_id
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


def _ids_in(text: str) -> set[str]:
    """The clip ids a text names: the whole text, a quoted token, or a token with an audio ending.

    A bare word in prose ("a call about a day") is never a mention of the clip ``a``.
    """
    found = {clip_id(text.strip())}
    found |= {clip_id(quoted) for quoted in re.findall(r"""['"]([A-Za-z0-9_.\-/]+)['"]""", text)}
    for token in re.findall(r"[A-Za-z0-9_.\-]+", text):
        if token.lower().endswith(AUDIO_SUFFIXES):
            found.add(clip_id(token))
    return found


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
    ids: set[str] = set()
    for text in texts:
        ids |= _ids_in(text)
    return [stem for stem in stems if stem in ids]


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


def _is_yaml(path: Path) -> bool:
    return path.suffix.lower() in (".yaml", ".yml")


def _load_scenario(path: Path) -> tuple[Any, str]:
    """A scenario or chain file as batch loads it: JSON, or YAML when PyYAML is installed."""
    text = path.read_text(encoding="utf-8")
    if _is_yaml(path):
        import yaml  # type: ignore[import-untyped,unused-ignore]

        return yaml.safe_load(text), text
    return json.loads(text), text


def _dump_scenario(path: Path, data: Any, text: str) -> str:
    if _is_yaml(path):
        import yaml  # type: ignore[import-untyped,unused-ignore]

        return str(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return json.dumps(data, indent=_indent(text), ensure_ascii=False) + "\n"


def _label(path: Path) -> str:
    """A file named by its folder and name: enough to find it, never a whole path."""
    return f"{path.parent.name}/{path.name}"


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
    # Scenario files that name a clip and cannot be rewritten, and why.
    unreadable: list[tuple[Path, str]] = field(default_factory=list)
    # stored run, chain or batch file -> the clips it used that it is not yet marked with.
    results: dict[Path, list[str]] = field(default_factory=dict)
    # Stored run files whose text is to be redacted, and the fields that hold it.
    redact: dict[Path, list[str]] = field(default_factory=dict)
    # Files that mention a clip and are not changed (uploads, scorecards, acceptance pages).
    mentioned: list[Path] = field(default_factory=list)
    # Sound files of calls that played a clip, in the results directories.
    recordings: list[Path] = field(default_factory=list)
    uploaded: list[Path] = field(default_factory=list)
    # The recordings the clips were cut from (ids from the manifests), and any found.
    sources: list[str] = field(default_factory=list)
    source_files: list[Path] = field(default_factory=list)

    @property
    def changes(self) -> int:
        return (
            len(self.audio)
            + sum(map(len, self.rows.values()))
            + sum(map(len, self.overrides.values()))
            + len(self.scenarios)
            + len(self.unreadable)
            + len(self.results)
        )


def _manifest_rows(directory: Path) -> list[dict[str, str]]:
    manifest = directory / "manifest.csv"
    if not manifest.exists():
        return []
    with manifest.open(encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _audio_files(bank: Path, stems: list[str]) -> list[Path]:
    """Audio under ``bank``, in any folder, whose name is exactly a clip id and an audio ending.

    Compared name to name, never matched as a pattern: ``a`` does not find ``a.v2.wav``.
    """
    wanted = set(stems)
    return sorted(
        p
        for p in bank.rglob("*")
        if p.suffix.lower() in AUDIO_SUFFIXES
        and (p.is_file() or p.is_symlink())
        and clip_id(p.name) in wanted
    )


def resolve(directories: list[Path], target: str) -> tuple[list[str], list[str]]:
    """The clips ``target`` names, and the speaker refs they carry.

    ``target`` is a speaker ref (``spk_...``): every clip with that ref in any
    manifest or earlier removal record; or one clip id, with or without ``.wav``.
    It must be known: a manifest row, an earlier record, or an audio file of exactly
    that name. It is never used as a pattern.
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
                    stems.append(clip_id(row.get("clip") or ""))
        for record in logged:
            if target in record.get("speakers", []):
                stems += [clip_id(str(c)) for c in record["clips"]]
        speakers.add(target)
        found = sorted(dict.fromkeys(s for s in stems if s))
    else:
        if not target or any(char in target for char in "/\\*?[]"):
            raise ForgetError(
                f"{target!r} is not a clip id (it has a path or pattern character in it); "
                "give one clip id exactly, or a speaker ref; nothing was changed"
            )
        stem = clip_id(target)
        known = (
            any(
                clip_id(row.get("clip") or "") == stem
                for directory in directories
                for row in _manifest_rows(directory)
            )
            or any(stem in [clip_id(str(c)) for c in record["clips"]] for record in logged)
            or any(_audio_files(d, [stem]) for d in directories)
        )
        found = [stem] if known else []
    if not found:
        raise ForgetError(
            f"{target!r} is not a clip or a speaker ref in any manifest, removal record or "
            "bank directory given; nothing was changed"
        )
    for directory in directories:
        for row in _manifest_rows(directory):
            if clip_id(row.get("clip") or "") in found:
                ref = Consent.from_row(row).speaker_ref
                if ref:
                    speakers.add(ref)
    for record in logged:
        if set(found) & {clip_id(str(c)) for c in record["clips"]}:
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
            if clip_id(clip) in stems and "{{" not in clip:
                clips[index] = f"missing/{clip_id(clip)}"
                hit.append(clip_id(clip))
    if hit:
        declared = node.setdefault("missing_clips", [])
        for stem in dict.fromkeys(hit):
            if not any(str(d).startswith(f"{stem}: removed") for d in declared):
                declared.append(_missing_text(stem, removal))
    for call in [*(node.get("chain") or []), *(node.get("calls") or [])]:
        hit += _rewrite_scenario(call, stems, removal)
    return hit


def _scenario_files(directory: Path) -> list[Path]:
    return sorted(
        p
        for p in directory.rglob("*")
        if p.is_file() and p.suffix.lower() in (".json", ".yaml", ".yml")
    )


def _plan_scenarios(plan: Plan, directories: list[Path]) -> None:
    for directory in directories:
        for path in _scenario_files(directory):
            try:
                data, _ = _load_scenario(path)
            except Exception as error:  # noqa: BLE001 - a file we cannot read may still name a clip
                text = path.read_text(encoding="utf-8", errors="replace")
                if _mentioned([text], plan.stems):
                    plan.unreadable.append((path, f"{type(error).__name__}"))
                continue
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


def _is_redacted(text: object) -> bool:
    return isinstance(text, str) and text.startswith("[redacted ")


# Keys whose string values say which step, tool, clip or class of fault it was, not what a
# person said. Everything else under the parts of a result listed in ``_TEXT_PARTS`` is text.
_KEEP_KEYS = frozenset(
    {
        "scenario",
        "scenario_file",
        "call_ref",
        "outcome",
        "fault",
        "kind",
        "name",
        "step_id",
        "step_path",
        "clips",
        "synthetic_clips",
        "field",
        "verdict",
        "stored_source",
        "role",
        "leg",
        "mutates",
    }
)
# The parts of a stored result that can hold what someone said, heard or typed.
_TEXT_PARTS = (
    "record",
    "trace",
    "trace_error",
    "read_back",
    "read_back_error",
    "hook_error",
    "grade",
)
# Under these, numbers are what was said too (an age, a phone number): redact them as well.
_VALUE_KEYS = frozenset({"arguments", "result", "outputs", "request", "dtmf"})

KEPT_NOTE = (
    "kept: grade outcome and fault class, which checks passed, timings, tool and step names, "
    "clip ids"
)


def _shape(path: str) -> str:
    return re.sub(r"(\[\*\])+$", "", re.sub(r"\[\d+\]", "[*]", path))


def _scrub(
    node: Any,
    marker: str,
    path: str,
    found: set[str],
    *,
    values: str = "",
    keep: bool = True,
) -> Any:
    """``node`` with every free-text string replaced by ``marker``; ``found`` gets each place.

    ``values`` is the path of the value subtree (tool arguments, say) the node is inside;
    it is reported as one field, and numbers in it are redacted too.
    """
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            plain = isinstance(value, str) or (
                isinstance(value, list) and all(isinstance(v, str) for v in value)
            )
            if keep and not values and key in _KEEP_KEYS and plain:
                out[key] = value
            else:
                inside = values or (here if key in _VALUE_KEYS else "")
                out[key] = _scrub(value, marker, here, found, values=inside, keep=keep)
        return out
    if isinstance(node, list):
        return [
            _scrub(v, marker, f"{path}[{i}]", found, values=values, keep=keep)
            for i, v in enumerate(node)
        ]
    if isinstance(node, str) and node and not _is_redacted(node):
        found.add(_shape(values or path))
        return marker
    if values and isinstance(node, int | float) and not isinstance(node, bool):
        found.add(_shape(values))
        return marker
    return node


def _redacted_result(data: dict[str, Any], removal: str) -> tuple[dict[str, Any], list[str]]:
    """The result with its free text redacted, and the list of fields that held any."""
    found: set[str] = set()
    marker = f"[redacted {removal}]"
    out = dict(data)
    for part in _TEXT_PARTS:
        if part in out:
            out[part] = _scrub(
                out[part], marker, part, found, keep=not part.startswith("read_back")
            )
    return out, sorted(found)


def _text_fields(data: dict[str, Any]) -> list[str]:
    """The fields of this result that still hold text (none once it is redacted)."""
    return _redacted_result(data, "x")[1]


def _plan_results(plan: Plan, directories: list[Path], *, keep_text: bool) -> None:
    stems = plan.stems
    for directory in directories:
        marked_runs: dict[str, list[str]] = {}
        files = sorted(p for p in directory.rglob("*.json") if p.is_file())
        for path in sorted(f for f in files if _is_result(f) and f.name.startswith("run-")):
            data = _read_json(path)
            if not isinstance(data, dict):
                continue
            used = _mentioned(_strings(data), stems)
            if used:
                marked_runs[path.name] = used
                _sound_files(plan, path, data)
            done = {str(m.get("clip")) for m in data.get(MARK, []) if isinstance(m, dict)}
            todo = [s for s in used if s not in done]
            fields = _text_fields(data) if used and not keep_text else []
            if todo or fields:
                plan.results[path] = todo
            if fields:
                plan.redact[path] = fields
        for path in files:
            if not _is_result(path) or path.name.startswith("run-"):
                continue
            data = _read_json(path)
            if not isinstance(data, dict):
                continue
            used = _mentioned(_strings(data), stems)
            if path.name.startswith("chain-"):
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


def _plan_sources(plan: Plan, banks: list[Path], recordings: list[Path]) -> None:
    """The recordings the clips were cut from: named by id, and found where the operator says."""
    for bank in banks:
        for row in _manifest_rows(bank):
            if clip_id(row.get("clip") or "") in plan.stems:
                for column in ("source_ref", "call"):
                    value = (row.get(column) or "").strip()
                    if value and value not in plan.sources:
                        plan.sources.append(value)
    for directory in recordings:
        for path in sorted(p for p in directory.rglob("*") if p.is_file()):
            if any(path.name == ref or path.name.startswith(ref + ".") for ref in plan.sources):
                plan.source_files.append(path)


def make_plan(
    target: str,
    banks: list[Path],
    scenarios: list[Path],
    results: list[Path],
    *,
    keep_text: bool = False,
    recordings: list[Path] | None = None,
) -> Plan:
    stems, speakers = resolve(banks, target)
    plan = Plan(target=target, stems=stems, speakers=speakers)
    for bank in banks:
        plan.audio += _audio_files(bank, stems)
        rows = [
            clip_id(row.get("clip") or "")
            for row in _manifest_rows(bank)
            if clip_id(row.get("clip") or "") in stems
        ]
        if rows:
            plan.rows[bank / "manifest.csv"] = rows
        overrides = _read_json(bank / OVERRIDES_FILE)
        entries = overrides.get("clips") if isinstance(overrides, dict) else None
        if isinstance(entries, dict):
            held = [k for k in entries if clip_id(str(k)) in stems]
            if held:
                plan.overrides[bank / OVERRIDES_FILE] = [clip_id(k) for k in held]
    _plan_sources(plan, banks, recordings or [])
    _plan_scenarios(plan, scenarios)
    _plan_results(plan, results, keep_text=keep_text)
    return plan


def describe(plan: Plan, *, dry: bool, keep_text: bool = False) -> list[str]:
    verb = "would" if dry else "will"
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
    lines += [f"  CANNOT rewrite {p} ({why}); it names a clip" for p, why in plan.unreadable]
    for path, clips in plan.results.items():
        lines.append(f"  {verb} mark result with {MARK}: {path} ({', '.join(clips)})")
        if path in plan.redact:
            lines.append(f"    {verb} redact: {', '.join(plan.redact[path])}")
    if plan.redact:
        lines.append(f"  {KEPT_NOTE}")
        lines.append(
            "  NOT redacted: batch and chain summaries (marked only), uploads, scorecards, "
            "acceptance pages, call recordings"
        )
    if keep_text:
        lines.append(
            "  --keep-text: nothing in stored results is redacted; each result keeps what was "
            "said and heard, and its mark says redacted: false"
        )
    lines += [f"  left alone, mentions a clip: {p}" for p in plan.mentioned]
    if plan.recordings:
        lines.append(
            f"  call recordings of calls that played these clips (not deleted; they hold the "
            f"voice, delete them if the request covers them): {len(plan.recordings)}"
        )
        lines += [f"    {p}" for p in plan.recordings]
    if plan.sources:
        lines.append(
            "  SOURCE RECORDINGS: these clips were cut from the recordings "
            f"{', '.join(plan.sources)} (the directory given to 'clipbank build --recordings'). "
            "They hold the whole call, this person's voice and the agent's, and are NOT "
            "deleted. Find and delete them if the request covers them (--recordings <dir> lists "
            "the files)."
        )
        lines += [f"    {p}" for p in plan.source_files]
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
        kept = [r for r in reader if clip_id(r.get("clip") or "") not in stems]
    temporary = path.with_name(path.name + ".forget-tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=names)
        writer.writeheader()
        writer.writerows(kept)
    os.replace(temporary, path)


def _rewrite_file(path: Path, stems: list[str], removal: str) -> None:
    data, text = _load_scenario(path)
    _rewrite_scenario(data, stems, removal)
    _write_atomic(path, _dump_scenario(path, data, text))


def _mark_result(
    path: Path, plan: Plan, removal: str, day: str, todo: list[str], *, wipe: bool
) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    held = [m for m in data.get(MARK, []) if isinstance(m, dict)]
    fields: list[str] = []
    if wipe:
        data, fields = _redacted_result(data, removal)
        for mark in held:
            if str(mark.get("clip")) in plan.stems:
                mark["redacted"] = True
                mark["redacted_fields"] = sorted({*mark.get("redacted_fields", []), *fields})
    held += [
        {
            "clip": s,
            "removal": removal,
            "removed_on": day,
            "redacted": wipe,
            "redacted_fields": fields,
        }
        for s in todo
    ]
    data[MARK] = sorted(held, key=lambda m: str(m.get("clip")))
    write_json(path, data)


def _drop_overrides(path: Path, stems: list[str]) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["clips"] = {k: v for k, v in data["clips"].items() if clip_id(str(k)) not in stems}
    _write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def execute(
    plan: Plan,
    banks: list[Path],
    *,
    operator: str,
    why: str,
    request_ref: str,
    keep_text: bool = False,
) -> tuple[str, list[str]]:
    """Carry the plan out; return the id of the record and what failed (empty when nothing).

    The record is appended first (outcome ``started``), so a run that dies leaves
    its trace and the clips are already ruled out. Each file is tried on its own: a
    failure is noted and the rest goes on. The record is appended again at the end,
    ``complete`` or ``incomplete``. Run again, it finishes what is left.
    """
    removal = removals.new_id()
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    log = banks[0] / removals.LOG_FILE
    base = {
        "removal": removal,
        "removed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "target": plan.target,
        "clips": plan.stems,
        "speakers": plan.speakers,
        "why": why,
        "request_ref": request_ref,
        "operator": operator,
        "text_kept": keep_text,
    }
    removals.append(log, {**base, "outcome": "started"})
    failed: list[str] = []
    done = {"audio_files": 0, "manifest_rows": 0, "overrides": 0, "scenarios": 0, "results": 0}

    def attempt(
        what: str, path: Path, action: Callable[[], None], counter: str, n: int = 1
    ) -> None:
        try:
            action()
        except Exception as error:  # noqa: BLE001 - one file failing must not stop the others
            failed.append(f"{what} {_label(path)}: {type(error).__name__}")
        else:
            done[counter] += n

    for audio in plan.audio:
        attempt("delete", audio, partial(audio.unlink, missing_ok=True), "audio_files")
    for path in plan.scenarios:
        attempt(
            "rewrite",
            path,
            partial(_rewrite_file, path, plan.stems, removal),
            "scenarios",
        )
    for path, why_not in plan.unreadable:
        failed.append(f"rewrite {_label(path)}: {why_not} (it names a clip and was not changed)")
    for path, todo in plan.results.items():
        wipe = path in plan.redact
        attempt(
            "mark",
            path,
            partial(_mark_result, path, plan, removal, day, todo, wipe=wipe),
            "results",
        )
    for path, clips in plan.rows.items():
        attempt(
            "remove rows from",
            path,
            partial(_remove_rows, path, plan.stems),
            "manifest_rows",
            len(clips),
        )
    for path, clips in plan.overrides.items():
        attempt(
            "remove overrides from",
            path,
            partial(_drop_overrides, path, plan.stems),
            "overrides",
            len(clips),
        )
    removals.append(
        log,
        {
            **base,
            "outcome": "incomplete" if failed else "complete",
            "failed": failed,
            "effects": {
                **done,
                "scenario_files": sorted(p.name for p in plan.scenarios),
                "result_files": sorted(p.name for p in plan.results),
            },
        },
    )
    return removal, failed


def run(args: argparse.Namespace) -> int:
    banks = [Path(args.clips), *(Path(p) for p in args.clips_extra)]
    for bank in banks:
        if not bank.is_dir():
            raise SystemExit(f"forget: clip bank directory not found: {bank}")
    for flag, value in (
        ("--scenarios", args.scenarios),
        ("--results", args.results),
        ("--recordings", args.recordings),
    ):
        for directory in value:
            if not Path(directory).is_dir():
                raise SystemExit(f"forget: {flag} directory not found: {directory}")
    problems = [
        opaque_id_problem(args.operator, OPERATOR_PREFIX, "--operator") if args.operator else "",
        reference_problem(args.request_ref, "--request-ref") if args.request_ref else "",
    ]
    if not args.dry_run and not args.operator:
        problems.append("--operator is required: an opaque id (op_...), never a name")
    if args.keep_text and args.reason != "retention_expired":
        problems.append(
            "--keep-text is only allowed with --reason retention_expired; for an erasure "
            "request or withdrawn consent, what was said is redacted"
        )
    if any(problems):
        raise SystemExit("forget: " + "; ".join(p for p in problems if p))
    for warning in removals.unfinished_lines(banks):
        print(f"forget: {warning}")
    try:
        plan = make_plan(
            args.target,
            banks,
            [Path(p) for p in args.scenarios],
            [Path(p) for p in args.results],
            keep_text=args.keep_text,
            recordings=[Path(p) for p in args.recordings],
        )
    except ForgetError as error:
        print(f"forget: {error}")
        return 2
    for line in describe(plan, dry=args.dry_run, keep_text=args.keep_text):
        print(line)
    pending = [
        r
        for r in removals.unfinished(banks)
        if {clip_id(str(c)) for c in r["clips"]} <= set(plan.stems)
    ]
    if args.dry_run or not (plan.changes or pending):
        return 0
    try:
        removal, failed = execute(
            plan,
            banks,
            operator=args.operator,
            why=args.reason,
            request_ref=args.request_ref,
            keep_text=args.keep_text,
        )
    except OSError as error:
        print(f"forget: INCOMPLETE: the removal log could not be written ({error!r})")
        return 1
    log = banks[0] / removals.LOG_FILE
    if failed:
        print(f"forget: INCOMPLETE, removal {removal}: {len(failed)} step(s) failed:")
        print("\n".join(f"  {line}" for line in failed))
        print("The record says so. Fix the cause and run the same command again to finish.")
        return 1
    print(f"removal {removal} complete; recorded in {log}")
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
        help="a directory of scenarios and chains (json, yaml) to rewrite (repeat for several)",
    )
    parser.add_argument(
        "--results",
        action="append",
        default=[],
        help="a directory of stored runs, chains and batches to mark (repeat for several)",
    )
    parser.add_argument(
        "--recordings",
        action="append",
        default=[],
        help="the directory given to 'clipbank build --recordings': its files for these "
        "clips' source calls are listed, never deleted",
    )
    parser.add_argument("--operator", default="", help="who is doing this: an opaque id, op_...")
    parser.add_argument("--reason", choices=removals.REASONS, default="erasure_request")
    parser.add_argument("--request-ref", default="", help="id of the request, held elsewhere")
    parser.add_argument(
        "--keep-text",
        action="store_true",
        help="keep what was said and heard in stored results (only for retention_expired); "
        "otherwise it is redacted",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="list everything it would touch; change nothing"
    )
    parser.set_defaults(handler=run)
