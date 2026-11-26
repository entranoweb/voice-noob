"""Check a directory of scenarios and chains before any call is placed.

Three separate questions, never folded into one "ok": do the files parse, are
the clips they name in the bank, and does what they require of the system under
test hold. The last can only be answered by a checker the deployment supplies
(``prereq.py``); without one it is reported as "unchecked".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from synthiq_caller import removals
from synthiq_caller.batch import _scenario_files, load_chains, load_scenarios
from synthiq_caller.chain import is_chain_file, load_chain, variables_in
from synthiq_caller.clips import ClipBank, ClipExcluded
from synthiq_caller.consent import today_utc
from synthiq_caller.prereq import PrereqChecker, requires_state
from synthiq_caller.scenario import UNGRADEABLE, ScenarioError, load_scenario


@dataclass
class Row:
    file: str
    mutates: str
    # "3 steps" for a scenario; the calls of a chain, in order.
    shape: str
    # "ok", "not checked" (no clip bank given) or "missing".
    clips_state: str
    missing_clips: list[str] = field(default_factory=list)
    needs_recordings: int = 0
    requires_state: str = "none"
    requires_detail: str = ""
    # Parts of the appointment the target profile should give and does not.
    missing_target: list[str] = field(default_factory=list)
    # Clips named that are past their retention date; a batch will not play them.
    expired_clips: list[str] = field(default_factory=list)
    # Clips named whose manifest row says nothing of where they came from.
    unknown_provenance: list[str] = field(default_factory=list)
    # Real clips named that have no lawful basis or consent record; a batch will not play them.
    consent_missing: list[str] = field(default_factory=list)
    # True when ``--allow-missing-consent`` was given: they are reported, not a problem.
    consent_allowed: bool = False
    # What the scenario says it still needs recorded (see ``missing_clips``).
    missing_recordings: list[str] = field(default_factory=list)
    # False for a scenario that states no expectations: it can never pass.
    states_expectations: bool = True

    @property
    def problem(self) -> bool:
        return (
            self.clips_state == "missing"
            or bool(self.expired_clips)
            or (bool(self.consent_missing) and not self.consent_allowed)
            or self.requires_state in ("unmet", "error")
        )

    def line(self) -> str:
        clips = (
            f"CLIPS NOT USABLE: {self.missing_clips}"
            if self.clips_state == "missing"
            else f"clips {self.clips_state}"
        )
        needs = (
            f"  MISSING CLIP: needs {self.needs_recordings} recording(s), will not run"
            + "".join(f"\n    - {text[:100]}" for text in self.missing_recordings)
            if self.needs_recordings
            else ""
        )
        expired = (
            f"  CLIPS PAST RETENTION: {self.expired_clips}, will not run"
            if self.expired_clips
            else ""
        )
        unknown = (
            f"  provenance unknown: {len(self.unknown_provenance)} clip(s), "
            "a batch needs --allow-unknown-provenance"
            if self.unknown_provenance
            else ""
        )
        no_consent = (
            f"  CONSENT RECORD MISSING: {len(self.consent_missing)} real clip(s)"
            + (
                ", allowed for this run"
                if self.consent_allowed
                else ", a batch needs --allow-missing-consent"
            )
            if self.consent_missing
            else ""
        )
        loud = self.requires_state not in ("none", "met")
        requires = f"requires {self.requires_state.upper() if loud else self.requires_state}"
        detail = f" ({self.requires_detail})" if self.requires_detail else ""
        target = (
            f"  TARGET PROFILE LACKS {self.missing_target}, will not run"
            if self.missing_target
            else ""
        )
        ungradeable = (
            "" if self.states_expectations else "  UNGRADEABLE until expectations are stated"
        )
        return (
            f"{self.file:<44} mutates={self.mutates:<10} {self.shape}  "
            f"{clips}{expired}{unknown}{no_consent}{needs}  {requires}{detail}{target}{ungradeable}"
        )


def ungradeable_why(file: str) -> str:
    """The one sentence that names a scenario that cannot be graded, and why."""
    return (
        f"{file}: {UNGRADEABLE}: the scenario states no expectations (expect.tools, fields, "
        "agent_says, agent_never_says, read_back or booking), so no call could pass it"
    )


def ungradeable_problems(rows: list[Row]) -> list[str]:
    """One line per scenario that cannot be graded, for ``validate --strict`` and the CI gate.

    Only gradeability is in here. An unverified label, an unchecked prerequisite
    or a clip not yet in the bank are not: they are reported as they always were.
    """
    return [ungradeable_why(row.file) for row in rows if not row.states_expectations]


def missing_clip_problems(rows: list[Row]) -> list[str]:
    """One line per scenario that names a clip that was removed (``forget``), for ``--strict``.

    Such a scenario will not run: a batch holds it back. A gate built on it would be a gate
    over fewer scenarios than the directory seems to hold.
    """
    return [
        f"{row.file}: MISSING CLIP: needs {row.needs_recordings} recording(s) and will not run"
        for row in rows
        if row.needs_recordings
    ]


def load_problems(directory: str | Path, target: dict[str, Any] | None = None) -> list[str]:
    """One line per file that cannot be loaded (``<file>: <why>``), for a clean error.

    ``load_scenarios`` and ``load_chains`` stop at the first bad file with an exception
    that does not name it; this loads the files one at a time to say which.
    """
    problems: list[str] = []
    for path in _scenario_files(directory):
        try:
            load_chain(path, target) if is_chain_file(path) else load_scenario(path, target)
        except (ScenarioError, ValueError) as error:
            problems.append(f"{path.name}: {error}")
    return problems


def _clips_state(bank: ClipBank | None, missing: list[str]) -> str:
    if missing:
        return "missing"
    return "ok" if bank is not None else "not checked"


def _retention(
    bank: ClipBank | None, named: list[str], today: date | None
) -> tuple[list[str], list[str], list[str]]:
    """The clips among ``named`` past retention, with no provenance, and real but unrecorded."""
    if bank is None:
        return [], [], []
    return (
        bank.expired_in(named, today),
        bank.provenance_unknown(named),
        bank.consent_missing(named),
    )


@dataclass
class BankFinding:
    clip: str
    # "past retention", "invalid field", "no provenance" or "gap".
    kind: str
    detail: str
    allowed: bool = False

    @property
    def problem(self) -> bool:
        if self.kind == "consent record missing":
            return not self.allowed
        return self.kind in (
            "past retention",
            "invalid field",
            "unfinished removal",
            "unusable audio",
        )

    def line(self) -> str:
        return f"{self.clip:<44} {self.kind.upper() if self.problem else self.kind}: {self.detail}"


def validate_bank(bank: ClipBank, today: date | None = None) -> list[BankFinding]:
    """What the bank's own manifests say is wrong or missing, whatever scenarios use.

    Past retention and invalid fields are problems. A clip with no provenance is
    reported, and a recording that lacks a speaker ref, a lawful basis or dates is
    listed as a gap: neither stops ``validate``, but a batch will not play a clip
    with no provenance unless told to.
    """
    day = today or today_utc()
    found: list[BankFinding] = []
    for info in bank.infos():
        consent = info.consent
        if consent.expired(day):
            found.append(
                BankFinding(info.clip, "past retention", f"retain_until {consent.retain_until}")
            )
        for problem in consent.problems():
            found.append(BankFinding(info.clip, "invalid field", problem))
        if info.provenance == "unknown" and not consent.provenance:
            found.append(BankFinding(info.clip, "no provenance", "the manifest does not say"))
        if lacks := consent.record_missing():
            found.append(
                BankFinding(
                    info.clip,
                    "consent record missing",
                    f"a real clip (special-category data) has no {' or '.join(lacks)}"
                    + ("; allowed for this run" if bank.allow_missing_consent else ""),
                    allowed=bank.allow_missing_consent,
                )
            )
        if gaps := consent.gaps():
            found.append(BankFinding(info.clip, "gap", f"no {', '.join(gaps)}"))
        if why := bank.unusable(info.clip):
            found.append(BankFinding(info.clip, "unusable audio", why))
    for line in removals.unfinished_lines(bank.directories):
        removal = line.split(":", 1)[0].removeprefix("UNFINISHED REMOVAL ")
        found.append(BankFinding(removal, "unfinished removal", line.split(": ", 1)[1]))
    return found


def validate_directory(
    directory: str | Path,
    bank: ClipBank | None,
    checker: PrereqChecker | None,
    target: dict[str, Any] | None = None,
    today: date | None = None,
) -> list[Row]:
    rows: list[Row] = []
    for file, scenario in load_scenarios(directory, target):
        missing: list[str] = []
        named: list[str] = []
        if bank is not None:
            # The fallback's clips are played too, when a question has no step.
            answers = [*scenario.steps, *([scenario.fallback] if scenario.fallback else [])]
            for step in answers:
                for clip in step.clips:
                    if clip.startswith("missing/") and scenario.missing_clips:
                        continue  # declared as not yet recorded; a batch will not run it
                    named.append(clip)
                    try:
                        bank.path(clip)
                    except ClipExcluded as ruled_out:
                        missing.append(str(ruled_out))
                    except FileNotFoundError:
                        missing.append(clip)
                    else:
                        if why := bank.unusable(clip):
                            missing.append(f"{clip} ({why})")
        late, unknown, no_consent = _retention(bank, named, today)
        prereq = requires_state(file, scenario.name, scenario.requires, checker)
        rows.append(
            Row(
                file=file,
                mutates=scenario.mutates,
                shape=f"steps={len(scenario.steps)}",
                clips_state=_clips_state(bank, missing),
                missing_clips=missing,
                needs_recordings=len(scenario.missing_clips),
                missing_recordings=list(scenario.missing_clips),
                expired_clips=late,
                unknown_provenance=unknown,
                consent_missing=no_consent,
                consent_allowed=bank is not None and bank.allow_missing_consent,
                requires_state=prereq.state,
                requires_detail=prereq.detail,
                missing_target=list(scenario.missing_target),
                states_expectations=scenario.states_expectations(),
            )
        )
    for file, chain in load_chains(directory, target):
        missing = []
        named = []
        if bank is not None:
            for call in chain.calls:
                raw_answers = [*call.raw.get("steps", ()), call.raw.get("fallback") or {}]
                for raw_step in raw_answers:
                    for clip in raw_step.get("clips", ()):
                        if variables_in(clip) or (
                            str(clip).startswith("missing/") and call.missing_clips
                        ):
                            continue  # chosen at run time, or declared as not yet recorded
                        named.append(str(clip))
                        try:
                            bank.path(str(clip))
                        except ClipExcluded as ruled_out:
                            missing.append(str(ruled_out))
                        except FileNotFoundError:
                            missing.append(str(clip))
                        else:
                            if why := bank.unusable(str(clip)):
                                missing.append(f"{clip} ({why})")
        late, unknown, no_consent = _retention(bank, named, today)
        prereq = requires_state(file, chain.name, chain.requires, checker)
        calls = " > ".join(f"{call.id}({call.mutates})" for call in chain.calls)
        rows.append(
            Row(
                file=file,
                mutates=chain.mutates,
                shape=f"chain clean_state={'yes' if chain.clean_state else 'no'}  {calls}",
                clips_state=_clips_state(bank, missing),
                missing_clips=missing,
                needs_recordings=sum(len(call.missing_clips) for call in chain.calls),
                missing_recordings=[text for call in chain.calls for text in call.missing_clips],
                expired_clips=late,
                unknown_provenance=unknown,
                consent_missing=no_consent,
                consent_allowed=bank is not None and bank.allow_missing_consent,
                requires_state=prereq.state,
                requires_detail=prereq.detail,
                missing_target=[
                    f"{call.id}: {part}" for call in chain.calls for part in call.missing_target
                ],
            )
        )
    return rows
