"""A dry run: what a batch would do to the system under test, without doing it.

``synthiq-caller batch ... --dry-run`` prints, for every scenario and chain in
the order the batch would take them, whether it would be held back and why, and
every command or address the rig would use around its call: the reset, the
before-call hook, the read-back and the after-call hook. No call is placed, no
command is run, no address is called, no secret is read and nothing is written.

It answers one question before a batch that changes a real system: "what
exactly is this about to run?" It does not check the clip bank's labels or the
name budget; those depend on the moment the batch runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from synthiq_caller.batch import ALLOWED, Hooks, NoHooks, ShellHooks
from synthiq_caller.chain import Chain
from synthiq_caller.external import split_command
from synthiq_caller.readback import CommandReadBack, HttpReadBack, NoReadBack, ReadBack
from synthiq_caller.reset import CommandReset, HttpReset, NoReset, Reset
from synthiq_caller.scenario import Scenario

RESET_ENV = "SYNTHIQ_SCENARIO_FILE, SYNTHIQ_SCENARIO_NAME, SYNTHIQ_MUTATES, SYNTHIQ_REPEAT"
READ_BACK_ENV = "SYNTHIQ_CALL_REF, SYNTHIQ_SCENARIO_FILE and one SYNTHIQ_FACT_<NAME> per fact"
AFTER_ENV = "SYNTHIQ_CALL_REF, SYNTHIQ_RUN_FILE, SYNTHIQ_RUN_ID, SYNTHIQ_OUT_DIR, SYNTHIQ_OUTCOME"


def shown(command: str | Sequence[str]) -> str:
    """The command as the list it will be run as, one argument per item."""
    try:
        return repr(split_command(command))
    except ValueError as error:
        return f"UNREADABLE ({error}): {command!r}"


def _address(method: str, url: str, header: str, secret: bool) -> str:
    return f"{method} {url}" + (f"  (secret sent in the {header} header)" if secret else "")


def describe_reset(reset: Reset | None) -> str:
    if reset is None:
        return ""
    if isinstance(reset, NoReset):
        return "nothing is done: the operator states the system is already clean"
    if isinstance(reset, CommandReset):
        return f"{shown(reset.command)}  limit {reset.timeout_s:.0f}s  env {RESET_ENV}"
    if isinstance(reset, HttpReset):
        return _address(reset.method, reset.url, reset.header, bool(reset.secret))
    return type(reset).__name__


def describe_read_back(read_back: ReadBack | None) -> str:
    if read_back is None or isinstance(read_back, NoReadBack):
        return ""
    if isinstance(read_back, CommandReadBack):
        return f"{shown(read_back.command)}  limit {read_back.timeout_s:.0f}s  env {READ_BACK_ENV}"
    if isinstance(read_back, HttpReadBack):
        return _address(read_back.method, read_back.url, read_back.header, bool(read_back.secret))
    return type(read_back).__name__


def describe_hook(hooks: Hooks | None, phase: str) -> str:
    if hooks is None or isinstance(hooks, NoHooks):
        return ""
    if isinstance(hooks, ShellHooks):
        command = hooks.before if phase == "before" else hooks.after
        if not command:
            return ""
        env = f"  env {AFTER_ENV}" if phase == "after" else ""
        return f"{shown(command)}  limit {hooks.timeout_s:.0f}s{env}"
    return type(hooks).__name__


def _call_lines(
    reset_text: str, before: str, read_back: str, after: str, leg: str, clean: bool, label: str
) -> list[str]:
    lines = []
    if clean:
        lines.append(f"  reset:      {reset_text}")
    if before:
        lines.append(f"  before:     {before}")
    lines.append(f"  call:       {label} over {leg} (not placed in a dry run)")
    if read_back:
        lines.append(f"  read back:  {read_back}")
    else:
        lines.append("  read back:  none; 'stored' would be what the agent's tool was sent")
    if after:
        lines.append(f"  after:      {after}")
    return lines


def dry_run(
    scenarios: list[tuple[str, Scenario]],
    chains: list[tuple[str, Chain]],
    *,
    leg: str,
    allow_mutating: str,
    repeats: int = 1,
    reset: Reset | None = None,
    read_back: ReadBack | None = None,
    hooks: Hooks | None = None,
    target: dict[str, Any] | None = None,
) -> list[str]:
    """Every line a dry run prints. Runs nothing."""
    allowed = ALLOWED[allow_mutating]
    reset_text = describe_reset(reset)
    before, after = describe_hook(hooks, "before"), describe_hook(hooks, "after")
    held = describe_read_back(read_back)
    name = (target or {}).get("name") or "none given"
    lines = [
        "DRY RUN: nothing below is run, called or placed.",
        f"target profile: {name}; the batch may change: {allow_mutating}; "
        f"each scenario {repeats} time(s)",
        "",
    ]
    would_run = 0
    for file, scenario in scenarios:
        lines.append(f"{file}  mutates={scenario.mutates}  clean_state={scenario.clean_state}")
        why = ""
        if scenario.mutates not in allowed:
            why = f"mutation guard: scenario {scenario.mutates}, batch allows {allow_mutating}"
        elif scenario.missing_clips:
            why = f"missing clips: {list(scenario.missing_clips)}"
        elif scenario.missing_target:
            why = f"the target profile does not give {list(scenario.missing_target)}"
        elif scenario.clean_state and reset is None:
            why = "needs a clean state and the batch has no reset configured"
        if why:
            lines.append(f"  NOT RUN: {why}")
            continue
        would_run += 1
        lines += _call_lines(reset_text, before, held, after, leg, scenario.clean_state, "one call")
    for file, chain in chains:
        lines.append(f"{file}  chain  mutates={chain.mutates}  clean_state={chain.clean_state}")
        refused = [c.id for c in chain.calls if c.mutates not in allowed]
        unrecorded = [c.id for c in chain.calls if c.missing_clips]
        unprofiled = {c.id: list(c.missing_target) for c in chain.calls if c.missing_target}
        why = ""
        if refused:
            why = f"mutation guard: calls {refused}, batch allows {allow_mutating}"
        elif unrecorded:
            why = f"missing clips in calls {unrecorded}"
        elif unprofiled:
            why = f"the target profile does not give {unprofiled}"
        elif chain.clean_state and reset is None:
            why = "needs a clean state and the batch has no reset configured"
        if why:
            lines.append(f"  NOT RUN: {why}")
            continue
        would_run += 1
        if chain.clean_state:
            lines.append(f"  reset:      {reset_text}  (once, before the first call)")
        for call in chain.calls:
            lines += _call_lines("", before, held, after, leg, False, f"call {call.id!r}")
    lines += [
        "",
        f"{would_run} of {len(scenarios) + len(chains)} file(s) would be run. Clip labels, the "
        "name budget and prerequisites are checked when the batch itself runs.",
    ]
    return lines
