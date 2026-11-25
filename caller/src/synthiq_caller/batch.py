"""Run a directory of scenarios, several times each, without doing damage.

Three guards sit in front of every call:

* **The mutation guard.** Each scenario states what it can change on the system
  under test (``mutates``). A batch runs only the levels it was allowed; the
  default allows none, so a batch started by accident changes nothing.
* **The name budget.** Systems that look patients up by name limit how often one
  name may be checked. The limiter refuses a call that would go over (or, when
  asked to space calls, waits until it would not), rather than let the target
  lock the name and fail every later call for the wrong reason. A scenario says
  how many checks its target allows (``identity_budget``) and how many one call
  spends (``identity_attempts``).
* **The clip provenance.** A clip whose manifest row does not say where it came
  from (real or synthetic) is not played unless the batch was told to allow it,
  and then every result it touches says "provenance unknown". A clip past its
  retention date is never played.
* **The clip labels.** A clip whose label no person has confirmed is not played
  unless the batch was told to allow it, and then every result it touches says
  "labels unverified".
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
import copy
import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
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
    variables_in,
)
from synthiq_caller.clips import ClipBank
from synthiq_caller.director import Director, RunRecord
from synthiq_caller.external import ExternalFailed, run_blocking, safe_id
from synthiq_caller.grading import Grade, grade
from synthiq_caller.jsonio import next_index, write_json, write_new_json
from synthiq_caller.leg import Leg
from synthiq_caller.prereq import PrereqChecker, requires_state
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


@dataclass(frozen=True)
class CallInfo:
    """The call an ``after_call`` hook is being run for."""

    # The engine's own reference for the call (a conversation id); "" when the leg gave none.
    call_ref: str
    # The stored run: "run-003.json", already written when the hook runs.
    run_file: str
    out: str
    # "passed", "failed" or "ungraded".
    outcome: str

    @property
    def run_id(self) -> str:
        return Path(self.run_file).stem


class Hooks(Protocol):
    async def before_call(self, scenario_file: str, scenario: Scenario, repeat: int) -> None: ...

    async def after_call(
        self, scenario_file: str, scenario: Scenario, repeat: int, call: CallInfo | None = None
    ) -> None: ...


class NoHooks:
    """The default: do nothing before or after a call."""

    async def before_call(self, scenario_file: str, scenario: Scenario, repeat: int) -> None:
        return None

    async def after_call(
        self, scenario_file: str, scenario: Scenario, repeat: int, call: CallInfo | None = None
    ) -> None:
        return None


_PLACEHOLDER = re.compile(r"\{(call_ref|run_file|run_id|scenario_file|repeat)\}")
_SAFE_WORD = re.compile(r"[A-Za-z0-9._:\-]+")


@dataclass
class ShellHooks:
    """Run a command before and after each call; a non-zero exit stops that call.

    The command gets the scenario in its environment: ``SYNTHIQ_SCENARIO_FILE``,
    ``SYNTHIQ_SCENARIO_NAME``, ``SYNTHIQ_MUTATES``, ``SYNTHIQ_REPEAT`` and
    ``SYNTHIQ_PHASE`` (``before`` or ``after``).

    The ``after`` command also gets the call: ``SYNTHIQ_CALL_REF`` (the engine's
    conversation id), ``SYNTHIQ_RUN_FILE``, ``SYNTHIQ_RUN_ID``, ``SYNTHIQ_OUT_DIR``
    and ``SYNTHIQ_OUTCOME``. The same values can be written into the command as
    ``{call_ref}``, ``{run_file}``, ``{run_id}``, ``{scenario_file}`` and
    ``{repeat}``, for a program that takes them as arguments. A value is only
    put on the command line if it is made of letters, digits and ``. _ - :``; a
    command that names a value the call does not have is not run. Nor is any
    command run for a call whose reference is not such a plain id.

    The command is split into a program and its arguments and run directly,
    never through a shell (``external.py``).
    """

    # A string is split into a program and its arguments; a list is taken as it
    # stands, and a ``{call_ref}`` is filled in inside whichever argument names it.
    before: str | Sequence[str] = ""
    after: str | Sequence[str] = ""
    timeout_s: float = 120.0
    # Skip the after command for a call whose scenario states it changes nothing
    # (``mutates: none``): a step that records what a call created has nothing
    # to record. A scenario that does not say what it changes still gets it.
    after_only_when_mutating: bool = False

    async def _run(
        self,
        command: str | Sequence[str],
        phase: str,
        file: str,
        scenario: Scenario,
        n: int,
        call: CallInfo | None = None,
    ) -> None:
        if not command:
            return
        env = {
            "SYNTHIQ_SCENARIO_FILE": file,
            "SYNTHIQ_SCENARIO_NAME": scenario.name,
            "SYNTHIQ_MUTATES": scenario.mutates,
            "SYNTHIQ_REPEAT": str(n),
            "SYNTHIQ_PHASE": phase,
        }
        words = {"scenario_file": file, "repeat": str(n)}
        if call is not None:
            if call.call_ref and not safe_id(call.call_ref):
                # The reference is whatever the platform under test returned.
                raise RuntimeError(
                    f"{phase}_call hook not run: the call reference is not safe to give to "
                    "a command (letters, digits and . _ - : only)"
                )
            env.update(
                {
                    "SYNTHIQ_CALL_REF": call.call_ref,
                    "SYNTHIQ_RUN_FILE": call.run_file,
                    "SYNTHIQ_RUN_ID": call.run_id,
                    "SYNTHIQ_OUT_DIR": call.out,
                    "SYNTHIQ_OUTCOME": call.outcome,
                }
            )
            words.update(call_ref=call.call_ref, run_file=call.run_file, run_id=call.run_id)
        parts = [command] if isinstance(command, str) else [str(part) for part in command]
        for word in {w for part in parts for w in _PLACEHOLDER.findall(part)}:
            value = words.get(word, "")
            if not _SAFE_WORD.fullmatch(value):
                raise RuntimeError(
                    f"{phase}_call hook names {{{word}}}, which this call does not have"
                    if not value
                    else f"{phase}_call hook: {{{word}}} is not safe to put on a command line"
                )
            parts = [part.replace(f"{{{word}}}", value) for part in parts]
        command = parts[0] if isinstance(command, str) else parts
        try:
            done = await asyncio.to_thread(run_blocking, command, env, self.timeout_s)
        except ExternalFailed as error:
            raise RuntimeError(f"{phase}_call hook: {error}") from error
        if done.returncode != 0:
            raise RuntimeError(f"{phase}_call hook exited {done.returncode}: {done.stderr[-200:]}")

    async def before_call(self, scenario_file: str, scenario: Scenario, repeat: int) -> None:
        await self._run(self.before, "before", scenario_file, scenario, repeat)

    async def after_call(
        self, scenario_file: str, scenario: Scenario, repeat: int, call: CallInfo | None = None
    ) -> None:
        if self.after_only_when_mutating and scenario.mutates == "none":
            return
        await self._run(self.after, "after", scenario_file, scenario, repeat, call)


@dataclass
class NameBudget:
    """At most ``limit`` identity checks per caller name in any ``window_s`` seconds.

    The default of two is the tightest lock known on a system under test; a
    scenario states its own target's allowance with ``identity_budget``. Given a
    ``path`` the spending is kept in that file, on the wall clock, so that one
    batch run straight after another does not spend the same allowance twice.
    """

    limit: int = 2
    window_s: float = 900.0
    clock: Callable[[], float] = time.monotonic
    path: Path | None = None
    _used: dict[str, list[float]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.path is None:
            return
        if self.clock is time.monotonic:
            self.clock = time.time  # a file outlives the process; so must its clock
        if self.path.exists():
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            self._used = {str(k): [float(t) for t in v] for k, v in stored.items()}

    @staticmethod
    def key(name: str) -> str:
        return " ".join(name.lower().split())

    def _recent(self, name: str) -> list[float]:
        now = self.clock()
        recent = [t for t in self._used.get(self.key(name), []) if now - t < self.window_s]
        self._used[self.key(name)] = recent
        return recent

    def remaining(self, name: str, limit: int | None = None) -> int:
        return max(0, (limit or self.limit) - len(self._recent(name)))

    def wait_s(self, name: str, cost: int = 1, limit: int | None = None) -> float | None:
        """Seconds until ``cost`` checks could be spent; None when they never could."""
        allowed = limit or self.limit
        if not name or self.limit <= 0:
            return 0.0
        if cost > allowed:
            return None
        recent = sorted(self._recent(name))
        over = len(recent) + cost - allowed
        if over <= 0:
            return 0.0
        return max(0.0, recent[over - 1] + self.window_s - self.clock())

    def take(self, name: str, cost: int = 1, limit: int | None = None) -> bool:
        """Spend ``cost`` checks for ``name``. False, and nothing spent, when too few are left."""
        if not name or self.limit <= 0:
            return True  # no name to protect, or the limiter is switched off
        if self.remaining(name, limit) < cost:
            return False
        now = self.clock()
        self._used[self.key(name)].extend([now] * cost)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_json(self.path, self._used)
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
    # The calls went to a scripted stand-in, not an agent (``--leg fake``).
    rehearsal: bool = False

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
# The same, for a leg that depends on which scenario is being run (a scripted
# stand-in has one script per scenario).
LegFactoryFor = Callable[[str], tuple[Leg, TraceReader | None]]


def connected(record: RunRecord) -> bool:
    """False for a call that was never answered or that the rig itself broke."""
    return not record.ended.startswith(("not answered", "rig error"))


def looks_unfinished(trace: Trace, record: RunRecord) -> bool:
    """A trace with nothing in it for a call in which the agent plainly spoke.

    Engines publish a call's record a moment after it ends. Read too soon, it
    comes back empty, and an empty trace graded as it stands would fail a call
    for something the test rig did, not the agent.

    A reader that waited for the engine to mark the call finished says so
    (``Trace.final``). Its trace is taken as it stands: an engine with no flow
    steps (Vapi) would otherwise never be gradeable on a call in which no tool
    ran, and "the tool must not be called" could never pass there.
    """
    if trace.final:
        # The engine itself said the call's record is complete. Empty then means
        # no tool ran, which is a fact about the call and can be graded.
        return False
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


def load_scenarios(
    directory: str | Path, target: dict[str, Any] | None = None
) -> list[tuple[str, Scenario]]:
    """Every single-call scenario file in ``directory``, in name order.

    ``target`` is the profile of the system under test (``target.py``); a
    scenario that leaves parts of its appointment to the profile is held back by
    the batch when they are not given.
    """
    return [
        (p.name, load_scenario(p, target))
        for p in _scenario_files(directory)
        if not is_chain_file(p)
    ]


def load_chains(
    directory: str | Path, target: dict[str, Any] | None = None
) -> list[tuple[str, Chain]]:
    """Every chain file in ``directory``, in name order.

    ``target`` is the profile of the system under test, as for ``load_scenarios``;
    a chain with a call that leaves parts of its appointment to the profile is
    held back whole when they are not given.
    """
    return [(p.name, load_chain(p, target)) for p in _scenario_files(directory) if is_chain_file(p)]


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
    allow_unverified_clips: bool = False,
    allow_unknown_provenance: bool = False,
    allow_missing_consent: bool = False,
    space_names: bool = False,
    prereq: PrereqChecker | None = None,
    make_leg_for: LegFactoryFor | None = None,
    rehearsal: bool = False,
    leg_name: str = "",
    batch_id: str = "",
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
    # The bank enforces this at playback too, so no path to a clip can skip the check. The
    # caller's bank is not changed: this run plays from a copy.
    clips = copy.copy(clips)
    clips.allow_unknown_provenance = allow_unknown_provenance
    clips.allow_missing_consent = allow_missing_consent
    result = BatchResult(out=str(out), rehearsal=rehearsal)
    gate = asyncio.Semaphore(max(1, max_parallel))
    # The highest number in use, not a count: after a gap a count would name a
    # file that exists, and a stored run must never be written over.
    counter = next_index(out, "run")
    chain_counter = next_index(out, "chain")
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
        played = [clip for step in scenario.steps for clip in step.clips]
        if scenario.fallback is not None:
            played += list(scenario.fallback.clips)
        ruled_out = clips.excluded_in(played)
        if ruled_out:
            # Found here, before the call: at play time it would break the call
            # half-way and leave a failure that is nobody's but the rig's.
            raise NotRun(f"excluded clips: {ruled_out}")
        late = clips.expired_in(played)
        if late:
            raise NotRun(f"clips past their retention date: {late[:3]}; remove or re-consent them")
        unknown = clips.provenance_unknown(played)
        if unknown and not allow_unknown_provenance:
            raise NotRun(
                f"clip provenance not recorded ({len(unknown)}): {unknown[:3]}; "
                "record it in the manifest, or allow unknown provenance for this batch"
            )
        no_consent = clips.consent_missing(played)
        if no_consent and not allow_missing_consent:
            raise NotRun(
                f"real clips without a lawful basis or consent record ({len(no_consent)}): "
                f"{no_consent[:3]}; record them in the manifest, or allow missing consent "
                "for this batch"
            )
        doubts = clips.label_doubts(played)
        if doubts and not allow_unverified_clips:
            raise NotRun(
                f"clip labels not confirmed by a person ({len(doubts)}): {doubts[:3]}; "
                "check them, or allow unverified clips for this batch"
            )
        name = scenario.facts.get("name", "")
        allowance = scenario.identity_budget or None
        cost = scenario.identity_attempts
        if not budget.take(name, cost, allowance):
            delay = budget.wait_s(name, cost, allowance)
            if space_names and delay is not None:
                await wait(delay + 1.0)
            if not (space_names and delay is not None and budget.take(name, cost, allowance)):
                raise NotRun(
                    f"name budget: {name!r} is allowed {allowance or budget.limit} identity "
                    f"check(s) per {budget.window_s:.0f}s and this call needs {cost}"
                )
        try:
            await hooks.before_call(file, scenario, repeat)
        except Exception as error:  # noqa: BLE001 - a failed hook must stop this call only
            raise NotRun(f"before hook: {error}") from error
        reader: TraceReader | None = None
        leg: Leg | None = None
        try:
            leg, reader = make_leg_for(file) if make_leg_for is not None else make_leg()
            director = Director(scenario, leg, clips, turn_settings=turn_settings)
            record = await director.run()
        except Exception as error:  # noqa: BLE001 - the rig breaking is a result to record
            # Nothing here is the agent's doing. Kept as a void call so that it
            # is seen and re-run, never silently missing from the count.
            record = RunRecord(scenario=scenario.name, ended=f"rig error: {error!r}")
        trace: Trace | None = None
        trace_error = ""
        if reader is not None and record.call_ref and read_trace and connected(record):
            trace, trace_error = await read_trace_when_ready(
                reader, record, settle_s=settle_s, wait=wait
            )
        # A leg may say more about the line it used (a phone leg: numbers, times, audio files).
        describe = getattr(leg, "details", None)
        line: dict[str, Any] = dict(describe()) if callable(describe) else {}
        kind = str(line.pop("leg", ""))
        # Over a phone line the leg's reference is the carrier's, not the engine's:
        # the engine's is the one the trace was found under, and there may be none.
        engine_ref = record.call_ref or ""
        if "carrier_call_ref" in line:
            engine_ref = trace.call_ref if trace is not None else ""
        held: dict[str, str] | None = None
        read_back_error = ""
        if read_back is not None:
            try:
                # The engine's reference, as the after-call hook gets: over a
                # phone line the carrier's id would match nothing the system holds.
                held = await read_back.read(
                    ReadBackContext(file, scenario.name, repeat, engine_ref, dict(scenario.facts))
                )
            except Exception as error:  # noqa: BLE001 - recorded; the grader falls back
                read_back_error = str(error) or repr(error)
        graded = grade(scenario, record, trace, held)
        payload: dict[str, Any] = {
            "scenario_file": file,
            "leg": leg_name or kind,
            **line,
            "mutates": scenario.mutates,
            "repeat": repeat,
            "record": record.to_json(),
            "trace": trace.to_json() if trace else None,
            "trace_error": trace_error,
            # None: no read-back was made. An object: what the system held afterwards.
            "read_back": held,
            "read_back_error": read_back_error,
            "hook_error": "",
            "labels_unverified": doubts,
            # Clips played whose manifest does not say where they came from.
            "provenance_unknown": unknown,
            # Real clips (special-category data) played with no lawful basis or consent record.
            "consent_missing": no_consent,
            # Clips played in this call that are a synthetic voice, not a recorded caller.
            "synthetic_clips": sorted({c for said in record.said for c in said.synthetic_clips}),
            "grade": graded.to_json(),
            **extra,
        }
        if rehearsal:
            # Written into the run itself, not only the batch: a run file copied
            # or uploaded on its own must still say what it is.
            payload["rehearsal"] = True
        if batch_id:
            # Which entry of the directory's manifest this call was made under
            # (``manifest.py``): the rig, scenarios, clips and target behind it.
            payload["batch"] = batch_id
        async with lock:
            counter += 1
            index = counter
            result.ran += 1
        name = f"run-{index:03d}.json"
        write_new_json(out / name, payload)
        # The run is stored first, so the hook can be told which call this was
        # (and read the file); a hook that fails is then written into it.
        try:
            await hooks.after_call(
                file,
                scenario,
                repeat,
                CallInfo(engine_ref, name, str(out), graded.outcome),
            )
        except Exception as error:  # noqa: BLE001 - recorded, never hidden
            payload["hook_error"] = f"after hook: {error}"
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
        if scenario.missing_target:
            # Run without them, the booking would be judged on less than the
            # scenario means, or on another system's times.
            block(
                f"the target profile does not give {list(scenario.missing_target)} for this "
                "scenario's appointment (--target)"
            )
            return
        if prereq is not None and scenario.requires.strip():
            # What the scenario needs on the system under test is not there: a
            # call placed now would fail for the fixture's reason, not the agent's.
            found = await asyncio.to_thread(
                requires_state, file, scenario.name, scenario.requires, prereq
            )
            if found.state == "unmet":
                block(f"prerequisite unmet: {found.detail or scenario.requires}")
                return
        every_clip = [clip for step in scenario.steps for clip in step.clips]
        if scenario.fallback is not None:
            every_clip += list(scenario.fallback.clips)
        excluded = clips.excluded_in(every_clip)
        if excluded:
            block(f"excluded clips: {excluded}")
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
        unprofiled = {c.id: list(c.missing_target) for c in chain.calls if c.missing_target}
        # Clips named outright in any call, fallback included. One chosen by a
        # captured value is only known once that call is reached, and is checked then.
        ruled_out = clips.excluded_in(
            [
                str(clip)
                for c in chain.calls
                for raw in [*c.raw.get("steps", ()), c.raw.get("fallback") or {}]
                for clip in raw.get("clips", ())
                if not variables_in(clip)
            ]
        )
        async with gate:
            unmet = ""
            if prereq is not None and chain.requires.strip():
                found = await asyncio.to_thread(
                    requires_state, file, chain.name, chain.requires, prereq
                )
                if found.state == "unmet":
                    unmet = f"prerequisite unmet: {found.detail or chain.requires}"
            if unmet:
                stop(0, unmet, unmet)
            elif refused:
                why = (
                    f"mutation guard: chain {chain.mutates} (calls {refused}), "
                    f"batch allows {allow_mutating}"
                )
                stop(0, why, why)
            elif unrecorded:
                why = f"missing clips in calls {unrecorded}"
                stop(0, why, why)
            elif unprofiled:
                # As for a single call: run without them, a booking would be
                # judged on less than the chain means, or on another system's times.
                why = (
                    f"the target profile does not give {unprofiled} for this chain's "
                    "appointments (--target)"
                )
                stop(0, why, why)
            elif ruled_out:
                why = f"excluded clips: {ruled_out}"
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
            write_new_json(
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
                    **({"batch": batch_id} if batch_id else {}),
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
    # Whatever happens to one call, the batch's own record is written.
    finished = await asyncio.gather(*jobs, return_exceptions=True)
    write_json(out / "batch.json", result.to_json())
    for outcome in finished:
        if isinstance(outcome, BaseException):
            raise outcome
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
    skipped = 0
    for path in sorted(out.glob("run-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        scenario = by_file.get(payload.get("scenario_file", ""))
        if scenario is None:
            continue
        if _was_redacted(payload):
            skipped += 1
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
    _say_skipped(skipped)
    return changed


def _was_redacted(payload: dict[str, Any]) -> bool:
    """Whether ``forget`` redacted this stored result: grading what is left would only fail it."""
    marks = payload.get("clips_removed")
    return isinstance(marks, list) and any(isinstance(m, dict) and m.get("redacted") for m in marks)


def _say_skipped(count: int) -> None:
    if count:
        print(f"{count} redacted results not regraded (their stored grade is kept)")


def regrade_chains(out: Path, chains: list[tuple[str, Chain]]) -> int:
    """Grade stored chain calls again, then each chain as a unit. Places no calls.

    Each call is rebuilt with the values it was given when it ran. A call that
    was never placed stays "not run": regrading cannot make a call happen.
    """
    by_file = dict(chains)
    changed = 0
    skipped = 0
    for path in sorted(out.glob("run-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        link = payload.get("chain")
        chain = by_file.get(link["file"]) if link else None
        call = next((c for c in chain.calls if c.id == link["id"]), None) if chain else None
        if call is None:
            continue
        if _was_redacted(payload):
            skipped += 1
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
    _say_skipped(skipped)
    return changed
