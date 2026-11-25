"""Command line: run scenarios, then report.

    synthiq-caller run scenario.json --leg elevenlabs-socket --agent <id> \\
        --clips <dir> --repeats 3 --out runs/
    synthiq-caller report runs/
    synthiq-caller batch scenarios/sophie --leg elevenlabs-socket --agent <id> \
        --clips <dir> --repeats 3 --out runs/        (runs lookup-only scenarios)
    synthiq-caller validate scenarios/sophie --clips <dir> [--check-requires-command "..."]
    synthiq-caller scorecard runs/a runs/b --out card.html [--previous last.html]
    synthiq-caller regrade runs/ --scenarios scenarios/sophie   (no calls placed)
    synthiq-caller baseline runs/ --out baseline.json
    synthiq-caller compare runs-new/ --baseline baseline.json --fail-on beyond-noise
    synthiq-caller gate runs/ --rate 0.95 --confidence 0.95
    synthiq-caller clipbank build --recordings <dir> --out <dir outside any repo>
    synthiq-caller clipbank scenario --clips <dir> --call <call id>

Engine credentials are read from the environment (or an env file named with
``--env-file``) and are never printed or written to the output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from synthiq_caller.batch import (
    ALLOWED,
    NameBudget,
    NoHooks,
    ShellHooks,
    load_chains,
    load_scenarios,
    regrade,
    regrade_chains,
    reread_missing_traces,
    run_batch,
)
from synthiq_caller.clips import ClipBank
from synthiq_caller.compare import (
    compare,
    load_baseline,
    make_baseline,
    render_comparison,
    save_baseline,
)
from synthiq_caller.director import RunRecord
from synthiq_caller.jsonio import utf8_console
from synthiq_caller.leg import Leg
from synthiq_caller.prereq import CommandChecker, HttpChecker, PrereqChecker
from synthiq_caller.readback import CommandReadBack, HttpReadBack, NoReadBack, ReadBack
from synthiq_caller.report import render, scenario_level, summarise
from synthiq_caller.reset import CommandReset, HttpReset, NoReset, Reset
from synthiq_caller.scenario import load_scenario
from synthiq_caller.stats import describe, gate
from synthiq_caller.traces.base import TraceReader
from synthiq_caller.validation import validate_directory


def _secret(name: str, env_file: str | None) -> str:
    value = os.environ.get(name, "")
    if not value and env_file:
        for line in Path(env_file).read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{name}="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not value:
        raise SystemExit(f"{name} is not set (environment or --env-file)")
    return value


TRACES = ("elevenlabs", "retell", "vapi")


def _trace_reader(args: argparse.Namespace) -> TraceReader:
    """The only place a trace source's name is turned into a reader.

    The reader must belong to the platform the call reached: its call reference
    is what the leg hands over.
    """
    choice = getattr(args, "trace", None) or "elevenlabs"
    if choice == "retell":
        from synthiq_caller.traces.retell import RetellTraceReader

        return RetellTraceReader(_secret("RETELL_API_KEY", args.env_file))
    if choice == "vapi":
        from synthiq_caller.traces.vapi import VapiTraceReader

        return VapiTraceReader(_secret("VAPI_API_KEY", args.env_file))
    from synthiq_caller.traces.elevenlabs import ElevenLabsTraceReader

    return ElevenLabsTraceReader(_secret("ELEVENLABS_API_KEY", args.env_file))


LEGS = ["elevenlabs-socket", "fake"]


def _build_fake(args: argparse.Namespace, scenario_file: str) -> tuple[Leg, TraceReader | None]:
    """A scripted stand-in for the agent: a rehearsal of the rig, not a test of anything."""
    from synthiq_caller.legs.fake import FakeLeg, ScriptedTraceReader, script_for

    if not getattr(args, "fake_agent", ""):
        raise SystemExit("--fake-agent (a script file, or a directory of them) is required")
    script = script_for(args.fake_agent, scenario_file)
    return FakeLeg(list(script.lines)), ScriptedTraceReader(script.trace)


def _build(args: argparse.Namespace) -> tuple[Leg, TraceReader | None]:
    """The only place a leg name is turned into engine-specific objects."""
    if args.leg == "elevenlabs-socket":
        from synthiq_caller.legs.elevenlabs_socket import ElevenLabsSocketLeg

        if not args.agent:
            raise SystemExit("--agent is required for the elevenlabs-socket leg")
        key = _secret("ELEVENLABS_API_KEY", args.env_file)
        leg = ElevenLabsSocketLeg(
            key, args.agent, keypad_as_message=getattr(args, "keypad_as_message", False)
        )
        return leg, _trace_reader(args)
    raise SystemExit(f"unknown or unbuilt leg: {args.leg}")


def _print_grade(index: int, payload: dict[str, Any]) -> None:
    g = payload["grade"]
    record = payload["record"]
    print(f"\nrun {index}: {g['outcome']}  ({record['duration_s']}s, {record['ended']})")
    if g.get("fault") or g.get("reason"):
        print(f"  fault: {g.get('fault') or '-'}  {g.get('reason', '')}")
    for check in g["checks"]:
        print(f"  [{'pass' if check['passed'] else 'FAIL'}] {check['what']}")
    if g["fields"]:
        print(
            f"  {'field':<14} {'said':<22} {'heard':<26} {'stored':<24} {'stored from':<12} verdict"
        )
        for row in g["fields"]:
            print(
                f"  {row['field']:<14} {str(row['said'])[:21]:<22} "
                f"{str(row['heard'] or '-')[:25]:<26} {str(row['stored'] or '-')[:23]:<24} "
                f"{(row.get('stored_source') or '-'):<12} {row['verdict']}"
            )
    if payload.get("trace_error"):
        print(f"  trace could not be read: {payload['trace_error']}")
    if payload.get("read_back_error"):
        print(f"  read-back failed, tool calls used for 'stored': {payload['read_back_error']}")


def report(directory: str, *, details: bool = True) -> int:
    files = sorted(Path(directory).glob("run-*.json"))
    if not files and not (Path(directory) / "batch.json").exists():
        print("no runs found")
        return 1
    if details:
        for index, path in enumerate(files, start=1):
            _print_grade(index, json.loads(path.read_text(encoding="utf-8")))
        print()
    print(render(summarise(directory)))
    return 0


def validate(args: argparse.Namespace) -> int:
    """Parse every scenario and chain; check clips against a bank and prerequisites if asked.

    A prerequisite is "unchecked" unless a checker was given: this command has no
    way of its own to look inside the system under test.
    """
    checker: PrereqChecker | None = None
    if args.check_requires_command:
        checker = CommandChecker(args.check_requires_command, timeout_s=args.check_requires_timeout)
    elif args.check_requires_url:
        checker = HttpChecker(
            args.check_requires_url,
            secret=_named_secret(args.check_requires_secret_env, args.env_file),
            header=args.check_requires_header,
            timeout_s=args.check_requires_timeout,
        )
    rows = validate_directory(
        args.scenarios, _bank(args) if args.clips else None, checker, _target(args)
    )
    for row in rows:
        print(row.line())
    unchecked = sum(row.requires_state == "unchecked" for row in rows)
    if unchecked:
        print(
            f"\n{unchecked} file(s) require something of the system under test that was not "
            "checked (--check-requires-command or --check-requires-url)."
        )
    return 1 if any(row.problem for row in rows) else 0


def _scorecard(args: argparse.Namespace) -> int:
    from synthiq_caller import scorecard

    card = scorecard.build(
        [Path(d) for d in args.directories],
        faults=scorecard.load_faults(args.faults) if args.faults else None,
        previous=scorecard.load_previous(args.previous) if args.previous else None,
        title=args.title,
    )
    out = scorecard.write(card, args.out)
    print(f"{describe(card['passed'], card['graded'])}  ->  {out}")
    return 0


def _gate(directory: str, rate: float, confidence: float, max_unjudged: float = 0.1) -> int:
    """Judge a batch against a claimed pass rate. Exit 0 only when it is demonstrated.

    Two readings are printed. By call, every placed call that is not void counts
    and anything not passed counts against. By scenario, a scenario counts once
    and passes only if every one of its calls passed: the reading that does not
    treat repeats of the same audio as independent evidence. A call that was
    placed and could not be graded counts against in both.

    Void calls and calls that were never placed are in neither reading, so a
    batch could meet the numbers on the part of itself that happened to run.
    ``max_unjudged`` is the share of what was attempted that may go unjudged
    (void, ungraded, ungradeable, not run) before the gate is refused whatever
    the numbers say. A rehearsal against a scripted agent never meets a gate.
    """
    summary = summarise(directory)
    counted = summary.runs - summary.void
    met_calls, _, by_call = gate(summary.passed, counted, rate, confidence)
    every, _, scenarios = scenario_level(summary)
    met, _, by_scenario = gate(every, scenarios, rate, confidence)
    not_run = len(summary.blocked)
    unjudged = summary.void + summary.ungraded + summary.ungradeable + not_run
    attempted = summary.runs + not_run
    share = unjudged / attempted if attempted else 1.0
    print(f"by call:     {by_call}")
    print(f"by scenario: {by_scenario}")
    print(
        f"attempted: {attempted} call(s); placed {summary.runs}; counted {counted}. "
        f"Not judged: {unjudged} ({share:.0%}): void {summary.void} (the rig's fault, re-run "
        f"them), ungraded {summary.ungraded}, ungradeable {summary.ungradeable}, "
        f"not run {not_run}. Calls that were placed and could not be graded count against."
    )
    if met_calls and not met:
        print("The by-call reading is met only by counting repeats as independent calls.")
    refused = []
    if summary.rehearsals:
        refused.append(
            f"{summary.rehearsals} call(s) are rehearsals against a scripted stand-in, not an agent"
        )
    if share > max_unjudged:
        refused.append(
            f"{share:.0%} of what was attempted was not judged, more than the "
            f"{max_unjudged:.0%} allowed (--max-unjudged); the numbers above describe only "
            "the part that was"
        )
    if refused:
        print("NOT met: " + "; ".join(refused) + ".")
        return 1
    return 0 if met else 1


def _checker(args: argparse.Namespace) -> PrereqChecker | None:
    """The checker for a scenario's ``requires``, when one was named on the command line."""
    if args.check_requires_command:
        return CommandChecker(args.check_requires_command, timeout_s=args.check_requires_timeout)
    if args.check_requires_url:
        return HttpChecker(
            args.check_requires_url,
            secret=_named_secret(args.check_requires_secret_env, args.env_file),
            header=args.check_requires_header,
            timeout_s=args.check_requires_timeout,
        )
    return None


def _bank(args: argparse.Namespace) -> ClipBank:
    return ClipBank(args.clips, extra=tuple(getattr(args, "clips_extra", None) or ()))


def _named_secret(env_name: str, env_file: str | None) -> str:
    return _secret(env_name, env_file) if env_name else ""


def _reset(args: argparse.Namespace) -> Reset | None:
    """The batch's reset. None means scenarios that need a clean state are not run."""
    if args.reset == "none":
        return NoReset()
    if args.reset == "command":
        if not args.reset_command:
            raise SystemExit("--reset command needs --reset-command")
        return CommandReset(args.reset_command, timeout_s=args.reset_timeout)
    if args.reset == "http":
        if not args.reset_url:
            raise SystemExit("--reset http needs --reset-url")
        return HttpReset(
            args.reset_url,
            secret=_named_secret(args.reset_secret_env, args.env_file),
            header=args.reset_header,
            timeout_s=args.reset_timeout,
        )
    return None


def _read_back(args: argparse.Namespace) -> ReadBack | None:
    """The batch's read-back. None means 'stored' is what the agent's tools were sent."""
    if args.read_back == "none":
        return NoReadBack()
    if args.read_back == "command":
        if not args.read_back_command:
            raise SystemExit("--read-back command needs --read-back-command")
        return CommandReadBack(args.read_back_command, timeout_s=args.read_back_timeout)
    if args.read_back == "http":
        if not args.read_back_url:
            raise SystemExit("--read-back http needs --read-back-url")
        return HttpReadBack(
            args.read_back_url,
            secret=_named_secret(args.read_back_secret_env, args.env_file),
            header=args.read_back_header,
            timeout_s=args.read_back_timeout,
        )
    return None


def _clipbank(args: argparse.Namespace) -> int:
    if args.clipbank_command == "build":
        from synthiq_caller.clipbank.build import build

        summary = build(Path(args.recordings), Path(args.out), pad_s=args.pad)
        print(json.dumps(summary, indent=1))
        return 0
    from synthiq_caller.clipbank.from_call import scenario_from_call

    print(json.dumps(scenario_from_call(Path(args.clips), args.call), indent=2))
    return 0


def _target(args: argparse.Namespace) -> dict[str, Any] | None:
    """The profile of the system under test, when one was named."""
    from synthiq_caller.target import load_target

    path = getattr(args, "target", "")
    return load_target(path) if path else None


TARGET_HELP = (
    "the profile of the system under test (see target.py): its bookable times, time zone "
    "and fixture marks. Scenarios that leave parts of their appointment to it are not run "
    "without it"
)


def _batch(args: argparse.Namespace) -> int:
    scenarios = load_scenarios(args.scenarios, _target(args))
    chains = load_chains(args.scenarios)
    if args.only:
        scenarios = [(f, s) for f, s in scenarios if any(o in f for o in args.only)]
        chains = [(f, c) for f, c in chains if any(o in f for o in args.only)]
    hooks = (
        ShellHooks(before=args.before_call, after=args.after_call)
        if (args.before_call or args.after_call)
        else NoHooks()
    )
    budget = NameBudget(
        limit=args.name_budget,
        window_s=args.name_window,
        path=Path(args.name_budget_file) if args.name_budget_file else None,
    )
    out = Path(args.out)
    rehearsal = args.leg == "fake"
    if rehearsal:
        if not args.fake_agent:
            raise SystemExit("--fake-agent (a script file, or a directory of them) is required")
        print(
            "REHEARSAL: these calls go to a scripted stand-in, not an agent. Every run is "
            "marked as a rehearsal and no gate can be met by one.\n"
        )
    result = asyncio.run(
        run_batch(
            scenarios,
            lambda: _build(args),
            _bank(args),
            out,
            repeats=args.repeats,
            max_parallel=args.max_parallel,
            allow_mutating=args.allow_mutating,
            budget=budget,
            hooks=hooks,
            read_trace=not args.no_trace,
            settle_s=args.settle,
            chains=chains,
            reset=_reset(args),
            read_back=_read_back(args),
            allow_unverified_clips=args.allow_unverified_clips,
            space_names=args.space_names,
            prereq=_checker(args),
            make_leg_for=(lambda file: _build_fake(args, file)) if rehearsal else None,
            rehearsal=rehearsal,
        )
    )
    print(f"ran {result.ran}, not run {len(result.blocked)}, chains {result.chains}")
    print()
    return report(str(out), details=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="synthiq-caller")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a scenario one or more times")
    run.add_argument("scenario")
    run.add_argument("--leg", required=True, choices=["elevenlabs-socket"])
    run.add_argument("--agent", default="")
    run.add_argument("--clips", required=True)
    run.add_argument("--repeats", type=int, default=1)
    run.add_argument("--out", required=True)
    run.add_argument("--env-file", default=None)
    run.add_argument("--no-trace", action="store_true", help="do not read the agent's trace")
    run.add_argument("--trace", choices=TRACES, default="elevenlabs", help="where the trace is")
    run.add_argument(
        "--allow-mutating",
        choices=sorted(ALLOWED),
        default="none",
        help="what the scenario may change on the system under test; the default runs "
        "only a scenario that changes nothing",
    )
    run.add_argument("--allow-unverified-clips", action="store_true")
    run.add_argument("--clips-extra", action="append", default=[], help="a further clip bank")
    run.add_argument("--name-budget", type=int, default=2, help="identity checks per name")

    rep = sub.add_parser("report", help="summarise a directory of runs")
    rep.add_argument("directory")
    rep.add_argument("--summary-only", action="store_true", help="skip the per-call detail")

    bat = sub.add_parser("batch", help="run a directory of scenarios with the safety guards on")
    bat.add_argument("scenarios")
    bat.add_argument("--leg", required=True, choices=LEGS)
    bat.add_argument("--target", default="", help=TARGET_HELP)
    bat.add_argument("--agent", default="")
    bat.add_argument(
        "--fake-agent",
        default="",
        help="with --leg fake: the scripted agent, as one script file or a directory holding "
        "one per scenario file (default.json for the rest). A rehearsal of the rig only: "
        "its runs are marked and can meet no gate",
    )
    bat.add_argument("--clips", required=True)
    bat.add_argument("--repeats", type=int, default=1)
    bat.add_argument("--max-parallel", type=int, default=1)
    bat.add_argument("--allow-mutating", choices=sorted(ALLOWED), default="none")
    bat.add_argument("--out", required=True)
    bat.add_argument("--env-file", default=None)
    bat.add_argument("--no-trace", action="store_true")
    bat.add_argument("--trace", choices=TRACES, default="elevenlabs", help="where the trace is")
    bat.add_argument(
        "--allow-unverified-clips",
        action="store_true",
        help="play clips whose label no person has confirmed; results are marked "
        "'labels unverified'",
    )
    bat.add_argument(
        "--clips-extra",
        action="append",
        default=[],
        help="a further clip bank directory (for example synthetic stand-ins); its "
        "manifest's provenance column is carried into every result",
    )
    bat.add_argument(
        "--check-requires-command",
        default="",
        help="run per scenario that has a 'requires': exit 1 (unmet) holds the scenario back",
    )
    bat.add_argument("--check-requires-url", default="", help="as for validate")
    bat.add_argument("--check-requires-secret-env", default="")
    bat.add_argument("--check-requires-header", default="X-Synthiq-Secret")
    bat.add_argument("--check-requires-timeout", type=float, default=60.0, help="seconds")
    bat.add_argument(
        "--space-names",
        action="store_true",
        help="wait for the name budget to free up instead of leaving the call unrun",
    )
    bat.add_argument(
        "--name-budget-file",
        default="",
        help="keep the name budget in this file, so batches run one after another share it",
    )
    bat.add_argument("--only", action="append", default=[], help="run files whose name contains")
    bat.add_argument(
        "--name-budget",
        type=int,
        default=2,
        help="identity checks per name per window, unless the scenario states its own "
        "(identity_budget); 0 switches off",
    )
    bat.add_argument("--name-window", type=float, default=900.0, help="seconds")
    bat.add_argument("--settle", type=float, default=2.0, help="wait before reading a trace")
    bat.add_argument("--before-call", default="", help="command run before each call (reset)")
    bat.add_argument("--after-call", default="", help="command run after each call")
    bat.add_argument(
        "--reset",
        choices=["none", "command", "http"],
        default=None,
        help="how to clean the system under test before a scenario or chain that asks for it; "
        "'none' states it is already clean; left out, those scenarios are not run",
    )
    bat.add_argument("--reset-command", default="", help="exit code 0 means clean")
    bat.add_argument("--reset-url", default="", help="any 2xx answer means clean")
    bat.add_argument("--reset-secret-env", default="", help="variable holding the reset secret")
    bat.add_argument("--reset-header", default="X-Synthiq-Secret")
    bat.add_argument("--reset-timeout", type=float, default=120.0, help="seconds")
    bat.add_argument(
        "--read-back",
        choices=["none", "command", "http"],
        default=None,
        help="how to fetch what the system under test holds after each call; left out or "
        "'none', 'stored' is what the agent's tools were sent and is labelled so",
    )
    bat.add_argument("--read-back-command", default="", help="prints one JSON object")
    bat.add_argument("--read-back-url", default="", help="answers with one JSON object")
    bat.add_argument("--read-back-secret-env", default="", help="variable holding the secret")
    bat.add_argument("--read-back-header", default="X-Synthiq-Secret")
    bat.add_argument("--read-back-timeout", type=float, default=60.0, help="seconds")
    bat.add_argument(
        "--keypad-as-message",
        action="store_true",
        help="socket leg only: send keypad digits as a message marked as keypad input "
        "(no tones are played; not checked against a live agent)",
    )

    reg = sub.add_parser("regrade", help="grade stored runs again; places no calls")
    reg.add_argument("directory")
    reg.add_argument("--scenarios", required=True)
    reg.add_argument("--target", default="", help=TARGET_HELP)
    reg.add_argument("--reread", action="store_true", help="fetch missing or empty traces again")
    reg.add_argument(
        "--reread-all",
        action="store_true",
        help="fetch every trace again, for runs stored by an older version of the reader",
    )
    reg.add_argument("--leg", choices=["elevenlabs-socket"], default="elevenlabs-socket")
    reg.add_argument("--agent", default="-")
    reg.add_argument("--trace", choices=TRACES, default="elevenlabs", help="where the trace is")
    reg.add_argument("--env-file", default=None)

    base = sub.add_parser("baseline", help="store a batch's pass counts to compare against")
    base.add_argument("directory")
    base.add_argument("--out", required=True)

    cmp_ = sub.add_parser("compare", help="set a batch beside a stored baseline")
    cmp_.add_argument("directory")
    cmp_.add_argument("--baseline", required=True)
    cmp_.add_argument(
        "--fail-on",
        choices=["never", "beyond-noise", "any"],
        default="never",
        help="exit 1 on a regression: only one beyond noise, or any drop at all",
    )

    gat = sub.add_parser("gate", help="say whether a batch demonstrates a pass rate")
    gat.add_argument("directory")
    gat.add_argument("--rate", type=float, default=0.95)
    gat.add_argument("--confidence", type=float, default=0.95)
    gat.add_argument(
        "--max-unjudged",
        type=float,
        default=0.1,
        help="the share of attempted calls that may go unjudged (void, ungraded, "
        "ungradeable, not run) before the gate is refused whatever the numbers say",
    )

    val = sub.add_parser("validate", help="check a directory of scenarios, and its clips")
    val.add_argument("scenarios")
    val.add_argument("--clips", default=None)
    val.add_argument("--target", default="", help=TARGET_HELP)
    val.add_argument("--env-file", default=None)
    val.add_argument(
        "--check-requires-command",
        default="",
        help="run per file that has a 'requires': exit 0 means met, 1 unmet, else an error",
    )
    val.add_argument(
        "--check-requires-url",
        default="",
        help='asked per file that has a "requires"; answers {"met": true|false, "detail": "..."}',
    )
    val.add_argument("--check-requires-secret-env", default="", help="variable holding the secret")
    val.add_argument("--check-requires-header", default="X-Synthiq-Secret")
    val.add_argument("--check-requires-timeout", type=float, default=60.0, help="seconds")

    card = sub.add_parser("scorecard", help="one HTML page for one or more run directories")
    card.add_argument("directories", nargs="+")
    card.add_argument("--out", required=True, help="the page to write; its data goes beside it")
    card.add_argument(
        "--previous", default="", help="an earlier scorecard page, its .json, or a baseline file"
    )
    card.add_argument("--faults", default="", help='JSON: {"<directory>/<run file>": "<class>"}')
    card.add_argument("--title", default="")
    val.add_argument("--clips-extra", action="append", default=[], help="a further clip bank")

    bank = sub.add_parser("clipbank", help="build a clip bank, or a scenario from a recorded call")
    bank_sub = bank.add_subparsers(dest="clipbank_command", required=True)
    bank_build = bank_sub.add_parser("build")
    bank_build.add_argument("--recordings", required=True)
    bank_build.add_argument("--out", required=True)
    bank_build.add_argument("--pad", type=float, default=0.15)
    bank_scn = bank_sub.add_parser("scenario")
    bank_scn.add_argument("--clips", required=True)
    bank_scn.add_argument("--call", required=True)

    args = parser.parse_args(argv)
    utf8_console()
    if args.command == "report":
        return report(args.directory, details=not args.summary_only)
    if args.command == "batch":
        return _batch(args)
    if args.command == "regrade":
        if args.reread or args.reread_all:
            _, reader = _build(args)
            if reader is not None:
                fetched = asyncio.run(
                    reread_missing_traces(Path(args.directory), reader, every=args.reread_all)
                )
                print(f"traces fetched again: {fetched}")
        changed = regrade(Path(args.directory), load_scenarios(args.scenarios))
        changed += regrade_chains(Path(args.directory), load_chains(args.scenarios))
        print(f"regraded: {changed} run(s) changed")
        return report(args.directory, details=False)
    if args.command == "validate":
        return validate(args)
    if args.command == "scorecard":
        return _scorecard(args)
    if args.command == "baseline":
        stored = make_baseline(summarise(args.directory), source=str(args.directory))
        save_baseline(stored, args.out)
        print(f"baseline of {len(stored.scenarios)} scenario(s), {len(stored.chains)} chain(s)")
        return 0
    if args.command == "compare":
        baseline = load_baseline(args.baseline)
        changes = compare(baseline, summarise(args.directory))
        print(render_comparison(baseline, changes))
        if args.fail_on == "any":
            return 1 if any(c.regressed for c in changes) else 0
        if args.fail_on == "beyond-noise":
            return 1 if any(c.beyond_noise for c in changes) else 0
        return 0
    if args.command == "clipbank":
        return _clipbank(args)
    if args.command == "gate":
        return _gate(args.directory, args.rate, args.confidence, args.max_unjudged)

    # One scenario goes through the same guards as a batch of them.
    scenario = load_scenario(args.scenario)
    out = Path(args.out)
    result = asyncio.run(
        run_batch(
            [(Path(args.scenario).name, scenario)],
            lambda: _build(args),
            _bank(args),
            out,
            repeats=args.repeats,
            allow_mutating=args.allow_mutating,
            budget=NameBudget(limit=args.name_budget),
            read_trace=not args.no_trace,
            settle_s=2.0,
            allow_unverified_clips=args.allow_unverified_clips,
        )
    )
    for held_back in result.blocked:
        print(f"not run: {held_back.reason}")
    return report(str(out))


if __name__ == "__main__":
    sys.exit(main())


# Kept importable for callers that build their own loops.
__all__ = ["RunRecord", "main", "report"]
