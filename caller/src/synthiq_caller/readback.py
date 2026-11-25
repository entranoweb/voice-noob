"""Reading back what the system under test actually holds after a call.

A tool that answers "booked" is the agent's account of what happened, not proof
that it happened. A read-back asks the system itself: after the call it fetches
the stored values, and the grader uses those as "stored".

When no read-back is configured, or it failed, or its answer does not mention a
field at all, the grader falls back to what the agent's tool was sent, and every
result says which of the two it used (``stored_source``: ``read back`` or
``tool call``).

A read-back that names a field and holds nothing under it (``null`` or an empty
string) is an answer, not a gap: the system was asked and has no value. That is
kept as an empty string and graded as "not stored". It is never replaced by what
the tool was sent.

A read-back answers with one flat JSON object, for example
``{"date_of_birth": "2002-02-20", "appointment_status": "booked"}``. The four
caller facts use their scenario names (``name``, ``date_of_birth``, ``postcode``,
``phone``); any other key can be checked with ``expect.read_back`` or captured
by a chain. Nothing here knows what system it is asking.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from synthiq_caller.external import (
    ExternalFailed,
    call_address,
    check_address,
    run_command,
    safe_id,
)

READ_BACK = "read back"
TOOL_CALL = "tool call"


class ReadBackFailed(RuntimeError):
    """The stored values could not be fetched. The grader then falls back."""


@dataclass(frozen=True)
class ReadBackContext:
    """Which call to look up: the scenario, the caller's facts and the call's reference."""

    scenario_file: str
    name: str
    repeat: int
    call_ref: str
    facts: dict[str, str] = field(default_factory=dict)

    def checked(self) -> None:
        """Refuse a call reference that is not a plain id before any command sees it.

        The reference is whatever the platform under test returned as its id.
        """
        if self.call_ref and not safe_id(self.call_ref):
            raise ReadBackFailed(
                "read-back refused: the call reference is not safe to give to a command "
                "(letters, digits and . _ - : only)"
            )

    def as_env(self) -> dict[str, str]:
        env = {
            "SYNTHIQ_SCENARIO_FILE": self.scenario_file,
            "SYNTHIQ_SCENARIO_NAME": self.name,
            "SYNTHIQ_REPEAT": str(self.repeat),
            "SYNTHIQ_CALL_REF": self.call_ref,
            "SYNTHIQ_PHASE": "read_back",
        }
        env.update({f"SYNTHIQ_FACT_{key.upper()}": value for key, value in self.facts.items()})
        return env

    def as_json(self) -> dict[str, Any]:
        return {
            "scenario_file": self.scenario_file,
            "name": self.name,
            "repeat": self.repeat,
            "call_ref": self.call_ref,
            "facts": dict(self.facts),
        }


class ReadBack(Protocol):
    async def read(self, context: ReadBackContext) -> dict[str, str]:
        """The values the system holds after this call. Raise ``ReadBackFailed`` if unknown."""
        ...


def held_nothing(read_back: dict[str, str] | None, key: str) -> bool:
    """True when a read-back was made, named ``key``, and held nothing under it."""
    return read_back is not None and key in read_back and not read_back[key].strip()


class NoReadBack:
    """Fetch nothing: "stored" is what the agent's tool was sent, and is labelled so."""

    async def read(self, context: ReadBackContext) -> dict[str, str]:
        return {}


def _values(text: str) -> dict[str, str]:
    try:
        data = json.loads(text)
    except ValueError as error:
        raise ReadBackFailed("read-back did not answer with JSON") from error
    if not isinstance(data, dict):
        raise ReadBackFailed("read-back must answer with one JSON object")
    # A key the system answered with nothing under it is kept, as "": it was
    # asked and holds no value. Only a key that is absent means "not asked".
    return {
        str(key): "" if value is None else str(value).strip()
        for key, value in data.items()
        if not isinstance(value, dict | list)
    }


@dataclass
class CommandReadBack:
    """Run a command that prints the stored values as one JSON object.

    The call is described in the environment: ``SYNTHIQ_CALL_REF``,
    ``SYNTHIQ_SCENARIO_FILE`` and one ``SYNTHIQ_FACT_<NAME>`` per caller fact.
    """

    command: str | Sequence[str]
    timeout_s: float = 60.0

    async def read(self, context: ReadBackContext) -> dict[str, str]:
        context.checked()
        try:
            printed = await run_command(self.command, context.as_env(), self.timeout_s)
        except ExternalFailed as error:
            raise ReadBackFailed(f"read-back {error}") from error
        return _values(printed)


@dataclass
class HttpReadBack:
    """Call an address with a secret header; it answers with the stored values as JSON."""

    url: str
    secret: str = field(default="", repr=False)
    header: str = "X-Synthiq-Secret"
    method: str = "POST"
    timeout_s: float = 30.0
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        check_address(self.url)

    async def read(self, context: ReadBackContext) -> dict[str, str]:
        context.checked()
        try:
            response = await call_address(
                self.url,
                context.as_json(),
                secret=self.secret,
                header=self.header,
                method=self.method,
                timeout_s=self.timeout_s,
                transport=self.transport,
            )
        except ExternalFailed as error:
            raise ReadBackFailed(f"read-back {error}") from error
        return _values(response.text)
