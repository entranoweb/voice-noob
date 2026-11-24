"""Run a directory of scenarios, several times each, without doing damage.

Three guards sit in front of every call:

* **The mutation guard.** Each scenario states what it can change on the system
  under test (``mutates``). A batch runs only the levels it was allowed; the
  default allows none, so a batch started by accident changes nothing.
* **The name budget.** Systems that look patients up by name limit how often one
  name may be checked. The limiter refuses a call that would go over, rather
  than let the target lock the name and fail every later call for the wrong
  reason.
* **The reset.** A scenario or chain that needs a clean start gets the batch's
  reset first (``reset.py``). When that fails, or none is configured, the call
  is not run. Nothing here knows how to reset any real system.

``before_call`` and ``after_call`` hooks run around every call, whatever it needs.

After a call the batch's read-back, when there is one, fetches what the system
under test actually holds (``readback.py``); the grader prefers that to the
agent's own account.

A chain of calls (``chain.py``) passes through the same guards as one unit: it
runs only when every one of its calls is allowed, and it stops at the first call
that does not pass.
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

from synthiq_caller.chain import (
    Chain,
    MissingVariable,
    capture_values,
    chain_outcome,
    is_chain_file,
    load_chain,
)
from synthiq_caller.clips import ClipBank
from synthiq_caller.director import Director, RunRecord
from synthiq_caller.grading import Grade, grade
from synthiq_caller.jsonio import write_json
from synthiq_caller.leg import Leg
from synthiq_caller.readback import ReadBack, ReadBackContext
from synthiq_caller.reset import Reset, ResetContext, ResetFailed
from synthiq_caller.scenario import MUTATION_LEVELS, Scenario, ScenarioError, load_scenario
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
    chains: int = 0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class NotRun(Exception):
    """A call the rig decided not to place. Never a mark against the agent."""


@dataclass
class Placed:
    """One call that was placed, as the chain runner needs it."""

    run: str
    grade: Grade
    record: RunRecord
    trace: Trace | None
    read_back: dict[str, str] | None


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


def _scenario_files(directory: str | Path) -> list[Path]:
    root = Path(directory)
    return sorted(p for p in root.iterdir() if p.suffix.lower() in (".json", ".yaml", ".yml"))


def load_scenarios(directory: str | Path) -> list[tuple[str, Scenario]]:
    """Every single-call scenario file in ``directory``, in name order."""
    return [(p.name, load_scenario(p)) for p in _scenario_files(directory) if not is_chain_file(p)]


def load_chains(directory: str | Path) -> list[tuple[str, Chain]]:
    """Every chain file in ``directory``, in name order."""
    return [(p.name, load_chain(p)) for p in _scenario_files(directory) if is_chain_file(p)]


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
    chains: list[tuple[str, Chain]] | None = None,
    reset: Reset | None = None,
    read_back: ReadBack | None = None,
) -> BatchResult:
    if allow_mutating not in ALLOWED:
        raise ValueError(f"allow_mutating must be one of {sorted(ALLOWED)}")
    allowed = ALLOWED[allow_mutating]
    chains = chains or []
    wants_reset = any(s.clean_state for _, s in scenarios) or any(c.clean_state for _, c in chains)
    if reset is not None and wants_reset and max_parallel > 1:
        # A reset while another call is on the line would pull the state from under it.
        raise ValueError("a batch that resets the system under test cannot run calls in parallel")
    if chains and max_parallel > 1:
        raise ValueError("a batch with chains cannot run calls in parallel: they share state")
    budget = budget if budget is not None else NameBudget()
    hooks = hooks if hooks is not None else NoHooks()
    out.mkdir(parents=True, exist_ok=True)
    result = BatchResult(out=str(out))
    gate = asyncio.Semaphore(max(1, max_parallel))
    counter = len(list(out.glob("run-*.json")))
    chain_counter = len(list(out.glob("chain-*.json")))
    lock = asyncio.Lock()

    async def clean(context: ResetContext) -> None:
        if reset is None:
            raise NotRun("needs a clean state and the batch has no reset configured")
        try:
            await reset.reset(context)
        except ResetFailed as error:
            raise NotRun(str(error)) from error
        except Exception as error:  # noqa: BLE001 - whatever a reset does, it stops this call only
            raise NotRun(f"reset failed: {error!r}") from error

    async def place(file: str, scenario: Scenario, repeat: int, extra: dict[str, Any]) -> Placed:
        """Place one call and store it. Raises ``NotRun`` when the rig holds it back."""
        nonlocal counter
        if not budget.take(scenario.facts.get("name", "")):
            raise NotRun(
                f"name budget: {budget.limit} calls per {budget.window_s:.0f}s already "
                f"used for {scenario.facts.get('name', '')!r}"
            )
        try:
            await hooks.before_call(file, scenario, repeat)
        except Exception as error:  # noqa: BLE001 - a failed hook must stop this call only
            raise NotRun(f"before hook: {error}") from error
        leg, reader = make_leg()
        director = Director(scenario, leg, clips, turn_settings=turn_settings)
        record = await director.run()
        trace: Trace | None = None
        trace_error = ""
        if reader is not None and record.call_ref and read_trace:
            trace, trace_error = await read_trace_when_ready(
                reader, record, settle_s=settle_s, wait=wait
            )
        held: dict[str, str] | None = None
        read_back_error = ""
        if read_back is not None:
            try:
                held = await read_back.read(
                    ReadBackContext(
                        file, scenario.name, repeat, record.call_ref or "", dict(scenario.facts)
                    )
                )
            except Exception as error:  # noqa: BLE001 - recorded; the grader falls back
                read_back_error = str(error) or repr(error)
        graded = grade(scenario, record, trace, held)
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
            # None: no read-back was made. An object: what the system held afterwards.
            "read_back": held,
            "read_back_error": read_back_error,
            "hook_error": hook_error,
            "grade": graded.to_json(),
            **extra,
        }
        async with lock:
            counter += 1
            index = counter
            result.ran += 1
        name = f"run-{index:03d}.json"
        write_json(out / name, payload)
        return Placed(name, graded, record, trace, held)

    async def one(file: str, scenario: Scenario, repeat: int) -> None:
        def block(reason: str) -> None:
            result.blocked.append(Blocked(file, scenario.name, repeat, reason))

        if scenario.mutates not in allowed:
            block(f"mutation guard: scenario {scenario.mutates}, batch allows {allow_mutating}")
            return
        if scenario.missing_clips:
            block(f"missing clips: {list(scenario.missing_clips)}")
            return
        async with gate:
            try:
                if scenario.clean_state:
                    await clean(ResetContext(file, scenario.name, scenario.mutates, repeat))
                await place(file, scenario, repeat, {})
            except NotRun as held_back:
                block(str(held_back))

    async def one_chain(file: str, chain: Chain, repeat: int) -> None:
        nonlocal chain_counter
        rows: list[dict[str, Any]] = [
            {"id": call.id, "run": None, "outcome": "not run", "reason": ""} for call in chain.calls
        ]
        variables: dict[str, str] = {}
        reason = ""
        uncaptured = False

        def stop(at: int, why: str, later: str) -> None:
            """Mark the call at ``at`` and every later one as not run."""
            nonlocal reason
            reason = reason or why
            for offset, call in enumerate(chain.calls[at:]):
                text = why if offset == 0 else later
                rows[at + offset]["reason"] = text
                result.blocked.append(Blocked(file, f"{chain.name} / {call.id}", repeat, text))

        async def calls() -> None:
            nonlocal reason, uncaptured
            for index, call in enumerate(chain.calls):
                later = f"chain stopped: call {call.id!r} was not run"
                try:
                    scenario = call.scenario(variables)
                except MissingVariable as gone:
                    stop(index, f"no value for {{{{{gone.args[0]}}}}}", later)
                    return
                except ScenarioError as error:
                    stop(index, f"the call is not valid once filled in: {error}", later)
                    return
                extra = {
                    "chain": {"file": file, "name": chain.name, "id": call.id},
                    "variables": dict(variables),
                }
                try:
                    placed = await place(file, scenario, repeat, extra)
                except NotRun as held_back:
                    stop(index, str(held_back), later)
                    return
                rows[index].update(run=placed.run, outcome=placed.grade.outcome)
                rest = index + 1
                if placed.grade.outcome != "passed":
                    why = f"chain stopped: call {call.id!r} {placed.grade.outcome}"
                    reason = why
                    stop(rest, why, why)
                    return
                captured, lost = capture_values(
                    call.captures, placed.record, placed.trace, placed.grade, placed.read_back
                )
                variables.update(captured)
                if lost and rest < len(chain.calls):
                    uncaptured = True
                    rows[index]["reason"] = f"could not capture {lost}"
                    why = f"chain stopped: call {call.id!r} gave no value for {lost}"
                    stop(rest, why, why)
                    return
            return None

        refused = [c.id for c in chain.calls if c.mutates not in allowed]
        unrecorded = [c.id for c in chain.calls if c.missing_clips]
        async with gate:
            if refused:
                why = (
                    f"mutation guard: chain {chain.mutates} (calls {refused}), "
                    f"batch allows {allow_mutating}"
                )
                stop(0, why, why)
            elif unrecorded:
                why = f"missing clips in calls {unrecorded}"
                stop(0, why, why)
            else:
                try:
                    if chain.clean_state:
                        await clean(
                            ResetContext(file, chain.name, chain.mutates, repeat, chain=True)
                        )
                except NotRun as held_back:
                    stop(0, str(held_back), str(held_back))
                else:
                    await calls()
            outcome = chain_outcome([row["outcome"] for row in rows])
            if outcome == "incomplete" and uncaptured:
                outcome = "ungraded"  # the call passed but did not show what the next one needs
            chain_counter += 1
            result.chains += 1
            write_json(
                out / f"chain-{chain_counter:03d}.json",
                {
                    "chain_file": file,
                    "chain": chain.name,
                    "repeat": repeat,
                    "mutates": chain.mutates,
                    "outcome": outcome,
                    "uncaptured": uncaptured,
                    "reason": reason,
                    "calls": rows,
                    "variables": variables,
                },
            )

    jobs = [
        one(file, scenario, repeat)
        for repeat in range(1, repeats + 1)
        for file, scenario in scenarios
    ]
    jobs += [
        one_chain(file, chain, repeat) for repeat in range(1, repeats + 1) for file, chain in chains
    ]
    await asyncio.gather(*jobs)
    write_json(out / "batch.json", result.to_json())
    return result


async def reread_missing_traces(out: Path, reader: TraceReader, *, every: bool = False) -> int:
    """Fetch again the trace of every stored run that has none, or an empty one.

    ``every`` fetches all of them again, for runs stored before the reader
    learned to keep something (a tool's reported values, for one). A trace that
    is already stored is never replaced by a failed fetch.
    """
    fetched = 0
    for path in sorted(out.glob("run-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        record = RunRecord.from_json(payload["record"])
        old = Trace.from_json(payload["trace"]) if payload.get("trace") else None
        usable = old is not None and not looks_unfinished(old, record)
        if usable and not every:
            continue
        trace, error = await read_trace_when_ready(reader, record, attempts=2)
        if trace is None and usable:
            continue  # keep what is there
        payload["trace"] = trace.to_json() if trace else None
        payload["trace_error"] = error
        write_json(path, payload)
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
        fresh = grade(scenario, record, trace, payload.get("read_back")).to_json()
        if fresh != payload["grade"]:
            payload["grade"] = fresh
            write_json(path, payload)
            changed += 1
    return changed


def regrade_chains(out: Path, chains: list[tuple[str, Chain]]) -> int:
    """Grade stored chain calls again, then each chain as a unit. Places no calls.

    Each call is rebuilt with the values it was given when it ran. A call that
    was never placed stays "not run": regrading cannot make a call happen.
    """
    by_file = dict(chains)
    changed = 0
    for path in sorted(out.glob("run-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        link = payload.get("chain")
        chain = by_file.get(link["file"]) if link else None
        call = next((c for c in chain.calls if c.id == link["id"]), None) if chain else None
        if call is None:
            continue
        try:
            scenario = call.scenario(payload.get("variables") or {})
        except (MissingVariable, ScenarioError):
            continue  # the chain was rewritten past what this stored call can answer
        record = RunRecord.from_json(payload["record"])
        trace = Trace.from_json(payload["trace"]) if payload.get("trace") else None
        if trace is not None and looks_unfinished(trace, record):
            trace = None
        fresh = grade(scenario, record, trace, payload.get("read_back")).to_json()
        if fresh != payload["grade"]:
            payload["grade"] = fresh
            write_json(path, payload)
            changed += 1
    for path in sorted(out.glob("chain-*.json")):
        stored = json.loads(path.read_text(encoding="utf-8"))
        for row in stored["calls"]:
            if row["run"] and (out / row["run"]).exists():
                run = json.loads((out / row["run"]).read_text(encoding="utf-8"))
                row["outcome"] = run["grade"]["outcome"]
        outcome = chain_outcome([row["outcome"] for row in stored["calls"]])
        if outcome == "incomplete" and stored.get("uncaptured"):
            outcome = "ungraded"
        if outcome != stored["outcome"]:
            stored["outcome"] = outcome
        write_json(path, stored)
    return changed
