"""Checking what a scenario says must already be true on the system under test.

A scenario's (or chain's) ``requires`` is free text for people ("Mark Marshall
is on file"). Nothing in this package can tell whether it holds, so without a
checker the honest answer is "unchecked", never "ok". A deployment that can tell
supplies a checker: a command or an address of its own, as for the reset and the
read-back. Neither knows anything about any particular system.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from synthiq_caller.external import check_address

# "none": nothing is required. "unchecked": something is, and nobody looked.
STATES = ("none", "unchecked", "met", "unmet", "error")


@dataclass(frozen=True)
class PrereqResult:
    state: str
    detail: str = ""


class PrereqChecker(Protocol):
    def check(self, file: str, name: str, requires: str) -> PrereqResult: ...


@dataclass
class CommandChecker:
    """Run a command per file: exit 0 means met, 1 means unmet, anything else an error.

    The command gets ``SYNTHIQ_SCENARIO_FILE``, ``SYNTHIQ_SCENARIO_NAME`` and
    ``SYNTHIQ_REQUIRES`` in its environment. Its last line of output is kept as
    the detail. A string goes through the shell; a list is run as it stands.
    """

    command: str | Sequence[str]
    timeout_s: float = 60.0

    def check(self, file: str, name: str, requires: str) -> PrereqResult:
        env = {
            **os.environ,
            "SYNTHIQ_SCENARIO_FILE": file,
            "SYNTHIQ_SCENARIO_NAME": name,
            "SYNTHIQ_REQUIRES": requires,
        }
        try:
            done = subprocess.run(
                self.command,
                shell=isinstance(self.command, str),  # noqa: S603 - the operator's own configuration
                env=env,
                timeout=self.timeout_s,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return PrereqResult("error", f"checker ran past {self.timeout_s:.0f}s")
        except OSError as error:
            return PrereqResult("error", f"checker could not start: {error}")
        lines = (done.stdout or done.stderr or "").strip().splitlines()
        detail = lines[-1][:200] if lines else ""
        if done.returncode == 0:
            return PrereqResult("met", detail)
        if done.returncode == 1:
            return PrereqResult("unmet", detail)
        return PrereqResult("error", detail or f"checker exited {done.returncode}")


@dataclass
class HttpChecker:
    """Ask an address, with a secret header; it answers ``{"met": bool, "detail": str}``."""

    url: str
    secret: str = field(default="", repr=False)
    header: str = "X-Synthiq-Secret"
    timeout_s: float = 30.0
    transport: httpx.BaseTransport | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        check_address(self.url)

    def check(self, file: str, name: str, requires: str) -> PrereqResult:
        headers = {self.header: self.secret} if self.secret else {}
        body = {"scenario_file": file, "scenario": name, "requires": requires}
        try:
            with httpx.Client(timeout=self.timeout_s, transport=self.transport) as client:
                response = client.post(self.url, json=body, headers=headers)
        except httpx.HTTPError as error:
            return PrereqResult("error", f"checker could not be asked: {type(error).__name__}")
        if not response.is_success:
            return PrereqResult("error", f"checker answered {response.status_code}")
        try:
            answer = response.json()
        except ValueError:
            answer = None
        if not isinstance(answer, dict) or not isinstance(answer.get("met"), bool):
            return PrereqResult("error", "checker did not answer with 'met' true or false")
        return PrereqResult(
            "met" if answer["met"] else "unmet", str(answer.get("detail", ""))[:200]
        )


def requires_state(
    file: str, name: str, requires: str, checker: PrereqChecker | None
) -> PrereqResult:
    if not requires.strip():
        return PrereqResult("none")
    if checker is None:
        return PrereqResult("unchecked", requires)
    return checker.check(file, name, requires)
