"""Runs one scenario on one leg and writes down exactly what happened.

Three things run at once: a reader that takes events from the leg, a pump that
sends caller audio in real time (the queued clip when there is one, silence
otherwise, because a line that goes dead is not what a phone does), and the
director's own loop, which decides when to speak and what to say.

The director does not judge the call. It records; grading happens afterwards
from the agent's tool results.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from dataclasses import field as dc_field
from typing import Any

from synthiq_caller.clips import ClipBank
from synthiq_caller.leg import AgentAudio, AgentText, Closed, Heard, Interrupted, Leg
from synthiq_caller.scenario import Scenario, Step
from synthiq_caller.turns import AgentTurnTracker, TurnSettings


@dataclass
class Said:
    """One thing the caller did in answer to the agent."""

    t: float
    step_index: int
    field: str
    intended: str
    clips: list[str]
    dtmf: str
    seconds: float
    talk_over: bool
    answering: str
    # What the agent's recogniser reported between this answer and the next.
    heard: list[str] = dc_field(default_factory=list)


@dataclass
class RunRecord:
    scenario: str
    call_ref: str | None = None
    ended: str = ""
    duration_s: float = 0.0
    timeline: list[dict[str, Any]] = dc_field(default_factory=list)
    said: list[Said] = dc_field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(data: dict[str, Any]) -> RunRecord:
        said = [Said(**s) for s in data.get("said", [])]
        return RunRecord(
            scenario=data["scenario"],
            call_ref=data.get("call_ref"),
            ended=data.get("ended", ""),
            duration_s=float(data.get("duration_s", 0.0)),
            timeline=list(data.get("timeline", [])),
            said=said,
        )


class Director:
    def __init__(
        self,
        scenario: Scenario,
        leg: Leg,
        clips: ClipBank,
        *,
        turn_settings: TurnSettings | None = None,
        chunk_ms: int = 100,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.scenario = scenario
        self.leg = leg
        self.clips = clips
        self.tracker = AgentTurnTracker(turn_settings)
        self.chunk_ms = chunk_ms
        self._clock = clock
        self._t0 = 0.0
        self._queue = b""
        self._done = False
        self._pending: list[tuple[float, str]] = []  # agent lines not yet answered
        self._uses: dict[int, int] = {}
        self.record = RunRecord(scenario=scenario.name)

    def _now(self) -> float:
        return self._clock() - self._t0

    def _note(self, kind: str, **details: Any) -> None:
        self.record.timeline.append({"t": round(self._now(), 2), "kind": kind, **details})

    async def run(self) -> RunRecord:
        await self.leg.connect()
        self._t0 = self._clock()
        tasks = [
            asyncio.create_task(self._read()),
            asyncio.create_task(self._pump()),
            asyncio.create_task(self._direct()),
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        self._done = True
        for task in done:
            error = task.exception()
            if error is not None:
                self._note("error", text=repr(error))
                self.record.ended = self.record.ended or f"error: {error!r}"
        for task in tasks:
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self.leg.close()
        self.record.call_ref = self.leg.call_ref
        self.record.duration_s = round(self._now(), 2)
        return self.record

    async def _read(self) -> None:
        async for event in self.leg.events():
            now = self._now()
            if isinstance(event, AgentAudio):
                self.tracker.feed(event.pcm, event.rate, now)
            elif isinstance(event, AgentText):
                self.tracker.mark(now)
                self._pending.append((now, event.text))
                self._note("agent", text=event.text)
            elif isinstance(event, Heard):
                self._note("heard", text=event.text)
                if self.record.said and event.text.strip(". "):
                    self.record.said[-1].heard.append(event.text)
            elif isinstance(event, Interrupted):
                self.tracker.interrupted(now)
                self._note("interrupted")
            elif isinstance(event, Closed):
                self.record.ended = (
                    self.record.ended or f"agent ended the call {event.reason}".strip()
                )
                self._note("closed", reason=event.reason)
                return
        self.record.ended = self.record.ended or "agent ended the call"

    async def _pump(self) -> None:
        rate = self.leg.input_rate
        size = int(rate * self.chunk_ms / 1000) * 2
        period = self.chunk_ms / 1000
        next_at = self._clock()
        while not self._done:
            if self._queue:
                chunk, self._queue = self._queue[:size], self._queue[size:]
                chunk = chunk.ljust(size, b"\0")
            else:
                chunk = b"\0" * size
            await self.leg.send_pcm(chunk)
            next_at += period
            await asyncio.sleep(max(0.0, next_at - self._clock()))

    def _match(self, text: str) -> tuple[int, Step] | None:
        for index, step in enumerate(self.scenario.steps):
            if step.max_uses and self._uses.get(index, 0) >= step.max_uses:
                continue
            if step.when.search(text):
                return index, step
        return None

    async def _direct(self) -> None:
        scenario = self.scenario
        while self._now() < scenario.max_seconds:
            await asyncio.sleep(0.02)
            if self._queue or not self._pending:
                continue
            now = self._now()
            text = " ".join(line for _, line in self._pending)
            first_at = self._pending[0][0]

            if scenario.hangup_after is not None and scenario.hangup_after.search(text):
                self.record.ended = "caller hung up: scenario end"
                self._note("hangup", on=text[-100:])
                return

            match = self._match(text)
            if match is not None and match[1].talk_over:
                if now < first_at + match[1].delay_s:
                    continue
            else:
                if not self.tracker.turn_finished(now):
                    continue
                if match is not None and match[1].delay_s:
                    await asyncio.sleep(match[1].delay_s)

            self._pending.clear()
            if match is None:
                # A line that asks nothing ("I'm sorry to hear that."). Nothing to say.
                self._note("no_answer_needed", agent=text[-100:])
                continue
            index, step = match
            self._uses[index] = self._uses.get(index, 0) + 1
            await self._act(index, step, text)
        self.record.ended = self.record.ended or "caller hung up: time limit"
        self._note("hangup", on="time limit")

    async def _act(self, index: int, step: Step, answering: str) -> None:
        seconds = 0.0
        if step.stay_silent:
            self._note("silent", step=index)
        elif step.dtmf:
            await self.leg.send_dtmf(step.dtmf)
            self._note("dtmf", step=index, digits=step.dtmf)
        else:
            pcm = self.clips.joined(list(step.clips), self.leg.input_rate, step.gap_s)
            seconds = len(pcm) / 2 / self.leg.input_rate
            self._queue = pcm
            self._note("said", step=index, clips=list(step.clips), seconds=round(seconds, 2))
        self.record.said.append(
            Said(
                t=round(self._now(), 2),
                step_index=index,
                field=step.field,
                intended=step.intended,
                clips=list(step.clips),
                dtmf=step.dtmf,
                seconds=round(seconds, 2),
                talk_over=step.talk_over,
                answering=answering[-140:],
            )
        )
