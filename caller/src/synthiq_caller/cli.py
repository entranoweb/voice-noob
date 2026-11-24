"""Command line: run scenarios, then report.

    synthiq-caller run scenario.json --leg elevenlabs-socket --agent <id> \\
        --clips <dir> --repeats 3 --out runs/
    synthiq-caller report runs/
    synthiq-caller batch scenarios/sophie --leg elevenlabs-socket --agent <id> \
        --clips <dir> --repeats 3 --out runs/        (runs lookup-only scenarios)
    synthiq-caller validate scenarios/sophie --clips <dir>
    synthiq-caller regrade runs/ --scenarios scenarios/sophie   (no calls placed)
    synthiq-caller baseline runs/ --out baseline.json
    synthiq-caller compare runs-new/ --baseline baseline.json --fail-on beyond-noise
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
from synthiq_caller.chain import variables_in
from synthiq_caller.clips import ClipBank
from synthiq_caller.compare import (
    compare,
    load_baseline,
    make_baseline,
    render_comparison,
    save_baseline,
)
from synthiq_caller.director import Director, RunRecord
from synthiq_caller.grading import Grade, grade
from synthiq_caller.leg import Leg
from synthiq_caller.readback import CommandReadBack, HttpReadBack, NoReadBack, ReadBack
from synthiq_caller.report import render, summarise
from synthiq_caller.reset import CommandReset, HttpReset, NoReset, Reset
from synthiq_caller.scenario import Scenario, load_scenario
from synthiq_caller.traces.base import Trace, TraceReader


def _secret(name: str, env_file: str | None) -> str:
    value = os.environ.get(name, "")
    if not value and env_file:
        for line in Path(env_file).read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{name}="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not value:
        raise SystemExit(f"{name} is not set (environment or --env-file)")
    return value


def _build(args: argparse.Namespace) -> tuple[Leg, TraceReader | None]:
    """The only place a leg name is turned into engine-specific objects."""
    if args.leg == "elevenlabs-socket":
        from synthiq_caller.legs.elevenlabs_socket import ElevenLabsSocketLeg
        from synthiq_caller.traces.elevenlabs import ElevenLabsTraceReader

        if not args.agent:
            raise SystemExit("--agent is required for the elevenlabs-socket leg")
        key = _secret("ELEVENLABS_API_KEY", args.env_file)
        leg = ElevenLabsSocketLeg(
            key, args.agent, keypad_as_message=getattr(args, "keypad_as_message", False)
        )
        return leg, ElevenLabsTraceReader(key)
    raise SystemExit(f"unknown or unbuilt leg: {args.leg}")


async def _run_once(scenario: Scenario, args: argparse.Namespace, index: int, out: Path) -> Grade:
    leg, reader = _build(args)
    record = await Director(scenario, leg, ClipBank(args.clips)).run()
    trace: Trace | None = None
    trace_error = ""
    if reader is not None and record.call_ref and not args.no_trace:
        try:
            trace = await reader.read(record.call_ref)
        except Exception as error:  # noqa: BLE001 - a missing trace must not lose the run
            trace_error = repr(error)
    result = grade(scenario, record, trace)
    payload: dict[str, Any] = {
        "record": record.to_json(),
        "trace": trace.to_json() if trace else None,
        "trace_error": trace_error,
        "grade": result.to_json(),
    }
    (out / f"run-{index:03d}.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return result


def _print_grade(index: int, payload: dict[str, Any]) -> None:
    g = payload["grade"]
    record = payload["record"]
    print(f"\nrun {index}: {g['outcome']}  ({record['duration_s']}s, {record['ended']})")
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


def validate(directory: str, clips_dir: str | None) -> int:
    """Parse every scenario and, given a clip bank, check each clip it names exists."""
    bank = ClipBank(clips_dir) if clips_dir else None
    problems = 0
    for file, scenario in load_scenarios(directory):
        missing: list[str] = []
        if bank is not None:
            for step in scenario.steps:
                for clip in step.clips:
                    if clip.startswith("missing/") and scenario.missing_clips:
                        continue  # declared as not yet recorded; a batch will not run it
                    try:
                        bank.path(clip)
                    except FileNotFoundError:
                        missing.append(clip)
        count = len(scenario.missing_clips)
        note = f"  needs {count} recording(s), will not run" if count else ""
        state = "ok" if not missing else f"CLIPS NOT IN BANK: {missing}"
        problems += bool(missing)
        print(
            f"{file:<44} mutates={scenario.mutates:<10} "
            f"steps={len(scenario.steps):<3} {state}{note}"
        )
    for file, chain in load_chains(directory):
        missing = []
        if bank is not None:
            for call in chain.calls:
                for step in call.raw.get("steps", ()):
                    for clip in step.get("clips", ()):
                        if variables_in(clip) or (
                            str(clip).startswith("missing/") and call.missing_clips
                        ):
                            continue  # chosen at run time, or declared as not yet recorded
                        try:
                            bank.path(str(clip))
                        except FileNotFoundError:
                            missing.append(str(clip))
        state = "ok" if not missing else f"CLIPS NOT IN BANK: {missing}"
        problems += bool(missing)
        calls = " > ".join(f"{call.id}({call.mutates})" for call in chain.calls)
        print(
            f"{file:<44} mutates={chain.mutates:<10} chain "
            f"clean_state={'yes' if chain.clean_state else 'no'}  {calls}  {state}"
        )
    return 1 if problems else 0


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


def _batch(args: argparse.Namespace) -> int:
    scenarios = load_scenarios(args.scenarios)
    chains = load_chains(args.scenarios)
    if args.only:
        scenarios = [(f, s) for f, s in scenarios if any(o in f for o in args.only)]
        chains = [(f, c) for f, c in chains if any(o in f for o in args.only)]
    hooks = (
        ShellHooks(before=args.before_call, after=args.after_call)
        if (args.before_call or args.after_call)
        else NoHooks()
    )
    budget = NameBudget(limit=args.name_budget, window_s=args.name_window)
    out = Path(args.out)
    result = asyncio.run(
        run_batch(
            scenarios,
            lambda: _build(args),
            ClipBank(args.clips),
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

    rep = sub.add_parser("report", help="summarise a directory of runs")
    rep.add_argument("directory")
    rep.add_argument("--summary-only", action="store_true", help="skip the per-call detail")

    bat = sub.add_parser("batch", help="run a directory of scenarios with the safety guards on")
    bat.add_argument("scenarios")
    bat.add_argument("--leg", required=True, choices=["elevenlabs-socket"])
    bat.add_argument("--agent", default="")
    bat.add_argument("--clips", required=True)
    bat.add_argument("--repeats", type=int, default=1)
    bat.add_argument("--max-parallel", type=int, default=1)
    bat.add_argument("--allow-mutating", choices=sorted(ALLOWED), default="none")
    bat.add_argument("--out", required=True)
    bat.add_argument("--env-file", default=None)
    bat.add_argument("--no-trace", action="store_true")
    bat.add_argument("--only", action="append", default=[], help="run files whose name contains")
    bat.add_argument("--name-budget", type=int, default=6, help="calls per name; 0 switches off")
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
    reg.add_argument("--reread", action="store_true", help="fetch missing or empty traces again")
    reg.add_argument("--leg", choices=["elevenlabs-socket"], default="elevenlabs-socket")
    reg.add_argument("--agent", default="-")
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

    val = sub.add_parser("validate", help="check a directory of scenarios, and its clips")
    val.add_argument("scenarios")
    val.add_argument("--clips", default=None)

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
    if args.command == "report":
        return report(args.directory, details=not args.summary_only)
    if args.command == "batch":
        return _batch(args)
    if args.command == "regrade":
        if args.reread:
            _, reader = _build(args)
            if reader is not None:
                fetched = asyncio.run(reread_missing_traces(Path(args.directory), reader))
                print(f"traces fetched again: {fetched}")
        changed = regrade(Path(args.directory), load_scenarios(args.scenarios))
        changed += regrade_chains(Path(args.directory), load_chains(args.scenarios))
        print(f"regraded: {changed} run(s) changed")
        return report(args.directory, details=False)
    if args.command == "validate":
        return validate(args.scenarios, args.clips)
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

    scenario = load_scenario(args.scenario)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    start = len(list(out.glob("run-*.json")))
    for offset in range(args.repeats):
        asyncio.run(_run_once(scenario, args, start + offset + 1, out))
    return report(str(out))


if __name__ == "__main__":
    sys.exit(main())


# Kept importable for callers that build their own loops.
__all__ = ["RunRecord", "main", "report"]
