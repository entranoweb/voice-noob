"""Putting the system under test back to a known state before a call.

A scenario that only gives the same answer from a clean start says so with
``"clean_state": true``. The batch then runs its reset before that call. A reset
that fails means the call is **not run**: the agent was never tried, so nothing
may be counted against it.

Nothing here knows how to reset any particular system. A deployment supplies
that, as a command to run or an address to call.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from synthiq_caller.external import ExternalFailed, call_address, check_address, run_command


class ResetFailed(RuntimeError):
    """The reset did not finish cleanly; the call it was for must not be placed."""


@dataclass(frozen=True)
class ResetContext:
    """What is about to run, for a reset that needs to know."""

    scenario_file: str
    name: str
    mutates: str
    repeat: int
    # Set when the reset is for a chain of calls rather than a single one.
    chain: bool = False

    def as_env(self) -> dict[str, str]:
        return {
            "SYNTHIQ_SCENARIO_FILE": self.scenario_file,
            "SYNTHIQ_SCENARIO_NAME": self.name,
            "SYNTHIQ_MUTATES": self.mutates,
            "SYNTHIQ_REPEAT": str(self.repeat),
            "SYNTHIQ_CHAIN": "1" if self.chain else "0",
            "SYNTHIQ_PHASE": "reset",
        }

    def as_json(self) -> dict[str, str | int | bool]:
        return {
            "scenario_file": self.scenario_file,
            "name": self.name,
            "mutates": self.mutates,
            "repeat": self.repeat,
            "chain": self.chain,
        }


class Reset(Protocol):
    async def reset(self, context: ResetContext) -> None:
        """Return once the system is clean. Raise ``ResetFailed`` when it is not."""
        ...


class NoReset:
    """The operator states the system is already clean before every call."""

    async def reset(self, context: ResetContext) -> None:
        return None


@dataclass
class CommandReset:
    """Run a command; exit code 0 within the time limit means the system is clean.

    A string is run through the shell, a list is run as it stands. The scenario
    is passed in the environment (``SYNTHIQ_SCENARIO_FILE`` and the rest).
    """

    command: str | Sequence[str]
    timeout_s: float = 120.0

    async def reset(self, context: ResetContext) -> None:
        try:
            await run_command(self.command, context.as_env(), self.timeout_s)
        except ExternalFailed as error:
            raise ResetFailed(f"reset {error}") from error


@dataclass
class HttpReset:
    """Call an address with a secret header; any 2xx answer means the system is clean.

    The scenario is sent as the JSON body. The secret is never put in an error
    message. A plain ``http`` address is refused unless it is this machine, so
    the secret is not sent in the clear.
    """

    url: str
    secret: str = field(default="", repr=False)
    header: str = "X-Synthiq-Secret"
    method: str = "POST"
    timeout_s: float = 60.0
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        check_address(self.url)

    async def reset(self, context: ResetContext) -> None:
        try:
            await call_address(
                self.url,
                dict(context.as_json()),
                secret=self.secret,
                header=self.header,
                method=self.method,
                timeout_s=self.timeout_s,
                transport=self.transport,
            )
        except ExternalFailed as error:
            raise ResetFailed(f"reset {error}") from error
