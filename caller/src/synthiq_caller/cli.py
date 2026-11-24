"""Command line: run scenarios, then report.

    synthiq-caller run scenario.json --leg elevenlabs-socket --agent <id> \\
        --clips <dir> --repeats 3 --out runs/
    synthiq-caller report runs/

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

from synthiq_caller.clips import ClipBank
from synthiq_caller.director import Director, RunRecord
from synthiq_caller.grading import Grade, grade
from synthiq_caller.leg import Leg
from synthiq_caller.scenario import Scenario, load_scenario
from synthiq_caller.stats import describe
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
        return ElevenLabsSocketLeg(key, args.agent), ElevenLabsTraceReader(key)
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
        print(f"  {'field':<14} {'said':<22} {'heard':<26} {'stored':<24} verdict")
        for row in g["fields"]:
            print(
                f"  {row['field']:<14} {str(row['said'])[:21]:<22} "
                f"{str(row['heard'] or '-')[:25]:<26} {str(row['stored'] or '-')[:23]:<24} "
                f"{row['verdict']}"
            )
    if payload.get("trace_error"):
        print(f"  trace could not be read: {payload['trace_error']}")


def report(directory: str) -> int:
    files = sorted(Path(directory).glob("run-*.json"))
    if not files:
        print("no runs found")
        return 1
    outcomes: list[str] = []
    for index, path in enumerate(files, start=1):
        payload = json.loads(path.read_text(encoding="utf-8"))
        outcomes.append(payload["grade"]["outcome"])
        _print_grade(index, payload)
    graded = [o for o in outcomes if o != "ungraded"]
    print(f"\npassed: {describe(graded.count('passed'), len(graded))}")
    if len(graded) != len(outcomes):
        print(f"ungraded runs (no trace or no expectations): {len(outcomes) - len(graded)}")
    return 0


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

    args = parser.parse_args(argv)
    if args.command == "report":
        return report(args.directory)

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
