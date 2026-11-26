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
import copy
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
from synthiq_caller.capture import CAPTURE_FILE, _written_names
from synthiq_caller.clipid import AUDIO_SUFFIXES, clip_id
from synthiq_caller.clips import OVERRIDES_FILE
from synthiq_caller.completeness import ALTERED, verdict
from synthiq_caller.consent import (
    OPERATOR_PREFIX,
    SPEAKER_PREFIX,
    Consent,
    opaque_id_problem,
    reference_problem,
)
from synthiq_caller.ingest import CLIPS_DIR, SCENARIO_DIR
from synthiq_caller.jsonio import write_json, write_run
from synthiq_caller.manifestcsv import ManifestError, read_rows

MARK = "clips_removed"

# Every top-level key the rig writes into a result file (run, phone run, chain): the batch
# payload, what a leg adds about its line, what a chain call adds, regrade, a phone run, the
# digest and forget's own mark. Anything else in a file is a hand-added field.
RIG_FIELDS = frozenset(
    {
        "scenario_file", "leg", "mutates", "states_expectations", "repeat", "record", "trace",
        "trace_error", "read_back", "read_back_error", "hook_error", "labels_unverified",
        "provenance_unknown", "consent_missing", "synthetic_clips", "grade", "rehearsal",
        "batch", "carrier_call_ref", "phone", "audio", "chain", "variables", "dry_run",
        "recordings", "chain_file", "outcome", "uncaptured", "reason", "calls", "integrity",
        "fault", MARK,
    }
)  # fmt: skip

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
    # The caller-side track of a recorded call that played a clip: our own audio of the
    # person's voice, written beside the run file. Deleted (not with --keep-text).
    call_audio: list[Path] = field(default_factory=list)
    # The whole call a clip was cut from, in an ingested case folder: its audio, and its
    # transcript and conversation (the last two stay under --keep-text, and are listed).
    case_files: list[Path] = field(default_factory=list)
    case_kept: list[Path] = field(default_factory=list)
    # The skeleton scenarios ``ingest`` wrote beside the clips; they hold the call's words.
    case_scenarios: list[Path] = field(default_factory=list)
    # scenario file -> JSON paths that still hold a forgotten answer after the rewrite.
    leftovers: dict[Path, list[str]] = field(default_factory=dict)
    # Scenario directories beside a clip bank (or its case) that were not scanned.
    unscanned: list[Path] = field(default_factory=list)
    uploaded: list[Path] = field(default_factory=list)
    # The recordings the clips were cut from (ids from the manifests), and any found.
    sources: list[str] = field(default_factory=list)
    source_files: list[Path] = field(default_factory=list)

    @property
    def changes(self) -> int:
        return (
            len(self.audio)
            + len(self.call_audio)
            + len(self.case_files)
            + sum(map(len, self.rows.values()))
            + sum(map(len, self.overrides.values()))
            + len(self.scenarios)
            + len(self.leftovers)
            + len(self.unreadable)
            + len(self.results)
        )


def _manifest_rows(directory: Path) -> list[dict[str, str]]:
    manifest = directory / "manifest.csv"
    if not manifest.exists():
        return []
    return read_rows(manifest)[1]


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


def _same_words(text: str) -> str:
    """Spoken words compared by their letters and digits only: "Sam Example." is "sam example"."""
    return " ".join(re.sub(r"[^\w\s]", " ", text).casefold().split())


def _removed_here(clip: object, stems: list[str]) -> str:
    """The removed clip a step's ``clips`` entry names (as it was, or as ``missing/<clip>``)."""
    if not isinstance(clip, str) or "{{" in clip:
        return ""
    name = clip[len("missing/") :] if clip.startswith("missing/") else clip
    return clip_id(name) if clip_id(name) in stems else ""


def _blanked(stem: str) -> str:
    return f"[removed: {stem}]"


def _rewrite_scenario(
    node: Any,
    stems: list[str],
    removal: str,
    *,
    everywhere: bool = False,
    left: list[str] | None = None,
) -> list[str]:
    """Rewrite a scenario for removed clips; ``left`` receives the JSON paths that still hold
    a forgotten answer afterwards (case-insensitive, whole words; never the words themselves).

    A short answer ("Ann") is only blanked beside the step's own field, so the same word
    elsewhere is found by the final search and reported rather than called done. When the
    search finds something and ``everywhere`` is not set, the step's own ``intended`` and
    persona fact are kept, so a rerun (``--blank-short-everywhere``, or after editing by
    hand) still knows what to look for. ``everywhere`` blanks every whole-word match.
    """
    if not isinstance(node, dict):
        return []
    probe = copy.deepcopy(node)
    found: dict[str, tuple[str, str]] = {}
    _rewrite_node(probe, stems, removal, everywhere=everywhere, withhold=False, found=found)
    withhold = bool(_search(probe, found)) and not everywhere
    found = {}
    changed = _rewrite_node(
        node, stems, removal, everywhere=everywhere, withhold=withhold, found=found
    )
    if left is not None:
        left += _search(node, found)
    return changed


def _terms(spoken: dict[str, tuple[str, str]]) -> dict[str, tuple[str, str]]:
    """The answers, and each word of a short multi-word one (a first and a last name)."""
    terms = dict(spoken)
    for words, owner in spoken.items():
        parts = words.split()
        if 1 < len(parts) <= MAX_NAME_WORDS:
            for part in parts:
                if len(part) >= MIN_WORD_LETTERS:
                    terms.setdefault(part, owner)
    return terms


MAX_NAME_WORDS = 3
MIN_WORD_LETTERS = 3
_MARKER = re.compile(r"\[removed: [^\]]*\]")


def _search(node: dict[str, Any], spoken: dict[str, tuple[str, str]]) -> list[str]:
    """JSON paths of every string that still holds a forgotten answer as a whole word."""
    patterns = [_span(words) for words in _terms(spoken)]
    paths: list[str] = []

    def walk(item: Any, path: str) -> None:
        if isinstance(item, dict):
            for key, value in item.items():
                if key not in ("clips", "missing_clips", "field", "tool_arg"):
                    walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(item, list):
            for index, value in enumerate(item):
                walk(value, f"{path}[{index}]")
        elif isinstance(item, str):
            held = _MARKER.sub(" ", item)
            if any(pattern.search(held) for pattern in patterns):
                paths.append(path)

    if patterns:
        walk(node, "")
    return paths


def _rewrite_node(
    node: Any,
    stems: list[str],
    removal: str,
    *,
    everywhere: bool,
    withhold: bool,
    found: dict[str, tuple[str, str]],
) -> list[str]:
    """Point every step that plays a removed clip at ``missing/<clip>``. Returns the clips changed.

    A scenario has ``steps`` and perhaps a ``fallback``; a chain has ``chain``, a
    list of calls each shaped like a scenario. A clip chosen at run time (``{{variable}}``) is
    not named here and is left alone.

    What the clip said goes with it, wherever the scenario keeps it: the step's
    ``intended``, the persona fact it answered, and any expectation (a tool argument or
    output) that holds the same words. The names of the fields stay. A step already
    pointing at ``missing/<clip>`` is looked at again, so a removal that stopped half way,
    or one made before this blanked the words, is finished by running it again.
    """
    if not isinstance(node, dict):
        return []
    changed: list[str] = []
    spoken: dict[str, tuple[str, str]] = {}  # normalised words -> (clip, step field)
    protect: dict[int, set[str]] = {}  # dict id -> keys kept as they are (the sources)
    answers = [*(node.get("steps") or []), node.get("fallback")]
    for answer in answers:
        clips = answer.get("clips") if isinstance(answer, dict) else None
        if not isinstance(answer, dict) or not isinstance(clips, list):
            continue
        gone: list[str] = []
        for index, clip in enumerate(clips):
            stem = _removed_here(clip, stems)
            if not stem:
                continue
            gone.append(stem)
            if not clip.startswith("missing/"):
                clips[index] = f"missing/{stem}"
                changed.append(stem)
                declared = node.setdefault("missing_clips", [])
                if not any(str(d).startswith(f"{stem}: removed") for d in declared):
                    declared.append(_missing_text(stem, removal))
        if gone:
            changed += _blank_words(
                node, answer, gone[0], spoken, protect=protect if withhold else None
            )
    found.update(spoken)
    if spoken:
        changed += _blank_elsewhere(node, spoken, everywhere=everywhere, protect=protect)
    for call in [*(node.get("chain") or []), *(node.get("calls") or [])]:
        changed += _rewrite_node(
            call, stems, removal, everywhere=everywhere, withhold=withhold, found=found
        )
    return changed


def _blank_words(
    node: dict[str, Any],
    answer: dict[str, Any],
    stem: str,
    spoken: dict[str, tuple[str, str]],
    *,
    protect: dict[int, set[str]] | None = None,
) -> list[str]:
    """Blank the step's ``intended`` and the persona fact it answers (its ``field``).

    ``spoken`` collects the words found (normalised, with the clip and the step's field), so
    the same words can be blanked elsewhere in the scenario by ``_blank_elsewhere``.
    """
    changed: list[str] = []
    name = answer.get("field")
    tie = name if isinstance(name, str) else ""
    intended = answer.get("intended")
    if isinstance(intended, str) and intended and intended != _blanked(stem):
        if _same_words(intended):
            spoken[_same_words(intended)] = (stem, tie)
        if protect is not None:
            protect.setdefault(id(answer), set()).add("intended")
        else:
            answer["intended"] = _blanked(stem)
            changed.append(stem)
    persona = node.get("persona")
    facts = persona.get("facts") if isinstance(persona, dict) else None
    if isinstance(facts, dict) and tie and tie in facts:
        held = facts[tie]
        if isinstance(held, str) and held and held != _blanked(stem):
            if _same_words(held):
                spoken[_same_words(held)] = (stem, tie)
            if protect is not None:
                protect.setdefault(id(facts), set()).add(tie)
            else:
                facts[tie] = _blanked(stem)
                changed.append(stem)
    return changed


# A short answer ("yes", "ok") is also what an unrelated check expects. It is blanked only
# where the scenario ties it to the clip's step (the same ``field``), never matched elsewhere.
SPECIFIC_LETTERS = 8


def _specific(words: str) -> bool:
    return len(words.split()) >= 2 or len(words) >= SPECIFIC_LETTERS


def _span(words: str) -> re.Pattern[str]:
    """The words wherever they sit in a longer string, whatever the case or punctuation."""
    parts = [re.escape(w) for w in words.split()]
    return re.compile(r"(?<!\w)" + r"[\W_]+".join(parts) + r"(?!\w)", re.IGNORECASE)


def _blank_elsewhere(
    node: dict[str, Any],
    spoken: dict[str, tuple[str, str]],
    *,
    everywhere: bool = False,
    protect: dict[int, set[str]] | None = None,
) -> list[str]:
    """Blank what a removed clip said wherever else the scenario holds it.

    Every string in the scenario is searched (persona, expect, description, intended, and any
    other field), inside longer strings too: ``My name is <words> thanks`` keeps the rest.
    Specific words (two or more, or eight letters or more) are replaced wherever they are. Short
    ones only where the value sits beside the step's ``field``, since "yes" is also what an
    unrelated check expects. Field names, check names and ``clips`` lists are not touched.
    """
    changed: list[str] = []
    targets = _terms(spoken) if everywhere else spoken

    def text(value: str, tied: str) -> str:
        for words, (stem, tie) in targets.items():
            if not (everywhere or _specific(words) or (tied and tied == tie)):
                continue
            if _same_words(value) == words:
                changed.append(stem)
                return _blanked(stem)
            if everywhere or _specific(words):
                value, count = _span(words).subn(_blanked(stem), value)
                changed.extend([stem] * count)
        return value

    def walk(item: Any, tied: str) -> Any:
        if isinstance(item, dict):
            here = item["field"] if isinstance(item.get("field"), str) else tied
            kept = (
                "clips",
                "missing_clips",
                "field",
                "tool_arg",
                *(protect or {}).get(id(item), ()),
            )
            return {key: value if key in kept else walk(value, here) for key, value in item.items()}
        if isinstance(item, list):
            return [walk(value, tied) for value in item]
        return text(item, tied) if isinstance(item, str) else item

    for key in list(node):
        if key not in ("clips", "missing_clips"):
            node[key] = walk(node[key], "")
    return changed


def _scenario_files(directory: Path) -> list[Path]:
    return sorted(
        p
        for p in directory.rglob("*")
        if p.is_file() and p.suffix.lower() in (".json", ".yaml", ".yml")
    )


def _plan_scenarios(plan: Plan, directories: list[Path], *, everywhere: bool = False) -> None:
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
            left: list[str] = []
            hit = _rewrite_scenario(data, plan.stems, "dry-run", everywhere=everywhere, left=left)
            if hit:
                plan.scenarios[path] = sorted(dict.fromkeys(hit))
            if left:
                plan.leftovers[path] = left


def _result_kind(path: Path, data: Any = None) -> str:
    """``run``, ``chain``, ``batch`` or empty, for a stored result.

    A file is a result by name (``run-NNN``, ``phone-run-NNN``, ``chain-NNN``, ``batch``) or,
    whatever it is called, by the shape of a stored call: a ``record`` that is a RunRecord
    (it has ``scenario`` and ``said``). A file with only a ``grade`` or ``trace`` key (a notes
    file, say) is not a result; ``forget`` lists it if it mentions a clip and leaves it.
    """
    name = path.name
    if not name.endswith(".json"):
        return ""
    if name == "batch.json":
        return "batch"
    if name.startswith(("run-", "phone-run-")) and name[:-5].count("-") in (1, 2):
        return "run"
    if name.startswith("chain-") and name[:-5].count("-") == 1:
        return "chain"
    if isinstance(data, dict) and "steps" not in data and "chain" not in data:
        if isinstance(data.get("calls"), list) and "variables" in data:
            return "chain"
        record = data.get("record")
        if isinstance(record, dict) and "scenario" in record and "said" in record:
            return "run"
    return ""


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
        if part == "grade" and isinstance(out.get(part), dict):
            out[part] = _scrub_grade(out[part], marker, found)
        elif part in out:
            out[part] = _scrub(
                out[part], marker, part, found, keep=not part.startswith("read_back")
            )
    _scrub_chain(out, marker, found)
    return out, sorted(found)


def _scrub_grade(grade: dict[str, Any], marker: str, found: set[str]) -> dict[str, Any]:
    """The grade with its text redacted but each check still named.

    A check's ``what`` can hold what was said or expected, so it is replaced by the check's
    ``label`` (its kind and the field or tool it is about, no values) and the label stays.
    A check stored without a label cannot be named safely, and is redacted whole.
    """
    rest = {k: v for k, v in grade.items() if k != "checks"}
    out = _scrub(rest, marker, "grade", found)
    checks = grade.get("checks")
    if not isinstance(checks, list):
        return {**out, **({"checks": checks} if "checks" in grade else {})}
    held: list[Any] = []
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            held.append(_scrub(check, marker, f"grade.checks[{index}]", found))
            continue
        others = {k: v for k, v in check.items() if k not in ("what", "label")}
        row = _scrub(others, marker, f"grade.checks[{index}]", found)
        label, what = check.get("label"), check.get("what")
        if "label" in check:
            row["label"] = label
        if isinstance(label, str) and label:
            row["what"] = label
            if _is_text(what) and what != label:
                found.add("grade.checks[*].what")
        elif _is_text(what):
            row["what"] = marker
            found.add("grade.checks[*].what")
        elif "what" in check:
            row["what"] = what
        held.append(row)
    # Keep the grade's own key order, with the checks where they were.
    return {k: (held if k == "checks" else out.get(k, v)) for k, v in grade.items()}


def _is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and not _is_redacted(value)


def _scrub_chain(out: dict[str, Any], marker: str, found: set[str]) -> None:
    """A chain result's captured values and free-text reasons, changed in place on ``out``.

    The variable names stay (they say what the chain carries); the values are what was
    said on the call, taken from its trace.
    """
    variables = out.get("variables")
    if isinstance(variables, dict):
        held = {k: marker if _is_text(v) else v for k, v in variables.items()}
        if held != variables:
            found.add("variables")
        out["variables"] = held
    if _is_text(out.get("reason")):
        out["reason"] = marker
        found.add("reason")
    calls = out.get("calls")
    if isinstance(calls, list):
        rows: list[Any] = []
        for row in calls:
            if isinstance(row, dict) and _is_text(row.get("reason")):
                row = {**row, "reason": marker}
                found.add("calls[*].reason")
            rows.append(row)
        out["calls"] = rows


def _text_fields(data: dict[str, Any]) -> list[str]:
    """The fields of this result that still hold text (none once it is redacted)."""
    return _redacted_result(data, "x")[1]


def _plan_results(plan: Plan, directories: list[Path], *, keep_text: bool) -> None:
    stems = plan.stems
    for directory in directories:
        marked_runs: dict[str, list[str]] = {}
        files = sorted(p for p in directory.rglob("*.json") if p.is_file())
        loaded = {f: _read_json(f) for f in files}
        kinds = {f: _result_kind(f, loaded[f]) for f in files}
        for path in sorted(f for f in files if kinds[f] == "run"):
            data = loaded[path]
            if not isinstance(data, dict):
                continue
            used = _mentioned(_strings(data), stems)
            if used:
                marked_runs[path.name] = used
                _sound_files(plan, path, data)
                _call_audio(plan, path, data, keep_text=keep_text)
            done = {str(m.get("clip")) for m in data.get(MARK, []) if isinstance(m, dict)}
            todo = [s for s in used if s not in done]
            fields = _text_fields(data) if used and not keep_text else []
            if todo or fields:
                plan.results[path] = todo
            if fields:
                plan.redact[path] = fields
        for path in files:
            if kinds[path] in ("", "run"):
                continue
            data = loaded[path]
            if not isinstance(data, dict):
                continue
            used = _mentioned(_strings(data), stems)
            if kinds[path] == "chain":
                carried: list[str] = []
                for row in data.get("calls") or []:
                    run = row.get("run") if isinstance(row, dict) else None
                    if carried and run:
                        _plan_downstream(plan, path.parent / str(run), carried, keep_text)
                    carried += marked_runs.get(str(run), [])
                used += carried
            used = list(dict.fromkeys(used))
            done = {str(m.get("clip")) for m in data.get(MARK, []) if isinstance(m, dict)}
            todo = [s for s in used if s not in done]
            chain_fields = (
                _text_fields(data) if used and not keep_text and kinds[path] == "chain" else []
            )
            if todo or chain_fields:
                plan.results[path] = todo
            if chain_fields:
                plan.redact[path] = chain_fields
        for path in files:
            if not kinds[path] and _mentioned(_strings(loaded[path]), stems):
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


def _plan_downstream(plan: Plan, run: Path, carried: list[str], keep_text: bool) -> None:
    """A call after a forgotten one in the same chain was given what that call captured.

    Its stored scenario, trace arguments and checks hold those values, so it is marked and
    redacted like the forgotten call itself (the chain's later calls cannot be told apart
    from its values by text alone).
    """
    data = _read_json(run)
    if not isinstance(data, dict):
        return
    done = {str(m.get("clip")) for m in data.get(MARK, []) if isinstance(m, dict)}
    todo = [c for c in dict.fromkeys(carried) if c not in done]
    fields = [] if keep_text else _text_fields(data)
    if todo or fields:
        plan.results[run] = sorted({*plan.results.get(run, []), *todo})
    if fields:
        plan.redact[run] = fields


class NotDeleted(Exception):  # noqa: N818 - a plain statement of what was left, not an error class
    """A clip's audio that ``forget`` will not delete, and why."""


class NotRestamped(Exception):  # noqa: N818 - a plain statement of what was left, not an error class
    """A result that ``forget`` changed but will not vouch for, and why."""


def _outside(path: Path, banks: list[Path]) -> bool:
    here = path.resolve()
    return not any(here.is_relative_to(bank.resolve()) for bank in banks)


def _delete_clip(path: Path, banks: list[Path], stems: list[str]) -> None:
    """Delete a clip's audio. A symlink's target goes too, if it is inside a bank.

    Unlinking a link frees nothing: the audio is still on disk under its real name. A target
    outside every bank is somebody else's file, so nothing is touched, and it is said plainly.
    """
    if path.is_symlink():
        target = path.resolve()
        if target.exists() and _outside(path, banks):
            raise NotDeleted(f"not deleted: {target} is outside the bank (linked from {path})")
        if target.exists() and clip_id(target.name) not in stems:
            # Somebody else's clip: only the link goes.
            print(
                f"forget: target {target} belongs to clip {clip_id(target.name)}, not removed "
                f"(only the link {path} was)"
            )
        else:
            target.unlink(missing_ok=True)
    path.unlink(missing_ok=True)


def _tracks(data: Any) -> list[dict[str, Any]]:
    """Every ``audio`` entry of a stored run: ``{"caller": "audio/x.caller.wav", "agent": ...}``."""
    found: list[dict[str, Any]] = []
    if isinstance(data, dict):
        if isinstance(data.get("caller"), str) and isinstance(data.get("agent"), str):
            found.append(data)
        for value in data.values():
            found += _tracks(value)
    elif isinstance(data, list):
        for value in data:
            found += _tracks(value)
    return found


def _call_audio(plan: Plan, run: Path, data: Any, *, keep_text: bool) -> None:
    """The recorded call's caller track goes with the clip; the agent track is only listed."""
    for entry in _tracks(data):
        caller = (run.parent / str(entry["caller"])).resolve()
        agent = (run.parent / str(entry["agent"])).resolve()
        here = run.parent.resolve()
        if not (caller.is_relative_to(here) and agent.is_relative_to(here)):
            continue  # never touch a path that leaves the results folder (or enters "out2")
        if caller.is_file():
            target = plan.recordings if keep_text else plan.call_audio
            if caller not in target:
                target.append(caller)
        if agent.is_file() and agent not in plan.recordings:
            plan.recordings.append(agent)


def _sound_files(plan: Plan, run: Path, data: Any) -> None:
    for text in _strings(data):
        if text.lower().endswith((".wav", ".mp3")) and "/" not in text:
            for found in run.parent.rglob(text):
                if found not in plan.recordings:
                    plan.recordings.append(found)


def _plan_cases(plan: Plan, banks: list[Path], *, keep_text: bool) -> None:
    """An ingested case folder holds the whole call a clip was cut from.

    ``ingest`` writes the clips to ``<case>/clips``, beside ``audio.*``, ``transcript.txt``
    and ``conversation.json``. Forgetting a clip from that bank forgets the call: its audio
    goes; its transcript and conversation go too, or are listed and kept under ``--keep-text``.
    """
    for bank in banks:
        case = bank.parent
        # capture.json is what stays when a run that died took the conversation and audio:
        # the case is still the case, and its scenarios still hold the words.
        if bank.name != CLIPS_DIR or not (case / CAPTURE_FILE).is_file():
            continue
        came_from_here = (
            any(p.is_relative_to(bank) for p in plan.audio)
            or (bank / "manifest.csv") in plan.rows
            or any(stem in plan.stems for stem in removals.read_removed([bank]))
        )
        if not came_from_here:
            continue
        if (case / SCENARIO_DIR).is_dir():
            plan.case_scenarios.append(case / SCENARIO_DIR)
        for path in sorted(case.glob("audio.*")):
            if path.is_file() or path.is_symlink():
                plan.case_files.append(path)
        # Every file a capture writes except capture.json (ids, times and hashes, no words),
        # so a file added to capture later is not missed.
        for name in sorted(_written_names() - {CAPTURE_FILE}):
            path = case / name
            if path.exists():
                (plan.case_kept if keep_text else plan.case_files).append(path)


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
    everywhere: bool = False,
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
    _plan_cases(plan, banks, keep_text=keep_text)
    _plan_sources(plan, banks, recordings or [])
    given = {d.resolve() for d in scenarios}
    _plan_scenarios(
        plan,
        [*scenarios, *(d for d in plan.case_scenarios if d.resolve() not in given)],
        everywhere=everywhere,
    )
    scanned = [d.resolve() for d in [*scenarios, *plan.case_scenarios]]
    plan.unscanned = _unscanned(banks, scanned)
    _plan_results(plan, results, keep_text=keep_text)
    return plan


def _unscanned(banks: list[Path], scanned: list[Path]) -> list[Path]:
    """``scenarios`` directories up to three levels under a bank's parent that nobody scanned.

    A scenario kept in one of them may hold the words a removed clip said; ``forget`` only
    rewrites the directories it was given, so these are named rather than left silent.
    """
    found: list[Path] = []
    for bank in banks:
        root = bank.parent
        for pattern in (SCENARIO_DIR, f"*/{SCENARIO_DIR}", f"*/*/{SCENARIO_DIR}"):
            for path in sorted(root.glob(pattern)):
                relative = path.relative_to(root)
                if any(part.startswith(".") for part in relative.parts) or not path.is_dir():
                    continue
                resolved = path.resolve()
                if resolved.is_relative_to(bank.resolve()) or any(
                    resolved.is_relative_to(d) for d in scanned
                ):
                    continue
                if path not in found:
                    found.append(path)
    return found


def describe(
    plan: Plan, *, dry: bool, keep_text: bool = False, finishing: list[str] | None = None
) -> list[str]:
    verb = "would" if dry else "will"
    lines = [
        f"{'DRY RUN: nothing is changed. ' if dry else ''}forget {plan.target}: "
        f"{len(plan.stems)} clip(s)" + (f", speaker(s) {plan.speakers}" if plan.speakers else "")
    ]
    lines += [f"  clip {s}" for s in plan.stems]
    for p in plan.audio:
        if p.is_symlink() and p.resolve().exists():
            target = p.resolve()
            lines.append(
                f"  {verb} delete audio: {p} (a link: its target {target} goes too if it is "
                "inside the bank; if not, it is left and reported)"
            )
        else:
            lines.append(f"  {verb} delete audio: {p}")
    lines += [f"  {verb} delete caller-side call audio: {p}" for p in plan.call_audio]
    lines += [
        f"  {verb} delete the whole call these clips were cut from: {p}" for p in plan.case_files
    ]
    lines += [
        f"  --keep-text: the call's text is KEPT, delete it if the request covers it: {p}"
        for p in plan.case_kept
    ]
    lines += [f"  {verb} remove manifest row: {p} ({', '.join(c)})" for p, c in plan.rows.items()]
    lines += [f"  {verb} remove override: {p} ({', '.join(c)})" for p, c in plan.overrides.items()]
    lines += [
        f"  {verb} make a scenario MISSING CLIP: {p} ({', '.join(c)})"
        for p, c in plan.scenarios.items()
    ]
    if plan.scenarios:
        lines.append(
            "  what the removed clips said is blanked wherever the scenario holds it, inside "
            "longer text too; a short answer (one word under eight letters) only where the "
            "scenario ties it to the step. The names of the fields and checks stay. "
            "NOT removed: any other persona fact, "
            "which may still hold what the caller said. Remove or edit persona facts by hand in:"
        )
        lines += [f"    {p}" for p in plan.scenarios]
    lines += [f"  CANNOT rewrite {p} ({why}); it names a clip" for p, why in plan.unreadable]
    for path, where in plan.leftovers.items():
        lines.append(
            f"  STILL HOLDS a forgotten answer after the rewrite: {path} at {', '.join(where)} "
            "(blank them with --blank-short-everywhere, or edit by hand); not complete until gone"
        )
    if plan.unscanned:
        lines.append(
            "  WARNING: scenario directories found here were NOT scanned (a scenario in one may "
            "still hold what the removed clips said; name each with --scenarios and run again):"
        )
        lines += [f"    {p}" for p in plan.unscanned]
    for path, clips in plan.results.items():
        lines.append(f"  {verb} mark result with {MARK}: {path} ({', '.join(clips)})")
        if path in plan.redact:
            lines.append(f"    {verb} redact: {', '.join(plan.redact[path])}")
    if plan.redact:
        lines.append(f"  {KEPT_NOTE}")
        lines.append(
            "  NOT redacted: batch summaries (marked only), uploads, scorecards, "
            "acceptance pages, the agent track of recorded calls, carrier and engine recordings"
        )
    if keep_text:
        lines.append(
            "  --keep-text: nothing in stored results is redacted; each result keeps what was "
            "said and heard, and its mark says redacted: false"
        )
    lines += [f"  mentions {', '.join(plan.stems)}, not changed: {p}" for p in plan.mentioned]
    if plan.recordings:
        lines.append(
            "  call recordings of calls that played these clips (NOT deleted: the agent "
            "track of a recorded call, or every recording with --keep-text; delete them if "
            f"the request covers them): {len(plan.recordings)}"
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
    if not plan.changes and finishing:
        lines += [f"  finishing earlier unfinished removal {removal}" for removal in finishing]
    elif not plan.changes:
        lines.append("  nothing to change: already removed, or no stored file uses these clips")
    lines.append(UPLOAD_REMINDER)
    if plan.uploaded:
        lines.append("  uploaded directories: " + ", ".join(str(p.parent) for p in plan.uploaded))
    return lines


def _remove_rows(path: Path, stems: list[str]) -> None:
    names, rows = read_rows(path)  # a column written twice comes back once, or refuses
    kept = [r for r in rows if clip_id(r.get("clip") or "") not in stems]
    temporary = path.with_name(path.name + ".forget-tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=names)
        writer.writeheader()
        writer.writerows(kept)
    os.replace(temporary, path)


def _rewrite_file(path: Path, stems: list[str], removal: str, everywhere: bool = False) -> None:
    data, text = _load_scenario(path)
    _rewrite_scenario(data, stems, removal, everywhere=everywhere)
    _write_atomic(path, _dump_scenario(path, data, text))


def altered_results(plan: Plan) -> dict[Path, str]:
    """The result files this plan would mark that are not what the rig wrote: file -> why."""
    found: dict[Path, str] = {}
    for path in plan.results:
        state = verdict(path, json.loads(path.read_text(encoding="utf-8")))
        if state in (ALTERED, "stripped"):
            found[path] = (
                "it was edited after the rig wrote it (its digest does not match)"
                if state == ALTERED
                else "its integrity digest was removed after the rig wrote it"
            )
    return found


def _mark_result(
    path: Path,
    plan: Plan,
    removal: str,
    day: str,
    todo: list[str],
    *,
    wipe: bool,
    cleared: dict[str, list[str]] | None = None,
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
    state = verdict(path, json.loads(path.read_text(encoding="utf-8")))
    if state in (ALTERED, "stripped") and cleared is not None:
        # --erase-altered: a field the rig does not write may hold what was said; clear it
        hand = sorted(k for k in data if k not in RIG_FIELDS)
        for key in hand:
            del data[key]
        if hand:
            cleared[_label(path)] = hand
    if state == "unstamped":
        write_json(path, data)  # written before digests existed: nothing to vouch for or lose
        return
    if state != "ok":
        # A digest that does not match, or one that was removed, means the file is not what the
        # rig wrote. Erasure is a duty, so the values are still blanked, but stamping the file
        # now would make an edited result read as untouched: leave its digest exactly as it was.
        write_json(path, data)
        raise NotRestamped(
            f"{_label(path)}: erasure is done (the values are blanked); only the integrity "
            "digest was left untouched, because the file "
            + ("was edited" if state == ALTERED else "had its digest removed")
            + " after the rig wrote it, so it still reads as altered for gate, acceptance and "
            "compare"
        )
    write_run(path, data)


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
    verify: Callable[[], list[str]] | None = None,
    everywhere: bool = False,
    erase_altered: bool = False,
) -> tuple[str, list[str], list[str], dict[str, list[str]]]:
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
    cleared: dict[str, list[str]] = {}  # hand-added fields removed from altered files
    untouched: list[str] = []  # erased, but already altered: the digest was left as it was
    done = {"audio_files": 0, "manifest_rows": 0, "overrides": 0, "scenarios": 0, "results": 0}

    def attempt(
        what: str, path: Path, action: Callable[[], None], counter: str, n: int = 1
    ) -> None:
        try:
            action()
        except NotRestamped as error:
            untouched.append(str(error))  # the work is done: not a step that failed
            done[counter] += n
        except Exception as error:  # noqa: BLE001 - one file failing must not stop the others
            failed.append(
                str(error)
                if isinstance(error, NotDeleted)
                else f"{what} {_label(path)}: {type(error).__name__}"
            )
        else:
            done[counter] += n

    for path in plan.scenarios:
        attempt(
            "rewrite",
            path,
            partial(_rewrite_file, path, plan.stems, removal, everywhere),
            "scenarios",
        )
    for audio in plan.audio:
        attempt("delete", audio, partial(_delete_clip, audio, banks, plan.stems), "audio_files")
    for audio in plan.call_audio:
        attempt("delete", audio, partial(audio.unlink, missing_ok=True), "audio_files")
    for audio in plan.case_files:
        attempt("delete", audio, partial(audio.unlink, missing_ok=True), "audio_files")
    for path, why_not in plan.unreadable:
        failed.append(f"rewrite {_label(path)}: {why_not} (it names a clip and was not changed)")
    for path, todo in plan.results.items():
        wipe = path in plan.redact
        attempt(
            "mark",
            path,
            partial(
                _mark_result,
                path,
                plan,
                removal,
                day,
                todo,
                wipe=wipe,
                cleared=cleared if erase_altered else None,
            ),
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
    if verify is not None:
        # "complete" is only written when a fresh plan finds nothing left to do
        try:
            failed += verify()
        except Exception as error:  # noqa: BLE001 - a check that cannot run is not a pass
            failed.append(f"re-check after the run: {type(error).__name__}: {error}")
    removals.append(
        log,
        {
            **base,
            "outcome": "incomplete" if failed else "complete",
            "failed": failed,
            "digest_untouched": untouched,
            "erase_altered": erase_altered,
            "cleared_fields": cleared,
            "effects": {
                **done,
                "scenario_files": sorted(p.name for p in plan.scenarios),
                "result_files": sorted(p.name for p in plan.results),
            },
        },
    )
    return removal, failed, untouched, cleared


def _left_to_do(plan: Plan) -> list[str]:
    """What a plan still has to do, one line per kind; empty when there is nothing."""
    kinds = {
        "audio file(s) still present": len(plan.audio)
        + len(plan.call_audio)
        + len(plan.case_files),
        "scenario file(s) still holding the words": len(plan.scenarios),
        "stored result(s) not yet marked": len(plan.results),
        "manifest row(s) still present": sum(map(len, plan.rows.values())),
        "override(s) still present": sum(map(len, plan.overrides.values())),
    }
    lines = [f"{count} {what}" for what, count in kinds.items() if count]
    lines += [
        f"a forgotten answer still in {path} at {', '.join(where)}"
        for path, where in plan.leftovers.items()
    ]
    return lines


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
            everywhere=bool(getattr(args, "blank_short_everywhere", False)),
        )
    except (ForgetError, ManifestError) as error:
        suffix = "; nothing was changed" if isinstance(error, ManifestError) else ""
        print(f"forget: {error}{suffix}")
        return 2
    pending = [
        r
        for r in removals.unfinished(banks)
        if {clip_id(str(c)) for c in r["clips"]} <= set(plan.stems)
    ]
    for line in describe(
        plan,
        dry=args.dry_run,
        keep_text=args.keep_text,
        finishing=[str(r["removal"]) for r in pending],
    ):
        print(line)
    erase_altered = bool(getattr(args, "erase_altered", False))
    refused = altered_results(plan)
    if refused and not erase_altered:
        # Nothing has been written yet and nothing will be: erasing first and reporting after
        # would destroy the evidence of what was edited.
        print(f"forget: REFUSED: {len(refused)} result file(s) are not what the rig wrote:")
        for path, why in refused.items():
            print(f"  {_label(path)}: {why}")
        print(
            "Nothing was changed: no audio removed, no scenario rewritten, no value blanked. "
            "Find out who changed them and why, then regenerate them (rerun the calls) or keep "
            "them out of --results. Only the owner may override this: --erase-altered erases "
            "anyway, blanks the forgotten values and clears every field the rig does not "
            "write in those files (each is listed), and leaves their digest untouched."
        )
        return 1
    if args.dry_run:
        return 0
    if not (plan.changes or pending):
        return _still_open(banks)

    everywhere = bool(getattr(args, "blank_short_everywhere", False))

    def recheck() -> list[str]:
        again = make_plan(
            args.target,
            banks,
            [Path(p) for p in args.scenarios],
            [Path(p) for p in args.results],
            keep_text=args.keep_text,
            recordings=[Path(p) for p in args.recordings],
            everywhere=everywhere,
        )
        return [f"left to do after the run: {line}" for line in _left_to_do(again)]

    try:
        removal, failed, untouched, cleared = execute(
            plan,
            banks,
            operator=args.operator,
            why=args.reason,
            request_ref=args.request_ref,
            keep_text=args.keep_text,
            verify=recheck,
            everywhere=everywhere,
            erase_altered=erase_altered,
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
    for name, fields in cleared.items():
        print(f"forget: cleared hand-added field(s) from {name}: {', '.join(fields)}")
    if untouched:
        print(
            f"forget: {len(untouched)} result file(s) were already altered, so their integrity "
            "digest was left untouched (the erasure itself is done; nothing is left to rerun):"
        )
        print("\n".join(f"  {line}" for line in untouched))
        _still_open(banks)
        return 1
    return _still_open(banks)


def _still_open(banks: list[Path]) -> int:
    """1, naming each, when an earlier removal is still unfinished; otherwise 0."""
    lines = removals.unfinished_lines(banks)
    for line in lines:
        print(f"forget: {line}")
    return 1 if lines else 0


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
        "--blank-short-everywhere",
        action="store_true",
        help="also blank every whole-word match of a forgotten answer (and each word of a short "
        "name) anywhere in a scenario; without it a short answer is only blanked beside its "
        "own step, and a match left elsewhere is reported and the run is not complete",
    )
    parser.add_argument(
        "--erase-altered",
        action="store_true",
        help="OWNER ONLY: without it forget refuses, changing nothing, when a result file it "
        "would mark was edited or lost its digest. With it forget erases anyway, clears every "
        "field the rig does not write in those files (listed) and leaves their digest alone",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="list everything it would touch; change nothing"
    )
    parser.set_defaults(handler=run)
