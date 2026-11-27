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
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from dataclasses import field as dc_field
from typing import Any

from synthiq_caller.clips import ClipBank
from synthiq_caller.leg import AgentAudio, AgentText, Closed, Heard, Interrupted, Leg
from synthiq_caller.scenario import Scenario, Step
from synthiq_caller.turns import AgentTurnTracker, TurnSettings

# A line that asks for something without a question mark. The agent's re-asks
# are often imperatives: "Please could you tell me your date of birth."
_REQUEST = re.compile(
    r"\b(?:tell me|give me|let me (?:have|take)|"
    r"(?:could|can|may) i (?:please )?(?:take|have|get|ask)|"
    r"i(?:'ll| will)? need (?:your|the|a)\b|"
    r"(?:could|can|would) you (?:please )?(?:tell|give|confirm|say|spell|provide|repeat)|"
    r"please (?:could|can) you|"
    r"please (?:tell|give|confirm|say|spell|provide|repeat|state|enter)|"
    r"what(?:'s| is| was) your|confirm your|spell (?:that|it|your))\b",
    re.IGNORECASE,
)
# Words that, just before a fact's name, mean the agent already has it:
# "I have your date of birth", "thanks for your postcode".
_ACKNOWLEDGED = re.compile(
    r"(?:(?<!can )(?<!could )(?<!may )(?<!do )(?<!will )(?<!would )(?<!should )\bi have|"
    r"\bi've(?: got)?|\bi have got|\bgot|\bthanks for|\bthank you for|\byou said|"
    r"\byou gave(?: me)?|\bi can see|\bnoted)"
    r"\s+(?:your |the |that |a )?(?:[a-z']+ ){0,2}$",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"[^.?!]+[.?!]*")
# A line is finished when it ends in sentence punctuation, closing quotes or
# brackets aside. A read-back that stops after "SW1A," is the agent breathing.
_SENTENCE_END = re.compile(r"[.?!][\"')\]]*\s*$")


def ends_mid_sentence(text: str) -> bool:
    """Whether the agent's words stop short of the end of a sentence."""
    return not _SENTENCE_END.search(text.rstrip())


def asks_something(text: str) -> bool:
    """Whether a line from the agent wants an answer, question mark or not."""
    return "?" in text or bool(_REQUEST.search(text))


def asked_at(text: str, at: int) -> bool:
    """Whether the words at position ``at`` are being asked for, not merely mentioned.

    "I have your date of birth. Could I take your postcode?" mentions the date
    of birth and asks for the postcode. The sentence around ``at`` must ask
    (end in a question mark, or read as a request), and the words leading up to
    ``at`` must not say the agent already has it.
    """
    for sentence in _SENTENCE.finditer(text):
        if sentence.start() <= at < sentence.end():
            if _ACKNOWLEDGED.search(text[sentence.start() : at]):
                return False
            return asks_something(sentence.group())
    return False


def _line_went_down(error: BaseException) -> bool:
    """Whether an error is the connection failing, not the rig's own code."""
    if isinstance(error, FileNotFoundError):
        return False  # a clip that is not there
    return isinstance(error, ConnectionError | EOFError | OSError) or (
        "ConnectionClosed" in type(error).__name__
    )


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
    # A clip in this answer has no label a person has confirmed.
    labels_unverified: bool = False
    # The scenario had no step for the question; this is its fallback answer.
    fallback: bool = False
    # Clips in this answer that are a synthetic voice, not a recorded caller.
    synthetic_clips: list[str] = dc_field(default_factory=list)
    # False when the agent talked over this answer and the caller stopped before
    # it was all said: the agent did not hear all of it. Older runs lack the field.
    played: bool = True


@dataclass
class RunRecord:
    scenario: str
    call_ref: str | None = None
    ended: str = ""
    duration_s: float = 0.0
    timeline: list[dict[str, Any]] = dc_field(default_factory=list)
    said: list[Said] = dc_field(default_factory=list)
    # Questions from the agent the caller had nothing to say to. A call that
    # goes wrong after one of these went wrong because of the test rig.
    unanswered: list[str] = dc_field(default_factory=list)
    # Times the agent started speaking over the caller's answer. An agent fault,
    # counted here so it shows in the run file and not only in the timeline.
    agent_barge_ins: int = 0
    # The turn-taking settings this call was made with (adaptive floor and its
    # bounds), so runs made under different rules can be told apart.
    turn_settings: dict[str, Any] = dc_field(default_factory=dict)

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
            unanswered=list(data.get("unanswered", [])),
            agent_barge_ins=int(data.get("agent_barge_ins", 0)),
            turn_settings=dict(data.get("turn_settings", {})),
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
        max_yields_per_step: int = 3,
        hangup_wait_s: float = 10.0,
    ) -> None:
        self.scenario = scenario
        self.leg = leg
        self.clips = clips
        settings = turn_settings or TurnSettings()
        if getattr(leg, "text_follows_audio", False) and not settings.text_follows_audio:
            # A leg that learns the agent's words from a transcriber hears them
            # after they were spoken, not before.
            settings = replace(settings, text_follows_audio=True)
        self.tracker = AgentTurnTracker(settings)
        # After this many yields to the agent for one step, play it anyway: an
        # agent that always talks over the caller must not mute the caller.
        self._max_yields = max_yields_per_step
        # How long to wait for the agent to finish saying the end phrase.
        self._hangup_wait_s = hangup_wait_s
        self._hangup_seen_at: float | None = None
        self._yields: dict[int, int] = {}
        self._queued_said: list[Said] = []
        self._barge_counted = False
        self.chunk_ms = chunk_ms
        self._clock = clock
        self._t0 = 0.0
        self._queue = b""
        # Steps whose audio is in ``_queue`` and not yet sent, and whether all
        # of them are meant to be spoken over the agent.
        self._queued_steps: list[int] = []
        self._queue_talk_over = False
        self._done = False
        self._pending: list[tuple[float, str]] = []  # agent lines not yet answered
        self._uses: dict[int, int] = {}
        self.record = RunRecord(scenario=scenario.name, turn_settings=asdict(settings))

    def _now(self) -> float:
        return self._clock() - self._t0

    def _note(self, kind: str, **details: Any) -> None:
        self.record.timeline.append({"t": round(self._now(), 2), "kind": kind, **details})

    async def run(self) -> RunRecord:
        self._t0 = self._clock()
        try:
            await self.leg.connect()
        except Exception as refused:  # noqa: BLE001 - an unanswered call is a result, not a crash
            # Refused, engaged, never picked up, over a concurrency limit: the
            # caller dialled and nobody answered. Recorded as a call that failed.
            self._note("not_answered", text=repr(refused))
            self.record.ended = f"not answered: {refused!r}"
            with contextlib.suppress(Exception):
                await self.leg.close()
            self.record.call_ref = self.leg.call_ref
            return self.record
        self._t0 = self._clock()
        directing = asyncio.create_task(self._direct())
        tasks = [
            asyncio.create_task(self._read()),
            asyncio.create_task(self._pump()),
            directing,
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        self._done = True
        for task in done:
            error = task.exception()
            if error is not None:
                self._note("error", text=repr(error))
                # Two different things, and only one is the rig's. A connection
                # that fails under the reader or the pump is the line going
                # down: a call that died. Anything else here, and anything at
                # all in the director's own loop (a clip that is not there), is
                # the rig breaking, and the call is void.
                dropped = task is not directing and _line_went_down(error)
                kind = "line dropped" if dropped else "rig error"
                self.record.ended = self.record.ended or f"{kind}: {error!r}"
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
                began = self.tracker.feed(event.pcm, event.rate, now)
                if began is not None:
                    # Where response time is read from (``timing.py``).
                    self.record.timeline.append({"t": round(began, 2), "kind": "agent_speech"})
                await self._yield_if_talked_over(now)
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

    async def _yield_if_talked_over(self, now: float) -> None:
        """Stop talking when the agent starts speaking over the caller.

        A person who is talked over stops. Without this the caller carried on to
        the end of a long clip, over the top of the agent's reply. A step marked
        ``talk_over`` is the scenario testing exactly that, and is left alone.
        """
        if not self._queue or self._queue_talk_over or not self.tracker.speaking(now):
            return
        if not self._barge_counted:
            self._barge_counted = True
            self.record.agent_barge_ins += 1
        if any(self._yields.get(i, 0) >= self._max_yields for i in self._queued_steps):
            return  # yielded often enough for this step: say it
        left = len(self._queue) / 2 / self.leg.input_rate
        self._queue = b""
        for index in self._queued_steps:
            # The answer was cut off; if the agent asks again it may be given again.
            self._uses[index] = max(0, self._uses.get(index, 0) - 1)
            self._yields[index] = self._yields.get(index, 0) + 1
        for said in self._queued_said:
            said.played = False
        self._queued_steps = []
        self._queued_said = []
        clear = getattr(self.leg, "clear_playback", None)
        if clear is not None:
            await clear()
        self._note("yielded", unsent_s=round(left, 2))

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
                self._queued_steps = []
                self._queued_said = []
            await self.leg.send_pcm(chunk)
            next_at += period
            await asyncio.sleep(max(0.0, next_at - self._clock()))

    def _match(self, text: str) -> tuple[int, Step] | None:
        """The step that answers this turn: one whose words are asked for, if any.

        Steps are tried in file order. A step whose pattern only matches words
        the agent mentioned in passing ("I have your date of birth. What is
        your postcode?") gives way to a later step that matches what was asked.
        When nothing reads as asked, the first match stands, as it always did.
        """
        first: tuple[int, Step] | None = None
        for index, step in enumerate(self.scenario.steps):
            if step.max_uses and self._uses.get(index, 0) >= step.max_uses:
                continue
            hit = step.when.search(text)
            if not hit:
                continue
            if asked_at(text, hit.start()):
                return index, step
            first = first or (index, step)
        return first

    def _asked_together(self, text: str, first: tuple[int, Step]) -> list[tuple[int, Step]]:
        """Every answer one agent turn asks for, in the order it asked.

        "Could I take your date of birth and your postcode?" is two questions.
        When the step that matched carries a persona fact, every other unused
        step that carries a different fact and also matches the turn is answered
        with it, in the order the turn names them. Steps that carry no fact (a
        "yes", an opener) are never added: a read-back that mentions the date of
        birth is a request to confirm it, not to say it again. Nor is a fact the
        turn only acknowledges: "I have your date of birth. Could I take your
        postcode and a mobile number?" asks for two things, not three.
        """
        index, step = first
        if not step.field or step.talk_over:
            return [first]
        found = [(step.when.search(text), index, step)]
        fields = {step.field}
        for other_index, other in enumerate(self.scenario.steps):
            if other_index == index or not other.field or other.field in fields:
                continue
            if other.talk_over or other.stay_silent:
                continue
            if other.max_uses and self._uses.get(other_index, 0) >= other.max_uses:
                continue
            hit = other.when.search(text)
            if hit and asked_at(text, hit.start()):
                found.append((hit, other_index, other))
                fields.add(other.field)
        found.sort(key=lambda item: item[0].start() if item[0] else 0)
        return [(i, s) for _, i, s in found]

    async def _direct(self) -> None:
        scenario = self.scenario
        while self._now() < scenario.max_seconds:
            await asyncio.sleep(0.02)
            if self._queue or not self._pending:
                continue
            now = self._now()
            consumed = len(self._pending)
            text = " ".join(line for _, line in self._pending)
            first_at = self._pending[0][0]
            # The words of a read-back can stop mid-sentence while the agent
            # breathes. Wait one more hangover for the rest; the tracker's
            # ``max_turn_s`` still bounds the wait, so the rig cannot hang.
            hold = self.tracker.settings.hangover_s if ends_mid_sentence(text) else 0.0

            # The end phrase arrives as text before the agent has said it: hang
            # up once the turn has been spoken, not when the words are read.
            if scenario.hangup_after is not None and scenario.hangup_after.search(text):
                if self._hangup_seen_at is None:
                    self._hangup_seen_at = now
                if not self.tracker.turn_finished(now):
                    waited = now - self._hangup_seen_at
                    if waited < self._hangup_wait_s:
                        continue
                    # The agent never finished saying it. Hang up anyway, and
                    # say so: this is not the clean ending.
                    self.record.ended = (
                        f"caller hung up: scenario end, agent still speaking after {waited:.0f}s"
                    )
                    self._note("hangup", on=text[-100:], agent_still_speaking=True)
                    return
                self.record.ended = "caller hung up: scenario end"
                self._note("hangup", on=text[-100:])
                return

            match = self._match(text)
            if match is not None and match[1].talk_over:
                if now < first_at + match[1].delay_s:
                    continue
                self.tracker.answered()
            else:
                if not self.tracker.turn_finished(now, hold):
                    continue
                # The answer starts now: speech from here on is the agent's next
                # line, including any heard during the delay below.
                self.tracker.answered()
                if match is not None and match[1].delay_s:
                    await asyncio.sleep(match[1].delay_s)

            # Only the lines this turn read: any that arrived during the delay
            # above are the agent's next words, not ones already answered.
            del self._pending[:consumed]
            if match is None:
                if not asks_something(text):
                    # A line that asks nothing ("I'm sorry to hear that."). Nothing to say.
                    self._note("no_answer_needed", agent=text[-100:])
                    continue
                fallback = scenario.fallback
                used = self._uses.get(-1, 0)
                if fallback is not None and (not fallback.max_uses or used < fallback.max_uses):
                    self._uses[-1] = used + 1
                    await self._act(-1, fallback, text, fallback=True)
                    continue
                # A question the scenario has no answer for (reworded, or asked
                # once too often). A person would answer; the caller cannot.
                self.record.unanswered.append(text[-200:])
                self._note("unanswered", agent=text[-200:])
                continue
            for index, step in self._asked_together(text, match):
                self._uses[index] = self._uses.get(index, 0) + 1
                await self._act(index, step, text)
        self.record.ended = self.record.ended or "caller hung up: time limit"
        self._note("hangup", on="time limit")

    async def _act(self, index: int, step: Step, answering: str, *, fallback: bool = False) -> None:
        seconds = 0.0
        queued = False
        unverified = any(not self.clips.verified(clip) for clip in step.clips)
        synthetic = [clip for clip in step.clips if self.clips.synthetic(clip)]
        if step.stay_silent:
            self._note("silent", step=index)
        elif step.dtmf:
            await self.leg.send_dtmf(step.dtmf)
            self._note("dtmf", step=index, digits=step.dtmf)
        else:
            # Resampling a clip may shell out to ffmpeg; off the loop, so the
            # pump keeps sending and the reader keeps reading while it runs.
            pcm = await asyncio.to_thread(
                self.clips.joined, list(step.clips), self.leg.input_rate, step.gap_s
            )
            seconds = len(pcm) / 2 / self.leg.input_rate
            # Appended, not replaced: one turn can ask for two answers.
            pause = b"\0" * (int(self.leg.input_rate * step.gap_s) * 2) if self._queue else b""
            self._barge_counted = False
            self._queue_talk_over = step.talk_over and (
                self._queue_talk_over if self._queue else True
            )
            queued = True
            self._queued_steps.append(index)
            self._queue += pause + pcm
            self._note("said", step=index, clips=list(step.clips), seconds=round(seconds, 2))
        entry = Said(
            t=round(self._now(), 2),
            step_index=index,
            field=step.field,
            intended=step.intended,
            clips=list(step.clips),
            dtmf=step.dtmf,
            seconds=round(seconds, 2),
            talk_over=step.talk_over,
            answering=answering[-140:],
            labels_unverified=unverified,
            fallback=fallback,
            synthetic_clips=synthetic,
        )
        self.record.said.append(entry)
        if queued:
            self._queued_said.append(entry)
