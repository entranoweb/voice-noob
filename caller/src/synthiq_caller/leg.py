"""The leg: how the caller's audio reaches an agent, and what comes back.

A leg is the only thing the director talks to. It carries 16-bit mono PCM in
both directions at whatever rate each side states, and reports a small set of
events. Anything an engine cannot report is simply never emitted: ``Heard``, for
instance, exists only where the engine exposes what its recogniser made of the
caller's audio.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class AgentText:
    """The words the agent is about to speak, or has spoken."""

    text: str


@dataclass(frozen=True)
class AgentAudio:
    """A frame of the agent's audio: 16-bit mono PCM at ``rate`` hertz."""

    pcm: bytes
    rate: int


@dataclass(frozen=True)
class Heard:
    """What the agent's own recogniser reports it heard from the caller."""

    text: str


@dataclass(frozen=True)
class Interrupted:
    """The agent stopped speaking because the caller spoke over it."""


@dataclass(frozen=True)
class Closed:
    """The far end ended the call, or the transport dropped."""

    reason: str = ""


LegEvent = AgentText | AgentAudio | Heard | Interrupted | Closed


@runtime_checkable
class Leg(Protocol):
    """One call's transport. Implementations live in ``synthiq_caller.legs``."""

    @property
    def input_rate(self) -> int:
        """Sample rate, in hertz, of the PCM ``send_pcm`` expects."""
        ...

    @property
    def call_ref(self) -> str | None:
        """The engine's own identifier for this call, used to read its trace."""
        ...

    async def connect(self) -> None: ...

    async def send_pcm(self, pcm: bytes) -> None:
        """Send caller audio: 16-bit mono PCM at ``input_rate``."""
        ...

    async def send_dtmf(self, digits: str) -> None:
        """Send keypad digits. A leg that cannot raises ``NotImplementedError``."""
        ...

    def events(self) -> AsyncIterator[LegEvent]:
        """Events from the agent, ending with ``Closed``."""
        ...

    async def close(self) -> None: ...
