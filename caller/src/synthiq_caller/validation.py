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

from synthiq_caller.batch import load_chains, load_scenarios
from synthiq_caller.chain import variables_in
from synthiq_caller.clips import ClipBank, ClipExcluded
from synthiq_caller.prereq import PrereqChecker, requires_state


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
    # What the scenario says it still needs recorded (see ``missing_clips``).
    missing_recordings: list[str] = field(default_factory=list)

    @property
    def problem(self) -> bool:
        return (
            self.clips_state == "missing"
            or bool(self.expired_clips)
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
        loud = self.requires_state not in ("none", "met")
        requires = f"requires {self.requires_state.upper() if loud else self.requires_state}"
        detail = f" ({self.requires_detail})" if self.requires_detail else ""
        target = (
            f"  TARGET PROFILE LACKS {self.missing_target}, will not run"
            if self.missing_target
            else ""
        )
        return (
            f"{self.file:<44} mutates={self.mutates:<10} {self.shape}  "
            f"{clips}{expired}{unknown}{needs}  {requires}{detail}{target}"
        )


def _clips_state(bank: ClipBank | None, missing: list[str]) -> str:
    if missing:
        return "missing"
    return "ok" if bank is not None else "not checked"


def _retention(
    bank: ClipBank | None, named: list[str], today: date | None
) -> tuple[list[str], list[str]]:
    """The clips among ``named`` past retention, and those with no provenance."""
    if bank is None:
        return [], []
    return bank.expired_in(named, today), bank.provenance_unknown(named)


@dataclass
class BankFinding:
    clip: str
    # "past retention", "invalid field", "no provenance" or "gap".
    kind: str
    detail: str

    @property
    def problem(self) -> bool:
        return self.kind in ("past retention", "invalid field")

    def line(self) -> str:
        return f"{self.clip:<44} {self.kind.upper() if self.problem else self.kind}: {self.detail}"


def validate_bank(bank: ClipBank, today: date | None = None) -> list[BankFinding]:
    """What the bank's own manifests say is wrong or missing, whatever scenarios use.

    Past retention and invalid fields are problems. A clip with no provenance is
    reported, and a recording that lacks a speaker ref, a lawful basis or dates is
    listed as a gap: neither stops ``validate``, but a batch will not play a clip
    with no provenance unless told to.
    """
    day = today or date.today()
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
        if gaps := consent.gaps():
            found.append(BankFinding(info.clip, "gap", f"no {', '.join(gaps)}"))
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
        late, unknown = _retention(bank, named, today)
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
                requires_state=prereq.state,
                requires_detail=prereq.detail,
                missing_target=list(scenario.missing_target),
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
        late, unknown = _retention(bank, named, today)
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
                requires_state=prereq.state,
                requires_detail=prereq.detail,
                missing_target=[
                    f"{call.id}: {part}" for call in chain.calls for part in call.missing_target
                ],
            )
        )
    return rows
