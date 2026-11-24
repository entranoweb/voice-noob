"""Phone leg over a carrier media stream. NOT YET IMPLEMENTED.

This file exists so the shape of the phone leg is agreed before it is written.
Nothing here places a call. Constructing the class works; every method that
would touch a carrier raises ``NotImplementedError``.

The plan:

* Dial the agent's telephone number through Twilio (later any SIP trunk) and
  attach a bidirectional media stream.
* Run the stream through a Pipecat pipeline, which already provides the Twilio
  frame serializer, mu-law 8 kHz conversion, per-side recording and keypad tones.
* Map the stream onto this package's ``Leg`` protocol: inbound agent audio
  becomes ``AgentAudio`` at 8000 Hz, ``send_pcm`` takes 16-bit PCM at 8000 Hz and
  is encoded to mu-law on the way out, ``send_dtmf`` sends real keypad tones.
* The carrier reports no agent text and no recogniser output, so this leg emits
  neither ``AgentText`` nor ``Heard``. Agent text has to come from a referee
  recogniser listening to the agent's side (planned: Deepgram), behind the same
  ``AgentText`` event, so the director does not change.
* ``call_ref`` is the carrier's call id; the trace reader for the agent's engine
  finds the matching conversation by the dialled number and start time.

Why this leg matters: the socket leg skips the telephone codec, so it cannot
reproduce mishearing that only happens on the phone path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from synthiq_caller.leg import LegEvent

_MESSAGE = "the phone leg is not built yet: see this module's docstring for the plan"


class TwilioMediaLeg:
    def __init__(self, *, to_number: str, from_number: str) -> None:
        self.to_number = to_number
        self.from_number = from_number

    @property
    def input_rate(self) -> int:
        return 8000

    @property
    def call_ref(self) -> str | None:
        return None

    async def connect(self) -> None:
        raise NotImplementedError(_MESSAGE)

    async def send_pcm(self, pcm: bytes) -> None:
        raise NotImplementedError(_MESSAGE)

    async def send_dtmf(self, digits: str) -> None:
        raise NotImplementedError(_MESSAGE)

    def events(self) -> AsyncIterator[LegEvent]:
        raise NotImplementedError(_MESSAGE)

    async def close(self) -> None:
        return None
