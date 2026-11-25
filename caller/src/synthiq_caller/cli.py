"""Command line: run scenarios, then report.

    synthiq-caller run scenario.json --leg elevenlabs-socket --agent <id> \\
        --clips <dir> --repeats 3 --out runs/
    synthiq-caller report runs/ --fail-under 95
    synthiq-caller preflight --leg phone --public-url https://... --from +44... --to +44... \\
        --allowed-numbers allowed.txt                (places no call)
    synthiq-caller batch scenarios/sophie --leg phone --public-url https://... \\
        --from +44... --to +44... --allowed-numbers allowed.txt --clips <dir> --out runs/
    synthiq-caller batch scenarios/sophie --leg elevenlabs-socket --agent <id> \
        --clips <dir> --repeats 3 --out runs/        (runs lookup-only scenarios)
    synthiq-caller validate scenarios/sophie --clips <dir> [--check-requires-command "..."]
    synthiq-caller scorecard runs/a runs/b --out card.html [--previous last.html]
    synthiq-caller regrade runs/ --scenarios scenarios/sophie   (no calls placed)
    synthiq-caller baseline runs/ --out baseline.json
    synthiq-caller compare runs-new/ --baseline baseline.json --fail-on beyond-noise
    synthiq-caller gate runs/ --rate 0.95 --confidence 0.95
    synthiq-caller acceptance runs/a runs/b --claim 0.95 --out report.html
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

from synthiq_caller import manifest
from synthiq_caller.batch import (
    ALLOWED,
    BatchResult,
    Hooks,
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
from synthiq_caller.report import fail_under_exit, gate_reading, render, summarise
from synthiq_caller.reset import CommandReset, HttpReset, NoReset, Reset
from synthiq_caller.scenario import ScenarioError, load_scenario
from synthiq_caller.stats import describe
from synthiq_caller.traces.base import TraceReader
from synthiq_caller.validation import (
    load_problems,
    ungradeable_problems,
    validate_bank,
    validate_directory,
)


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


LEGS = ["elevenlabs-socket", "phone", "fake"]


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
        out = getattr(args, "out", None)
        leg = ElevenLabsSocketLeg(
            key,
            args.agent,
            keypad_as_message=getattr(args, "keypad_as_message", False),
            audio_dir=str(Path(out) / "audio")
            if out and not getattr(args, "no_audio", False)
            else "",
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


def report(directory: str, *, details: bool = True, fail_under: float | None = None) -> int:
    files = sorted(Path(directory).glob("run-*.json"))
    if not files and not (Path(directory) / "batch.json").exists():
        print("no runs found")
        return 1
    if details:
        for index, path in enumerate(files, start=1):
            _print_grade(index, json.loads(path.read_text(encoding="utf-8")))
        print()
    summary = summarise(directory)
    print(render(summary))
    code, line = fail_under_exit(summary, fail_under)
    if line:
        print(f"\n{line}")
    return code


BANK_LINES = 10  # findings of one kind listed before the rest are only counted


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
    try:
        rows = validate_directory(
            args.scenarios, _bank(args) if args.clips else None, checker, _target(args)
        )
    except (ScenarioError, ValueError) as error:
        # A file that does not load is an error with or without --strict.
        for line in load_problems(args.scenarios, _target(args)) or [str(error)]:
            print(f"ERROR: {line}")
        return 1
    for row in rows:
        print(row.line())
    findings = validate_bank(_bank(args)) if args.clips else []
    if findings:
        print(f"\nclip bank ({len(findings)} finding(s)):")
        shown: dict[str, int] = {}
        for finding in findings:
            shown[finding.kind] = shown.get(finding.kind, 0) + 1
            if finding.problem or shown[finding.kind] <= BANK_LINES:
                print("  " + finding.line())
        for kind, count in shown.items():
            if count > BANK_LINES and kind not in (
                "past retention",
                "invalid field",
                "consent record missing",
                "unfinished removal",
            ):
                print(f"  ... {count - BANK_LINES} more {kind} finding(s) not listed")
    unchecked = sum(row.requires_state == "unchecked" for row in rows)
    if unchecked:
        print(
            f"\n{unchecked} file(s) require something of the system under test that was not "
            "checked (--check-requires-command or --check-requires-url)."
        )
    ungradeable = sum(not row.states_expectations for row in rows)
    if ungradeable:
        print(
            f"\n{ungradeable} scenario(s) UNGRADEABLE until expectations are stated: they ask "
            "nothing of a call, so no call can pass them, and a batch holds them back "
            "(--allow-ungradeable)."
        )
    strict = ungradeable_problems(rows) if args.strict else []
    for line in strict:
        print(f"ERROR: {line}")
    if strict:
        print(
            f"\n{len(strict)} scenario(s) cannot be graded and --strict is on: state their "
            "expectations (expect.*) before they go into a gate or an acceptance run."
        )
    broken = any(row.problem for row in rows) or any(f.problem for f in findings)
    return 1 if strict or broken else 0


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

    The rule is ``report.gate_reading``; this prints it.
    """
    read = gate_reading(summarise(directory), rate, confidence, max_unjudged)
    print(f"by call:     {read.by_call}")
    print(f"by scenario: {read.by_scenario}")
    print(
        f"attempted: {read.attempted} call(s); placed {read.placed}; counted {read.counted}. "
        f"Not judged: {read.unjudged} ({read.share:.0%}): void {read.void} (the rig's fault, "
        f"re-run them), ungraded {read.ungraded}, ungradeable {read.ungradeable}, "
        f"not run {read.not_run}. Calls that were placed and could not be graded count against."
    )
    if read.met_calls and not read.met_scenarios:
        print("The by-call reading is met only by counting repeats as independent calls.")
    if read.refused:
        print("NOT met: " + "; ".join(read.refused) + ".")
        return 1
    return 0 if read.met else 1


def _acceptance(args: argparse.Namespace) -> int:
    """Write the acceptance report. 0: the claim is met. 1: it is not. 2: no report was made."""
    from synthiq_caller import acceptance

    try:
        found = acceptance.build(
            [Path(d) for d in args.directories],
            claim=args.claim,
            confidence=args.confidence,
            max_unjudged=args.max_unjudged,
            allow_mixed=args.allow_mixed_scenarios,
            allow_no_manifest=args.allow_no_manifest,
            title=args.title,
        )
    except acceptance.Refused as error:
        print(f"acceptance report refused: {error}")
        return 2
    out = acceptance.write(found, args.out)
    print(f"{found['verdict']}: {found['verdict_reason']}.  ->  {out}")
    return 0 if found["verdict"] == acceptance.MET else 1


TARGET_LABEL_HELP = (
    "a name for the system under test, written into the run manifest and onto reports; "
    "keep it neutral"
)
TARGET_VERSION_HELP = (
    "the version or configuration of the system under test, as you state it; the rig does "
    "not ask the platform, and reports say the version is as stated"
)


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
    return ClipBank(
        args.clips,
        extra=tuple(getattr(args, "clips_extra", None) or ()),
        allow_missing_consent=bool(getattr(args, "allow_missing_consent", False)),
    )


def _named_secret(env_name: str, env_file: str | None) -> str:
    return _secret(env_name, env_file) if env_name else ""


def _dry_secret(env_name: str, env_file: str | None, dry: bool) -> str:
    """The named secret; in a dry run a stand-in, so that none is read or needed."""
    if dry:
        return "not-read-in-a-dry-run" if env_name else ""
    return _named_secret(env_name, env_file)


def _hooks(args: argparse.Namespace) -> Hooks:
    """The batch's hooks: the command line's, else the target profile's."""
    from synthiq_caller.target import command_of

    target = _target(args)
    before: str | list[str] = args.before_call or command_of(target, "before_call")[0]
    after: str | list[str] = args.after_call or command_of(target, "after_call")[0]
    if not (before or after):
        return NoHooks()
    limits = [command_of(target, name)[1] for name in ("before_call", "after_call")]
    return ShellHooks(
        before=before,
        after=after,
        timeout_s=max([120.0, *filter(None, limits)]),
        # The profile's own after command only; one given on the command line runs always.
        after_only_when_mutating=not args.after_call
        and bool(((target or {}).get("after_call") or {}).get("only_when_mutating")),
    )


def _reset(args: argparse.Namespace, dry: bool = False) -> Reset | None:
    """The batch's reset. None means scenarios that need a clean state are not run.

    Left off the command line, it is the target profile's, when that gives one.
    ``dry`` builds it for a dry run: no secret is read.
    """
    from synthiq_caller.target import command_of

    if args.reset is None:
        command, limit = command_of(_target(args), "reset")
        return CommandReset(command, timeout_s=limit or args.reset_timeout) if command else None
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
            secret=_dry_secret(args.reset_secret_env, args.env_file, dry),
            header=args.reset_header,
            timeout_s=args.reset_timeout,
        )
    return None


def _read_back(args: argparse.Namespace, dry: bool = False) -> ReadBack | None:
    """The batch's read-back. None means 'stored' is what the agent's tools were sent.

    Left off the command line, it is the target profile's, when that gives one.
    """
    from synthiq_caller.target import command_of

    if args.read_back is None:
        command, limit = command_of(_target(args), "read_back")
        if command:
            return CommandReadBack(command, timeout_s=limit or args.read_back_timeout)
        return None
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
            secret=_dry_secret(args.read_back_secret_env, args.env_file, dry),
            header=args.read_back_header,
            timeout_s=args.read_back_timeout,
        )
    return None


def _clipbank(args: argparse.Namespace) -> int:
    if args.clipbank_command == "build":
        from synthiq_caller.clipbank.build import build

        given = {
            "provenance": args.provenance,
            "source_kind": args.source_kind,
            "speaker_ref": args.speaker_ref,
            "lawful_basis": args.lawful_basis,
            "consent_ref": args.consent_ref,
            "recorded_on": args.recorded_on,
            "retain_until": args.retain_until,
        }
        per_call = (
            json.loads(Path(args.call_fields).read_text(encoding="utf-8"))
            if args.call_fields
            else {}
        )
        try:
            summary = build(
                Path(args.recordings),
                Path(args.out),
                pad_s=args.pad,
                consent={k: v for k, v in given.items() if v},
                consent_by_call=per_call,
            )
        except ValueError as error:
            raise SystemExit(f"clipbank build: {error}") from error
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
    from synthiq_caller.target import exceeds_allowance

    target = _target(args)
    if exceeds_allowance(target, args.allow_mutating):
        # Checked before anything is loaded or run: the profile is the statement
        # of what this system may have done to it.
        raise SystemExit(
            f"--allow-mutating {args.allow_mutating} is more than the target profile "
            f"{target['name'] if target else ''!r} permits "
            f"(allow_mutating: {target['allow_mutating'] if target else ''})"
        )
    scenarios = load_scenarios(args.scenarios, target)
    chains = load_chains(args.scenarios, target)
    if args.only:
        scenarios = [(f, s) for f, s in scenarios if any(o in f for o in args.only)]
        chains = [(f, c) for f, c in chains if any(o in f for o in args.only)]
    hooks = _hooks(args)
    if args.dry_run:
        from synthiq_caller.plan import dry_run

        for line in dry_run(
            scenarios,
            chains,
            leg=args.leg,
            allow_mutating=args.allow_mutating,
            allow_ungradeable=args.allow_ungradeable,
            repeats=args.repeats,
            reset=_reset(args, dry=True),
            read_back=_read_back(args, dry=True),
            hooks=hooks,
            target=target,
        ):
            print(line)
        return 0
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
    phone = None
    if args.leg == "phone":
        from synthiq_caller.phone_batch import environment, open_phone_batch

        phone = open_phone_batch(args, environment(args.env_file), out)
    bank = _bank(args)
    batch_id = manifest.open_for(args, out, args.scenarios, scenarios, chains, bank)

    async def go() -> BatchResult:
        try:
            return await run_batch(
                scenarios,
                phone.make_leg if phone is not None else (lambda: _build(args)),
                bank,
                out,
                repeats=args.repeats,
                max_parallel=args.max_parallel,
                allow_mutating=args.allow_mutating,
                allow_ungradeable=args.allow_ungradeable,
                budget=budget,
                hooks=hooks,
                read_trace=not args.no_trace,
                turn_settings=phone.turn_settings if phone is not None else None,
                settle_s=args.settle,
                chains=chains,
                reset=_reset(args),
                read_back=_read_back(args),
                allow_unverified_clips=args.allow_unverified_clips,
                allow_unknown_provenance=args.allow_unknown_provenance,
                allow_missing_consent=args.allow_missing_consent,
                space_names=args.space_names,
                prereq=_checker(args),
                leg_name=args.leg,
                make_leg_for=(lambda file: _build_fake(args, file)) if rehearsal else None,
                rehearsal=rehearsal,
                batch_id=batch_id,
            )
        finally:
            manifest.close_batch(out, batch_id)
            if phone is not None:
                await phone.aclose()

    result = asyncio.run(go())
    print(f"ran {result.ran}, not run {len(result.blocked)}, chains {result.chains}")
    print()
    return report(str(out), details=False, fail_under=args.fail_under)


def _phone_arguments(parser: argparse.ArgumentParser) -> None:
    """What the phone leg needs to know, for ``batch`` and ``preflight`` alike."""
    parser.add_argument("--public-url", default="", help="https address the carrier can reach")
    parser.add_argument("--from", dest="from_number", default="", help="number to dial from")
    parser.add_argument("--to", default="", help="number to dial")
    parser.add_argument(
        "--allowed-numbers",
        default="",
        help="the only numbers that may be dialled: a file, one per line, or a comma-separated "
        "list. Nothing is dialled without it",
    )
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - the carrier connects in
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--transcriber", choices=["none", "scripted", "deepgram"], default="deepgram"
    )
    parser.add_argument("--agent-lines", default=None, help="one agent line per row (scripted)")
    parser.add_argument("--language", default="en-GB")


def _preflight(args: argparse.Namespace) -> int:
    from synthiq_caller.phone_batch import environment, phone_preflight, render_preflight
    from synthiq_caller.phone_server import check_public_url

    checks = asyncio.run(
        phone_preflight(
            env=environment(args.env_file),
            to_number=args.to,
            from_number=args.from_number,
            public_url=check_public_url(args.public_url),
            allowed_argument=args.allowed_numbers,
            host=args.host,
            port=args.port,
            transcriber=args.transcriber,
            trace=not args.no_trace,
        )
    )
    text, code = render_preflight(checks)
    print(text)
    return code


def _reread_reader(args: argparse.Namespace) -> TraceReader | None:
    """The reader ``regrade --reread`` fetches traces with, for the leg the runs used."""
    if args.leg != "phone":
        return _build(args)[1]
    from synthiq_caller.traces.elevenlabs import ElevenLabsTraceReader
    from synthiq_caller.traces.lookup import (
        ElevenLabsNumberTimeFinder,
        LookupTraceReader,
        calls_in,
    )

    key = _secret("ELEVENLABS_API_KEY", args.env_file)
    agent = "" if args.agent == "-" else args.agent
    finder = ElevenLabsNumberTimeFinder(key, agent_id=agent, window_s=args.trace_window)
    return LookupTraceReader(
        finder, ElevenLabsTraceReader(key), calls_in(Path(args.directory)), find_wait_s=0.0
    )


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
    run.add_argument("--no-audio", action="store_true", help="keep no per-side WAV files")
    run.add_argument("--trace", choices=TRACES, default="elevenlabs", help="where the trace is")
    run.add_argument(
        "--allow-mutating",
        choices=sorted(ALLOWED),
        default="none",
        help="what the scenario may change on the system under test; the default runs "
        "only a scenario that changes nothing",
    )
    run.add_argument("--allow-unverified-clips", action="store_true")
    run.add_argument(
        "--allow-unknown-provenance",
        action="store_true",
        help="play clips whose manifest does not say if they are real or synthetic; "
        "results are marked 'provenance unknown'",
    )
    run.add_argument(
        "--allow-missing-consent",
        action="store_true",
        help="play real clips that have no lawful basis or consent record (special-category "
        "data); results say 'consent record missing'. Not implied by --allow-unknown-provenance",
    )
    run.add_argument(
        "--allow-ungradeable",
        action="store_true",
        help="place a call for a scenario that states no expectations; it can only be ungraded",
    )
    run.add_argument("--clips-extra", action="append", default=[], help="a further clip bank")
    run.add_argument("--name-budget", type=int, default=2, help="identity checks per name")
    run.add_argument("--target-label", default="", help=TARGET_LABEL_HELP)
    run.add_argument("--target-version", default="", help=TARGET_VERSION_HELP)

    rep = sub.add_parser("report", help="summarise a directory of runs")
    rep.add_argument("directory")
    rep.add_argument("--summary-only", action="store_true", help="skip the per-call detail")
    rep.add_argument(
        "--fail-under",
        type=float,
        default=None,
        help="exit 1 when fewer than this percentage of the calls placed passed",
    )

    bat = sub.add_parser("batch", help="run a directory of scenarios with the safety guards on")
    bat.add_argument("scenarios")
    bat.add_argument("--leg", required=True, choices=LEGS)
    bat.add_argument("--target", default="", help=TARGET_HELP)
    bat.add_argument("--target-label", default="", help=TARGET_LABEL_HELP)
    bat.add_argument("--target-version", default="", help=TARGET_VERSION_HELP)
    bat.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be held back and every reset, hook and read-back command "
        "that would be used, then stop: nothing is run, called, placed or written",
    )
    _phone_arguments(bat)
    bat.add_argument("--no-record", action="store_true", help="phone: no carrier recording")
    bat.add_argument(
        "--no-audio", action="store_true", help="keep no per-side WAV files (either leg)"
    )
    bat.add_argument(
        "--trace-window",
        type=float,
        default=120.0,
        help="phone: seconds either side of the call in which to look for its trace",
    )
    bat.add_argument(
        "--trace-ignore-caller-number",
        action="store_true",
        help="phone: match the trace on the dialled number and time only "
        "(for a line that withholds the caller's number)",
    )
    bat.add_argument(
        "--fail-under",
        type=float,
        default=None,
        help="exit 1 when fewer than this percentage of the calls placed passed",
    )
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
    bat.add_argument(
        "--allow-ungradeable",
        action="store_true",
        help="place calls for scenarios that state no expectations; they can only be ungraded",
    )
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
        "--allow-unknown-provenance",
        action="store_true",
        help="play clips whose manifest does not say if they are real or synthetic; "
        "results are marked 'provenance unknown'",
    )
    bat.add_argument(
        "--allow-missing-consent",
        action="store_true",
        help="play real clips that have no lawful basis or consent record (special-category "
        "data); results say 'consent record missing'. Not implied by --allow-unknown-provenance",
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
    reg.add_argument("--leg", choices=["elevenlabs-socket", "phone"], default="elevenlabs-socket")
    reg.add_argument("--trace-window", type=float, default=120.0, help="phone: seconds")
    reg.add_argument("--agent", default="-")
    reg.add_argument("--trace", choices=TRACES, default="elevenlabs", help="where the trace is")
    reg.add_argument("--env-file", default=None)
    reg.add_argument(
        "--allow-mixed-scenarios",
        action="store_true",
        help="regrade even though the stored runs were made against different versions of "
        "the same scenario file; all of them then answer to the scenarios as they are now",
    )

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

    pre = sub.add_parser("preflight", help="check a leg is ready, without placing a call")
    pre.add_argument("--leg", required=True, choices=["phone"])
    _phone_arguments(pre)
    pre.add_argument("--env-file", default=None)
    pre.add_argument("--no-trace", action="store_true", help="the calls will not be graded")

    acc = sub.add_parser(
        "acceptance", help="one dated page: what was tested, what passed, what it does not show"
    )
    acc.add_argument("directories", nargs="+")
    acc.add_argument("--claim", type=float, default=0.95, help="the pass rate being claimed")
    acc.add_argument("--confidence", type=float, default=0.95)
    acc.add_argument("--out", required=True, help="the page to write; its data goes beside it")
    acc.add_argument("--title", default="")
    acc.add_argument(
        "--max-unjudged",
        type=float,
        default=0.1,
        help="as for gate: the share of attempted calls that may go unjudged",
    )
    acc.add_argument(
        "--allow-mixed-scenarios",
        action="store_true",
        help="report on runs made against different versions of a scenario file; the page "
        "lists the versions and says they are not one test",
    )
    acc.add_argument(
        "--allow-no-manifest",
        action="store_true",
        help="report on runs that have no manifest; the page says what could not be recorded",
    )

    val = sub.add_parser("validate", help="check a directory of scenarios, and its clips")
    val.add_argument("scenarios")
    val.add_argument("--clips", default=None)
    val.add_argument(
        "--allow-missing-consent",
        action="store_true",
        help="report real clips with no lawful basis or consent record without failing",
    )
    val.add_argument("--target", default="", help=TARGET_HELP)
    val.add_argument("--env-file", default=None)
    val.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 when any scenario cannot be graded (it states no expectations). Without it "
        "that is only a warning, so a freshly captured scenario can still be validated. Only "
        "gradeability is strict: an unverified clip label or an unchecked prerequisite does not "
        "fail it. The CI gate and acceptance apply this check themselves",
    )
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
    from synthiq_caller.consent import PROVENANCES, SOURCE_KINDS

    bank_build.add_argument(
        "--provenance",
        choices=PROVENANCES,
        default="real",
        help="what the recordings are: a person's voice (real) or a made one (synthetic)",
    )
    bank_build.add_argument("--source-kind", choices=SOURCE_KINDS, default="")
    bank_build.add_argument("--speaker-ref", default="", help="opaque id, spk_..., never a name")
    bank_build.add_argument(
        "--lawful-basis", default="", help="Article 6 basis and Article 9 condition"
    )
    bank_build.add_argument("--consent-ref", default="", help="where the consent record is held")
    bank_build.add_argument("--recorded-on", default="", help="YYYY-MM-DD")
    bank_build.add_argument("--retain-until", default="", help="YYYY-MM-DD")
    bank_build.add_argument(
        "--call-fields",
        default="",
        help='JSON {"<call id>": {"speaker_ref": ..., ...}} for fields that differ per call',
    )
    bank_scn = bank_sub.add_parser("scenario")
    bank_scn.add_argument("--clips", required=True)
    bank_scn.add_argument("--call", required=True)

    from synthiq_caller.upload import register as register_upload

    register_upload(sub)
    from synthiq_caller.forget import register as register_forget

    register_forget(sub)
    from synthiq_caller.capture import register as register_capture

    register_capture(sub)  # also adds `ingest`
    from synthiq_caller.onboarding import register as register_onboarding
    from synthiq_caller.reference import register as register_reference

    register_onboarding(sub)  # `init` and `doctor`
    register_reference(sub)

    args = parser.parse_args(argv)
    # Kept for the run manifest. Secrets are never on the command line: they are
    # named by environment variable.
    args.argv_raw = list(argv) if argv is not None else sys.argv[1:]
    utf8_console()
    if args.command in (
        "upload",
        "forget",
        "capture",
        "ingest",
        "init",
        "doctor",
        "reference",
    ):
        return int(args.handler(args))
    if args.command == "report":
        return report(args.directory, details=not args.summary_only, fail_under=args.fail_under)
    if args.command == "preflight":
        return _preflight(args)
    if args.command == "batch":
        return _batch(args)
    if args.command == "regrade":
        try:
            manifest.refuse_mixed(
                [args.directory],
                allow=args.allow_mixed_scenarios,
                remedy="Regrading would grade them all against the scenarios as they are now",
            )
        except manifest.MixedScenarios as error:
            print(f"regrade refused: {error}")
            return 2
        if args.reread or args.reread_all:
            reader = _reread_reader(args)
            if reader is not None:
                fetched = asyncio.run(
                    reread_missing_traces(Path(args.directory), reader, every=args.reread_all)
                )
                print(f"traces fetched again: {fetched}")
        profile = _target(args)
        scenarios = load_scenarios(args.scenarios, profile)
        chains = load_chains(args.scenarios, profile)
        changed = regrade(Path(args.directory), scenarios)
        changed += regrade_chains(Path(args.directory), chains)
        manifest.record_regrade(
            args.directory,
            scenarios=manifest.scenario_identity(
                args.scenarios, [file for file, _ in scenarios] + [file for file, _ in chains]
            ),
            changed=changed,
            argv=args.argv_raw,
        )
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
    if args.command == "acceptance":
        return _acceptance(args)
    if args.command == "gate":
        return _gate(args.directory, args.rate, args.confidence, args.max_unjudged)

    # One scenario goes through the same guards as a batch of them.
    scenario = load_scenario(args.scenario)
    out = Path(args.out)
    one = [(Path(args.scenario).name, scenario)]
    clip_bank = _bank(args)
    batch_id = manifest.open_for(args, out, Path(args.scenario).parent, one, [], clip_bank)
    result = asyncio.run(
        run_batch(
            one,
            lambda: _build(args),
            clip_bank,
            out,
            batch_id=batch_id,
            repeats=args.repeats,
            allow_mutating=args.allow_mutating,
            allow_ungradeable=args.allow_ungradeable,
            budget=NameBudget(limit=args.name_budget),
            read_trace=not args.no_trace,
            settle_s=2.0,
            allow_unverified_clips=args.allow_unverified_clips,
            allow_unknown_provenance=args.allow_unknown_provenance,
            allow_missing_consent=args.allow_missing_consent,
        )
    )
    manifest.close_batch(out, batch_id)
    for held_back in result.blocked:
        print(f"not run: {held_back.reason}")
    return report(str(out))


if __name__ == "__main__":
    sys.exit(main())


# Kept importable for callers that build their own loops.
__all__ = ["RunRecord", "main", "report"]
