"""Run a directory of scenarios, several times each, without doing damage.

Three guards sit in front of every call:

* **The mutation guard.** Each scenario states what it can change on the system
  under test (``mutates``). A batch runs only the levels it was allowed; the
  default allows none, so a batch started by accident changes nothing.
* **The name budget.** Systems that look patients up by name limit how often one
  name may be checked. The limiter refuses a call that would go over, rather
  than let the target lock the name and fail every later call for the wrong
  reason.
* **Hooks.** ``before_call`` and ``after_call`` are where a deployment resets
  the system under test. Nothing here knows how to reset any real system.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from synthiq_caller.clips import ClipBank
from synthiq_caller.director import Director, RunRecord
from synthiq_caller.grading import grade
from synthiq_caller.leg import Leg
from synthiq_caller.scenario import MUTATION_LEVELS, Scenario, load_scenario
from synthiq_caller.traces.base import Trace, TraceReader
from synthiq_caller.turns import TurnSettings

# What each --allow-mutating choice lets through.
ALLOWED: dict[str, tuple[str, ...]] = {
    "none": ("none",),
    "registers": ("none", "registers"),
    "books": ("none", "registers", "books"),
    "all": MUTATION_LEVELS,
}


class Hooks(Protocol):
    async def before_call(self, scenario_file: str, scenario: Scenario, repeat: int) -> None: ...

    async def after_call(self, scenario_file: str, scenario: Scenario, repeat: int) -> None: ...


class NoHooks:
    """The default: do nothing before or after a call."""

    async def before_call(self, scenario_file: str, scenario: Scenario, repeat: int) -> None:
        return None

    async def after_call(self, scenario_file: str, scenario: Scenario, repeat: int) -> None:
        return None


@dataclass
class ShellHooks:
    """Run a command before and after each call; a non-zero exit stops that call.

    The command gets the scenario in its environment: ``SYNTHIQ_SCENARIO_FILE``,
    ``SYNTHIQ_SCENARIO_NAME``, ``SYNTHIQ_MUTATES``, ``SYNTHIQ_REPEAT`` and
    ``SYNTHIQ_PHASE`` (``before`` or ``after``).
    """

    before: str = ""
    after: str = ""
    timeout_s: float = 120.0

    async def _run(self, command: str, phase: str, file: str, scenario: Scenario, n: int) -> None:
        if not command:
            return
        import os

        env = {
            **os.environ,
            "SYNTHIQ_SCENARIO_FILE": file,
            "SYNTHIQ_SCENARIO_NAME": scenario.name,
            "SYNTHIQ_MUTATES": scenario.mutates,
            "SYNTHIQ_REPEAT": str(n),
            "SYNTHIQ_PHASE": phase,
        }
        done = await asyncio.to_thread(
            subprocess.run,
            command,
            shell=True,  # noqa: S602 - the command is the operator's own configuration
            env=env,
            timeout=self.timeout_s,
            capture_output=True,
            text=True,
        )
        if done.returncode != 0:
            raise RuntimeError(f"{phase}_call hook exited {done.returncode}: {done.stderr[-200:]}")

    async def before_call(self, scenario_file: str, scenario: Scenario, repeat: int) -> None:
        await self._run(self.before, "before", scenario_file, scenario, repeat)

    async def after_call(self, scenario_file: str, scenario: Scenario, repeat: int) -> None:
        await self._run(self.after, "after", scenario_file, scenario, repeat)


@dataclass
class NameBudget:
    """At most ``limit`` calls per caller name in any ``window_s`` seconds."""

    limit: int = 6
    window_s: float = 900.0
    clock: Callable[[], float] = time.monotonic
    _used: dict[str, list[float]] = field(default_factory=dict)

    @staticmethod
    def key(name: str) -> str:
        return " ".join(name.lower().split())

    def remaining(self, name: str) -> int:
        now = self.clock()
        recent = [t for t in self._used.get(self.key(name), []) if now - t < self.window_s]
        self._used[self.key(name)] = recent
        return max(0, self.limit - len(recent))

    def take(self, name: str) -> bool:
        """Spend one call for ``name``. False, and nothing spent, when none is left."""
        if not name or self.limit <= 0:
            return True  # no name to protect, or the limiter is switched off
        if self.remaining(name) <= 0:
            return False
        self._used[self.key(name)].append(self.clock())
        return True


@dataclass
class Blocked:
    scenario_file: str
    scenario: str
    repeat: int
    reason: str


@dataclass
class BatchResult:
    out: str
    ran: int = 0
    blocked: list[Blocked] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


LegFactory = Callable[[], tuple[Leg, TraceReader | None]]


def looks_unfinished(trace: Trace, record: RunRecord) -> bool:
    """A trace with nothing in it for a call in which the agent plainly spoke.

    Engines publish a call's record a moment after it ends. Read too soon, it
    comes back empty, and an empty trace graded as it stands would fail a call
    for something the test rig did, not the agent.
    """
    agent_spoke = any(e.get("kind") == "agent" for e in record.timeline)
    return agent_spoke and not trace.tool_calls and not trace.step_path


async def read_trace_when_ready(
    reader: TraceReader,
    record: RunRecord,
    *,
    settle_s: float = 0.0,
    attempts: int = 4,
    retry_s: float = 3.0,
    wait: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> tuple[Trace | None, str]:
    """The call's trace, or None and why. Never returns a trace that looks unfinished."""
    if not record.call_ref:
        return None, "the leg gave no call reference"
    if settle_s:
        await wait(settle_s)
    error = ""
    for attempt in range(attempts):
        if attempt:
            await wait(retry_s)
        try:
            trace = await reader.read(record.call_ref)
        except Exception as caught:  # noqa: BLE001 - a missing trace must not lose the run
            error = repr(caught)
            continue
        if not looks_unfinished(trace, record):
            return trace, ""
        error = "the trace was still empty after the call ended"
    return None, error


def load_scenarios(directory: str | Path) -> list[tuple[str, Scenario]]:
    """Every scenario file in ``directory``, in name order."""
    root = Path(directory)
    files = sorted(p for p in root.iterdir() if p.suffix.lower() in (".json", ".yaml", ".yml"))
    return [(p.name, load_scenario(p)) for p in files]


async def run_batch(
    scenarios: list[tuple[str, Scenario]],
    make_leg: LegFactory,
    clips: ClipBank,
    out: Path,
    *,
    repeats: int = 1,
    max_parallel: int = 1,
    allow_mutating: str = "none",
    budget: NameBudget | None = None,
    hooks: Hooks | None = None,
    read_trace: bool = True,
    turn_settings: TurnSettings | None = None,
    settle_s: float = 0.0,
    wait: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> BatchResult:
    if allow_mutating not in ALLOWED:
        raise ValueError(f"allow_mutating must be one of {sorted(ALLOWED)}")
    allowed = ALLOWED[allow_mutating]
    budget = budget if budget is not None else NameBudget()
    hooks = hooks if hooks is not None else NoHooks()
    out.mkdir(parents=True, exist_ok=True)
    result = BatchResult(out=str(out))
    gate = asyncio.Semaphore(max(1, max_parallel))
    counter = len(list(out.glob("run-*.json")))
    lock = asyncio.Lock()

    async def one(file: str, scenario: Scenario, repeat: int) -> None:
        nonlocal counter
        if scenario.mutates not in allowed:
            result.blocked.append(
                Blocked(
                    file,
                    scenario.name,
                    repeat,
                    f"mutation guard: scenario {scenario.mutates}, batch allows {allow_mutating}",
                )
            )
            return
        if scenario.missing_clips:
            result.blocked.append(
                Blocked(
                    file, scenario.name, repeat, f"missing clips: {list(scenario.missing_clips)}"
                )
            )
            return
        async with gate:
            if not budget.take(scenario.facts.get("name", "")):
                result.blocked.append(
                    Blocked(
                        file,
                        scenario.name,
                        repeat,
                        f"name budget: {budget.limit} calls per {budget.window_s:.0f}s already "
                        f"used for {scenario.facts.get('name', '')!r}",
                    )
                )
                return
            try:
                await hooks.before_call(file, scenario, repeat)
            except Exception as error:  # noqa: BLE001 - a failed reset must stop this call only
                result.blocked.append(Blocked(file, scenario.name, repeat, f"before hook: {error}"))
                return
            leg, reader = make_leg()
            director = Director(scenario, leg, clips, turn_settings=turn_settings)
            record = await director.run()
            trace: Trace | None = None
            trace_error = ""
            if reader is not None and record.call_ref and read_trace:
                trace, trace_error = await read_trace_when_ready(
                    reader, record, settle_s=settle_s, wait=wait
                )
            graded = grade(scenario, record, trace)
            hook_error = ""
            try:
                await hooks.after_call(file, scenario, repeat)
            except Exception as error:  # noqa: BLE001 - recorded, never hidden
                hook_error = f"after hook: {error}"
            payload: dict[str, Any] = {
                "scenario_file": file,
                "mutates": scenario.mutates,
                "repeat": repeat,
                "record": record.to_json(),
                "trace": trace.to_json() if trace else None,
                "trace_error": trace_error,
                "hook_error": hook_error,
                "grade": graded.to_json(),
            }
            async with lock:
                counter += 1
                index = counter
                result.ran += 1
            (out / f"run-{index:03d}.json").write_text(
                json.dumps(payload, indent=1), encoding="utf-8"
            )

    jobs = [
        one(file, scenario, repeat)
        for repeat in range(1, repeats + 1)
        for file, scenario in scenarios
    ]
    await asyncio.gather(*jobs)
    (out / "batch.json").write_text(json.dumps(result.to_json(), indent=1), encoding="utf-8")
    return result


async def reread_missing_traces(out: Path, reader: TraceReader) -> int:
    """Fetch again the trace of every stored run that has none, or an empty one."""
    fetched = 0
    for path in sorted(out.glob("run-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        record = RunRecord.from_json(payload["record"])
        old = Trace.from_json(payload["trace"]) if payload.get("trace") else None
        if old is not None and not looks_unfinished(old, record):
            continue
        trace, error = await read_trace_when_ready(reader, record, attempts=2)
        payload["trace"] = trace.to_json() if trace else None
        payload["trace_error"] = error
        path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        fetched += trace is not None
    return fetched


def regrade(out: Path, scenarios: list[tuple[str, Scenario]]) -> int:
    """Grade stored runs again against the scenarios as they are now. Places no calls.

    For when an expectation or a persona fact was wrong: fix the scenario, then
    re-read the calls already made instead of making them again.
    """
    by_file = dict(scenarios)
    changed = 0
    for path in sorted(out.glob("run-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        scenario = by_file.get(payload.get("scenario_file", ""))
        if scenario is None:
            continue
        record = RunRecord.from_json(payload["record"])
        trace = Trace.from_json(payload["trace"]) if payload.get("trace") else None
        if trace is not None and looks_unfinished(trace, record):
            trace = None  # an empty trace proves nothing: ungraded, not failed
        fresh = grade(scenario, record, trace).to_json()
        if fresh != payload["grade"]:
            payload["grade"] = fresh
            path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
            changed += 1
    return changed
