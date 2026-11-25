"""What produced a directory of runs: the rig, the scenarios, the clips and the target.

A pass rate means nothing once it can no longer be tied to the scenarios that
were asked, the audio that was played, the version of the rig that graded it
and the system it was pointed at. Every batch therefore writes an entry into
``manifest.json`` in its run directory before it places a call, and closes it
when it ends:

    {"version": 1, "entries": [
      {"kind": "batch", "id": "b001", "started": "...", "ended": "...",
       "argv": ["batch", "..."],
       "rig": {"version": "0.1.0", "commit": "<sha>", "dirty": false},
       "scenarios": {"directory": "set-a", "files": {"01-x.json": "<sha256>"},
                     "set_sha256": "<sha256>"},
       "clips": {"banks": [{"directory": "clips", "manifest_sha256": "...",
                            "overrides_sha256": "...", "clips": 938}],
                 "played": 14, "played_sha256": "...", "unavailable": []},
       "target": {"label": "", "version": "", "agent": "", "leg": "", "trace": "",
                  "profile": {"file": "", "name": "", "sha256": ""}},
       "rehearsal": false, "seed": null, "order": "file name order, not shuffled"},
      {"kind": "regrade", "id": "g001", "at": "...", "rig": {...},
       "scenarios": {...}, "changed": 3}
    ]}

A directory can hold more than one batch, so every stored run carries the id of
the entry it was made under (``"batch": "b001"``). Entries are kept in the order
they happened. A ``regrade`` grades the stored calls again against the scenarios
as they are now, so from then on those calls answer to the regrade's scenario
hashes, not to the ones they were placed under.

Text files are hashed with their line endings normalised, so the same scenario
checked out on Windows and on Linux has one hash. Audio is hashed as it is.

Nothing here reads the network. A target's version is what the operator stated
(``--target-version``); the rig does not ask the platform for one.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from synthiq_caller.clips import OVERRIDES_FILE, ClipBank
from synthiq_caller.jsonio import write_json

MANIFEST_FILE = "manifest.json"
VERSION = 1
# The hash given to a run whose scenario version nobody recorded.
UNRECORDED = "unrecorded"
ORDER = "file name order, not shuffled"
_TEXT = {".json", ".yaml", ".yml", ".csv", ".toml", ".txt"}


class MixedScenarios(ValueError):
    """Runs graded against different versions of the same scenario file."""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def file_hash(path: Path) -> str:
    """SHA-256 of a file; a text file's line endings do not change it."""
    data = path.read_bytes()
    if path.suffix.lower() in _TEXT:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def set_hash(hashes: dict[str, str]) -> str:
    """One hash for a set of named hashes, whatever order they were gathered in."""
    lines = "".join(f"{name}\n{value}\n" for name, value in sorted(hashes.items()))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def scenario_identity(directory: str | Path, names: list[str]) -> dict[str, Any]:
    """The scenario and chain files a batch was given, each with its hash."""
    root = Path(directory)
    files = {name: file_hash(root / name) for name in sorted(set(names))}
    return {"directory": root.name, "files": files, "set_sha256": set_hash(files)}


def clip_identity(bank: ClipBank, played: list[str]) -> dict[str, Any]:
    """Which clip bank this was, and a hash of the audio the scenarios name."""
    banks = []
    for folder in bank.directories:
        sheet, overrides = folder / "manifest.csv", folder / OVERRIDES_FILE
        banks.append(
            {
                "directory": folder.name,
                "manifest_sha256": file_hash(sheet) if sheet.exists() else "",
                "overrides_sha256": file_hash(overrides) if overrides.exists() else "",
                "clips": sum(1 for _ in folder.glob("*.wav")),
            }
        )
    hashes: dict[str, str] = {}
    unavailable: list[str] = []
    for name in sorted(set(played)):
        try:
            hashes[name] = file_hash(bank.path(name))
        except FileNotFoundError:  # not in the bank, or ruled out by a person
            unavailable.append(name)
    return {
        "banks": banks,
        "played": len(hashes),
        "played_sha256": set_hash(hashes),
        "unavailable": unavailable,
    }


def _git(*args: str) -> str:
    try:
        done = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def rig_identity() -> dict[str, Any]:
    """The rig's version and the commit it was run from.

    ``SYNTHIQ_RIG_COMMIT`` names the commit when the rig runs from an installed
    package with no checkout around it. Without either, the commit is "unknown"
    and reports say so.
    """
    try:
        version = metadata.version("synthiq-voice-caller")
    except metadata.PackageNotFoundError:
        version = "unknown"
    stated = os.environ.get("SYNTHIQ_RIG_COMMIT", "").strip()
    if stated:
        return {"version": version, "commit": stated, "dirty": False}
    commit = _git("rev-parse", "HEAD")
    if not commit:
        return {"version": version, "commit": "unknown", "dirty": False}
    # Changes to the rig's own source that no commit holds.
    dirty = bool(_git("status", "--porcelain", "--", "."))
    return {"version": version, "commit": commit, "dirty": dirty}


def target_identity(
    *,
    label: str = "",
    version: str = "",
    agent: str = "",
    leg: str = "",
    trace: str = "",
    profile_path: str = "",
) -> dict[str, Any]:
    profile = {"file": "", "name": "", "sha256": ""}
    if profile_path:
        path = Path(profile_path)
        data = json.loads(path.read_text(encoding="utf-8"))
        name = str(data.get("name", "")) if isinstance(data, dict) else ""
        profile = {"file": path.name, "name": name, "sha256": file_hash(path)}
    return {
        "label": label,
        # Stated by whoever ran the batch; the rig asks no platform for it.
        "version": version,
        "agent": agent,
        "leg": leg,
        "trace": trace,
        "profile": profile,
    }


def load(directory: str | Path) -> dict[str, Any] | None:
    """The directory's manifest, or None when it has none (runs made before there was one)."""
    path = Path(directory) / MANIFEST_FILE
    if not path.exists():
        return None
    data: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != VERSION:
        raise ValueError(f"{path}: not a version {VERSION} run manifest")
    if not isinstance(data.get("entries"), list):
        raise ValueError(f"{path}: a manifest holds a list of entries")
    return data


def _save(directory: Path, manifest: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / MANIFEST_FILE, manifest)


def _next_id(manifest: dict[str, Any], prefix: str) -> str:
    used = [
        int(str(e.get("id", ""))[1:])
        for e in manifest["entries"]
        if str(e.get("id", "")).startswith(prefix) and str(e.get("id", ""))[1:].isdigit()
    ]
    return f"{prefix}{max(used, default=0) + 1:03d}"


def open_batch(
    directory: str | Path,
    *,
    argv: list[str],
    scenarios: dict[str, Any],
    clips: dict[str, Any],
    target: dict[str, Any],
    rehearsal: bool = False,
    repeats: int = 1,
    allow_mutating: str = "none",
    seed: int | None = None,
    started: str = "",
) -> str:
    """Record a batch that is about to start; returns the id its runs must carry."""
    root = Path(directory)
    manifest = load(root) or {"version": VERSION, "entries": []}
    entry_id = _next_id(manifest, "b")
    manifest["entries"].append(
        {
            "kind": "batch",
            "id": entry_id,
            "started": started or now(),
            "ended": None,
            "argv": list(argv),
            "rig": rig_identity(),
            "scenarios": scenarios,
            "clips": clips,
            "target": target,
            "rehearsal": rehearsal,
            "repeats": repeats,
            "allow_mutating": allow_mutating,
            # Scenarios run in file name order; there is nothing to seed.
            "seed": seed,
            "order": ORDER,
        }
    )
    _save(root, manifest)
    return entry_id


def close_batch(directory: str | Path, entry_id: str, *, ended: str = "") -> None:
    root = Path(directory)
    manifest = load(root)
    if manifest is None:
        return
    for entry in manifest["entries"]:
        if entry.get("id") == entry_id:
            entry["ended"] = ended or now()
    _save(root, manifest)


def record_regrade(
    directory: str | Path, *, scenarios: dict[str, Any], changed: int, argv: list[str]
) -> str:
    """Record that the stored calls were graded again, and against which scenarios."""
    root = Path(directory)
    manifest = load(root) or {"version": VERSION, "entries": []}
    entry_id = _next_id(manifest, "g")
    manifest["entries"].append(
        {
            "kind": "regrade",
            "id": entry_id,
            "at": now(),
            "argv": list(argv),
            "rig": rig_identity(),
            "scenarios": scenarios,
            "changed": changed,
        }
    )
    _save(root, manifest)
    return entry_id


def _runs(directory: Path) -> list[tuple[str, str, str]]:
    """(run file, scenario file, batch id) for every stored run."""
    found = []
    for path in sorted(directory.glob("run-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        found.append(
            (path.name, str(payload.get("scenario_file", "")), str(payload.get("batch", "")))
        )
    return found


def run_hashes(directory: str | Path) -> dict[str, dict[str, int]]:
    """For each scenario file with stored runs: the scenario hashes they answer to.

    ``{scenario file: {hash: number of runs}}``. A run answers to the scenario
    of the last regrade that covered its file, or else to the scenario it was
    placed under. A run nobody recorded either for answers to ``UNRECORDED``.
    """
    root = Path(directory)
    manifest = load(root)
    entries = manifest["entries"] if manifest else []
    position = {str(e.get("id")): index for index, e in enumerate(entries)}
    found: dict[str, dict[str, int]] = {}
    for _, file, batch in _runs(root):
        placed = position.get(batch, -1)
        answer = UNRECORDED
        if placed >= 0:
            answer = entries[placed].get("scenarios", {}).get("files", {}).get(file, UNRECORDED)
        for entry in entries[placed + 1 :]:
            covered = entry.get("scenarios", {}).get("files", {})
            if entry.get("kind") == "regrade" and file in covered:
                answer = covered[file]
        tally = found.setdefault(file, {})
        tally[answer] = tally.get(answer, 0) + 1
    return found


def mixed(directories: list[Path] | list[str]) -> dict[str, list[str]]:
    """Scenario files whose runs, across ``directories``, answer to more than one hash."""
    seen: dict[str, set[str]] = {}
    for directory in directories:
        for file, tally in run_hashes(directory).items():
            seen.setdefault(file, set()).update(tally)
    return {file: sorted(hashes) for file, hashes in sorted(seen.items()) if len(hashes) > 1}


def refuse_mixed(
    directories: list[Path] | list[str],
    *,
    allow: bool = False,
    remedy: str = "Regrade them against one version, or leave the older runs out",
) -> dict[str, list[str]]:
    """Raise ``MixedScenarios`` when runs of one scenario file answer to different versions of it.

    Returns what was mixed (empty when nothing was), for a caller that was told
    to go on and must still say so.
    """
    found = mixed(directories)
    if found and not allow:
        detail = "; ".join(
            f"{file}: {', '.join(h[:12] for h in hashes)}" for file, hashes in found.items()
        )
        raise MixedScenarios(
            "these runs were made against different versions of the same scenario file, so "
            f"they are not one test ({detail}). {remedy}; or pass --allow-mixed-scenarios to "
            "go on and have it recorded."
        )
    return found
