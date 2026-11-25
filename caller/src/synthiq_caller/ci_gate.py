"""The checks a scheduled or customer CI run makes before it places a single call.

    python -m synthiq_caller.ci_gate check --target NAME --allowed "a,b" --root DIR --set NAME
    python -m synthiq_caller.ci_gate bank --root DIR --set NAME --out DIR

``check`` refuses (exit 1, a message on stderr) when:

* the allow-list is empty or missing: nothing is allowed until somebody lists it;
* the target is empty, or is not on the list. The match is exact; there is no
  wildcard, so "everything" can only be written out;
* the set name is not a plain folder name (it could point outside ``--root``);
* the scenario set cannot be loaded, is empty, or holds any scenario or chain
  whose ``mutates`` is more than the batch's own ``--allow-mutating none`` lets
  through (``batch.ALLOWED``). That is the caller's mutation guard, used as it
  is: a scenario that does not say what it changes is ``unknown`` and is
  refused like one that books.

The batch run afterwards still passes ``--allow-mutating none``, so the guard
holds twice. Nothing here reads a secret or opens a connection.

``bank`` writes a clip bank of short generated tones, one per clip the scenarios
name, with a manifest that says so (``provenance`` starts "synthetic"). It
holds no recording of any person. It exists so the fake leg can be run in CI.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import wave
from pathlib import Path

from synthiq_caller.batch import ALLOWED, load_chains, load_scenarios
from synthiq_caller.chain import Chain
from synthiq_caller.legs.fake import RATE, tone
from synthiq_caller.scenario import Scenario, ScenarioError

SET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class GateRefused(Exception):
    """The run must not start. The message says why, in words for the person reading the log."""


def parse_allow_list(raw: str | None) -> list[str]:
    """Names split on commas and line breaks; blanks dropped, nothing else interpreted."""
    return [item.strip() for item in re.split(r"[,\n]", raw or "") if item.strip()]


def check_target(target: str, allow_raw: str | None) -> None:
    allowed = parse_allow_list(allow_raw)
    if not allowed:
        raise GateRefused(
            "the allow-list is empty: set the repository variable CALLER_ALLOWED_TARGETS to a "
            "comma-separated list of the targets this workflow may run against. Nothing runs "
            "until one is listed."
        )
    name = target.strip()
    if not name:
        raise GateRefused("no target was named")
    if name not in allowed:
        raise GateRefused(
            f"target {name!r} is not on the allow-list (CALLER_ALLOWED_TARGETS). Add it there "
            "if it is meant to be run against; the workflow will not run it until then."
        )


def safe_set_name(name: str) -> str:
    """A scenario-set name that cannot point outside its folder."""
    if not SET_NAME.match(name) or ".." in name:
        raise GateRefused(
            f"scenario set name {name!r} is not allowed: letters, digits, '.', '_' and '-' only"
        )
    return name


def resolve_set(root: str | Path, name: str) -> Path:
    """The folder of the named scenario set under ``root``."""
    return Path(root) / safe_set_name(name)


def check_scenarios(directory: str | Path) -> int:
    """Refuse a set with anything in it that is not read-only. Returns how many files it holds."""
    root = Path(directory)
    if not root.is_dir():
        raise GateRefused(f"scenario set {str(root)!r} does not exist")
    try:
        scenarios = load_scenarios(root)
        chains = load_chains(root)
    except (ScenarioError, ValueError, OSError) as error:
        raise GateRefused(f"the scenario set cannot be loaded: {error}") from error
    if not scenarios and not chains:
        raise GateRefused(f"scenario set {str(root)!r} holds no scenarios")
    allowed = ALLOWED["none"]
    items: list[tuple[str, Scenario | Chain]] = [*scenarios, *chains]
    refused = [
        f"{file} (mutates: {item.mutates})" for file, item in items if item.mutates not in allowed
    ]
    if refused:
        raise GateRefused(
            "mutating scenarios are refused: this workflow places no call that could change "
            "anything. Remove or mark read-only: " + ", ".join(refused)
        )
    return len(scenarios) + len(chains)


def check(target: str, allow_raw: str | None, directory: str | Path) -> int:
    check_target(target, allow_raw)
    return check_scenarios(directory)


def clip_names(directory: str | Path) -> list[str]:
    names: set[str] = set()
    for _file, scenario in load_scenarios(directory):
        for step in scenario.steps:
            names.update(Path(c).stem for c in step.clips)
    return sorted(names)


def make_bank(scenarios: str | Path, out: str | Path) -> list[str]:
    """A bank of generated tones for every clip the set names. No speech, no recording."""
    names = clip_names(scenarios)
    folder = Path(out)
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        with wave.open(str(folder / f"{name}.wav"), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(RATE)
            wav.writeframes(tone(0.25))
    with (folder / "manifest.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["clip", "call", "category", "secs", "said", "question", "provenance"])
        for name in names:
            writer.writerow([name, "", "", "0.25", "", "", "synthetic: generated tone, no speech"])
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="synthiq_caller.ci_gate")
    sub = parser.add_subparsers(dest="command", required=True)
    chk = sub.add_parser("check", help="refuse a target or a scenario set the run may not use")
    chk.add_argument("--target", required=True)
    chk.add_argument(
        "--allowed",
        default="",
        help="the allow-list: comma or line separated (the variable's value)",
    )
    chk.add_argument("--root", required=True, help="the folder holding the scenario sets")
    chk.add_argument("--set", required=True, dest="set_name", help="the scenario set's name")
    bank = sub.add_parser("bank", help="write a bank of generated tones for the fake leg")
    bank.add_argument("--root", required=True)
    bank.add_argument("--set", required=True, dest="set_name")
    bank.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            count = check(args.target, args.allowed, resolve_set(args.root, args.set_name))
            print(
                f"allowed: target {args.target!r}, set {args.set_name!r}; "
                f"{count} read-only scenario file(s)"
            )
        else:
            names = make_bank(resolve_set(args.root, args.set_name), args.out)
            print(f"wrote {len(names)} generated clip(s) to {args.out}")
    except GateRefused as refusal:
        print(f"::error title=Caller run refused::{refusal}", file=sys.stderr)
        print(f"REFUSED: {refusal}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
