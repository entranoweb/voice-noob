"""Was every planned call recorded, and is every recorded file still as the rig wrote it?

A pass rate over the calls that happen to be in a directory says nothing about
the calls that should be. A batch stopped half-way, a run file deleted, an
unreadable file left out: each leaves a smaller directory that grades as well
as a whole one. This module is the one check every command that reads a run
directory uses, so that none has its own idea of "complete".

Three things are compared:

* planned: ``manifest.json`` records, before the first call, every call a batch
  means to place (``plan``: a scenario or chain file and a repeat number);
* recorded: a run file, a chain file, or a held-back record that answers to it;
* readable: every run, chain, manifest and batch file parses as what it is.

The answer is one of four states:

* ``complete``: every planned call is accounted for.
* ``incomplete``: planned calls never ran, or a recorded file is missing.
* ``altered``: a run file no longer matches the digest the rig stamped on it.
* ``unknown``: the directory was made before plans were recorded (or by a
  command that makes none). A missing call cannot be seen, and it says so.

An unreadable file is not a state: it raises ``Unreadable``, and the command
stops with the file's name. A corrupt file is never skipped.

The integrity digest catches an accidental edit of a stored run (a hand edit of
``grade.outcome``, a half-finished script). It sits in the same file it
protects, so anyone who means to alter a run can recompute it. It is not
tamper-proofing and is not described as such.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

INTEGRITY = "integrity"
COMPLETE = "complete"
INCOMPLETE = "incomplete"
ALTERED = "altered"
UNKNOWN = "unknown"
SHOWN = 4  # problems listed before the rest are only counted


class Unreadable(ValueError):
    """A file the rig needs that cannot be read as what it should be."""


def read_json(path: Path) -> Any:
    """Parse a JSON file, or raise ``Unreadable`` naming it and saying why."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise Unreadable(f"{path}: cannot be read ({error.__class__.__name__}: {error})") from error
    if not text.strip():
        raise Unreadable(f"{path}: the file is empty (an interrupted write?)")
    try:
        return json.loads(text)
    except ValueError as error:
        raise Unreadable(f"{path}: not valid JSON ({error}); truncated or corrupt?") from error


def read_object(path: Path) -> dict[str, Any]:
    """A JSON file that must hold an object."""
    data = read_json(path)
    if not isinstance(data, dict):
        raise Unreadable(f"{path}: expected a JSON object, found {type(data).__name__}")
    return data


def read_run(path: Path) -> dict[str, Any]:
    """A stored run: an object, and if it carries a grade, a grade that is an object."""
    data = read_object(path)
    grade = data.get("grade")
    if grade is not None and not isinstance(grade, dict):
        raise Unreadable(f"{path}: 'grade' is not an object")
    return data


def digest(payload: dict[str, Any]) -> str:
    """SHA-256 over the payload without its own integrity block, in one fixed spelling."""
    body = {key: value for key, value in payload.items() if key != INTEGRITY}
    text = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stamp(payload: dict[str, Any]) -> dict[str, Any]:
    """The payload with a digest of what the rig is writing, for a later edit to disagree with."""
    body = {key: value for key, value in payload.items() if key != INTEGRITY}
    return {**body, INTEGRITY: {"sha256": digest(body)}}


def altered(payload: dict[str, Any]) -> bool | None:
    """True if the file's digest does not match its content; None if it never had one."""
    block = payload.get(INTEGRITY)
    if block is None:
        return None
    return not (isinstance(block, dict) and block.get("sha256") == digest(payload))


@dataclass
class Completeness:
    status: str = UNKNOWN
    planned: int = 0
    accounted: int = 0
    held_back: int = 0
    # Why the status is what it is, one line each.
    problems: list[str] = field(default_factory=list)
    # Run files with no digest: written before the rig stamped them.
    unstamped: int = 0
    # Why it is unknown, when it is.
    reason: str = ""

    @property
    def bad(self) -> bool:
        """The directory is known not to be what it was planned to be."""
        return self.status in (INCOMPLETE, ALTERED)

    @property
    def unknown(self) -> bool:
        return self.status == UNKNOWN

    def lines(self) -> list[str]:
        lines = self._lines()
        if self.unstamped and self.status != ALTERED:
            lines.append(
                f"  {self.unstamped} run file(s) carry no integrity digest (written before the "
                "rig stamped them): an edit of those cannot be seen"
            )
        return lines

    def _lines(self) -> list[str]:
        if self.status == COMPLETE:
            held = f"; {self.held_back} HELD BACK and not run" if self.held_back else ""
            return [
                f"completeness: all {self.planned} planned call(s) are accounted for "
                f"({self.planned - self.held_back} run{held})"
            ]
        head = {
            INCOMPLETE: f"INCOMPLETE: {self.accounted} of {self.planned} planned call(s) are "
            "accounted for",
            ALTERED: "ALTERED: a stored run is not as the rig wrote it",
            UNKNOWN: f"completeness unknown: {self.reason}",
        }[self.status]
        shown = self.problems[:SHOWN]
        more = len(self.problems) - len(shown)
        lines = [head, *(f"  {problem}" for problem in shown)]
        if more > 0:
            lines.append(f"  and {more} more")
        return lines

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "planned": self.planned,
            "accounted": self.accounted,
            "held_back": self.held_back,
            "unstamped": self.unstamped,
            "problems": list(self.problems),
            "reason": self.reason,
        }


def combine(parts: list[Completeness]) -> Completeness:
    """Several directories as one: the worst state, every problem, the counts added."""
    total = Completeness(status=COMPLETE)
    for one in parts:
        total.planned += one.planned
        total.accounted += one.accounted
        total.held_back += one.held_back
        total.problems += one.problems
        total.unstamped += one.unstamped
    kinds = {one.status for one in parts}
    if INCOMPLETE in kinds:
        total.status = INCOMPLETE
    elif ALTERED in kinds:
        total.status = ALTERED
    elif UNKNOWN in kinds or not parts:
        total.status = UNKNOWN
        total.reason = next((one.reason for one in parts if one.reason), "nothing was read")
    return total


def _key(file: Any, repeat: Any) -> tuple[str, int]:
    return str(file), int(repeat) if isinstance(repeat, int) else 0


def check(directory: str | Path) -> Completeness:
    """Compare what a directory was planned to hold with what it holds. Raises ``Unreadable``."""
    from synthiq_caller import manifest  # late: manifest reads files through this module

    root = Path(directory)
    runs = {path.name: read_run(path) for path in sorted(root.glob("run-*.json"))}
    chains = {path.name: read_object(path) for path in sorted(root.glob("chain-*.json"))}
    held = root / "batch.json"
    summary = read_object(held) if held.exists() else None
    document = manifest.load(root)
    entries = [e for e in (document or {}).get("entries", []) if e.get("kind") == "batch"]
    planned_entries = [e for e in entries if isinstance(e.get("plan"), dict)]
    ids = {str(e.get("id")) for e in planned_entries}

    result = Completeness(status=COMPLETE)
    problems = result.problems
    edits = 0
    for name, payload in {**runs, **chains}.items():
        verdict = altered(payload)
        if verdict is None:
            result.unstamped += 1
            if str(payload.get("batch", "")) in ids:
                # The rig stamps every file it writes under a plan, so a stripped digest
                # is an edit, not an old file.
                edits += 1
                problems.append(
                    f"{name}: its integrity digest is gone (removed after the rig wrote it)"
                )
        elif verdict:
            edits += 1
            problems.append(f"{name}: changed after the rig wrote it (its digest does not match)")

    # A file deleted from a directory the rig has no plan for: batch.json knows how many ran.
    ran = summary.get("ran") if summary else None
    if isinstance(ran, int) and ran > len(runs):
        problems.append(
            f"batch.json records {ran} placed call(s) but {len(runs)} run file(s) are here: "
            f"{ran - len(runs)} removed or lost"
        )

    for entry in planned_entries:
        batch = str(entry.get("id"))
        units = entry["plan"].get("units", [])
        done_runs = {
            _key(p.get("scenario_file"), p.get("repeat"))
            for p in runs.values()
            if p.get("batch") == batch and "chain" not in p
        }
        done_chains = {
            _key(c.get("chain_file"), c.get("repeat"))
            for c in chains.values()
            if c.get("batch") == batch
        }
        gave_up = {_key(h.get("file"), h.get("repeat")) for h in entry.get("held_back", [])}
        planned_runs = {
            _key(u.get("file"), u.get("repeat")) for u in units if u.get("kind") != "chain"
        }
        planned_chains = {
            _key(u.get("file"), u.get("repeat")) for u in units if u.get("kind") == "chain"
        }
        counted: dict[tuple[str, str, int], list[str]] = {}
        for name, p in runs.items():
            if p.get("batch") == batch and "chain" not in p:
                key = _key(p.get("scenario_file"), p.get("repeat"))
                counted.setdefault(("run", *key), []).append(name)
        for name, c in chains.items():
            if c.get("batch") == batch:
                key = _key(c.get("chain_file"), c.get("repeat"))
                counted.setdefault(("chain", *key), []).append(name)
        surplus: list[str] = []
        for (kind, file, repeat), names in counted.items():
            known = planned_chains if kind == "chain" else planned_runs
            if (file, repeat) not in known:
                surplus += names
            else:
                surplus += names[1:]
        if surplus:
            problems.append(
                f"batch {batch}: {len(surplus)} file(s) are not in the plan or repeat a call "
                f"already recorded (first: {sorted(surplus)[0]}): copied or added; they are not "
                "counted as calls"
            )
        never: list[str] = []
        for unit in units:
            key = _key(unit.get("file"), unit.get("repeat"))
            result.planned += 1
            seen = done_chains if unit.get("kind") == "chain" else done_runs
            if key in gave_up:
                result.held_back += 1
                result.accounted += 1
            elif key in seen:
                result.accounted += 1
            else:
                never.append(f"{key[0]} repeat {key[1]}")
        if never:
            if entry.get("status") == "finished":
                problems.append(
                    f"batch {batch}: {len(never)} planned call(s) have no run file and no "
                    f"held-back record (first: {never[0]}): removed or lost"
                )
            else:
                how = (
                    "was interrupted" if entry.get("status") == "interrupted" else "did not finish"
                )
                problems.append(
                    f"batch {batch} {how}: {len(never)} planned call(s) never ran "
                    f"(first: {never[0]})"
                )
        for name, chain in chains.items():
            if chain.get("batch") != batch:
                continue
            for row in chain.get("calls", []):
                gone = row.get("run") if isinstance(row, dict) else None
                if gone and gone not in runs:
                    problems.append(f"{name} refers to {gone}, which is missing")

    unplanned = sum(1 for p in runs.values() if str(p.get("batch", "")) not in ids)
    if problems:
        result.status = ALTERED if edits == len(problems) else INCOMPLETE
    elif not planned_entries:
        result.status = UNKNOWN
        result.reason = (
            "no plan is recorded for these runs (made before the rig wrote one), so a "
            "call that never ran, or a file deleted, cannot be seen"
        )
    elif unplanned:
        result.status = UNKNOWN
        result.reason = (
            f"{unplanned} run(s) belong to no planned batch (made before the rig wrote "
            "plans, or by another command), so what is missing from them cannot be seen"
        )
    return result
