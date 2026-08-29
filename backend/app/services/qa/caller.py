"""A simulated caller that reacts, instead of reading from a script.

A fixed ``conversation_flow`` can only find the failures someone already
imagined. It also flatters the agent: the script's next line arrives whether or
not the agent answered the previous one, so an agent that ignored a question
still gets handed the information it needed.

An adaptive caller has a goal and responds to what the agent actually said. If
the agent asks for a phone number, the caller gives the one in its persona. If
the agent stalls, the caller pushes. If the agent refuses, the caller stops
rather than repeating itself into the turn limit.

Two things this deliberately does not do. It does not try to be helpful — a
simulated caller that fills in gaps the agent should have asked about tests
nothing. And it does not decide whether the run passed; the caller talks, the
metrics judge. Letting the caller conclude "great, that worked" would put a
model's opinion back in the position deterministic assertions are supposed to
hold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()

# The caller emits this when its goal is met or plainly refused. A sentinel
# rather than a judgement: it ends the conversation and says nothing about
# whether the agent did the right thing.
DONE = "<DONE>"

# A hard ceiling regardless of persona, so a caller and an agent cannot talk
# each other in circles until the token budget notices.
MAX_TURNS_CEILING = 20

CALLER_SYSTEM = """You are a person telephoning a business. Stay in character \
at all times.

Who you are:
{identity}

What you want from this call:
{goal}

Facts you know about yourself. Give these out when, and only when, you are \
asked for them:
{facts}

How to behave:
- Speak one short utterance at a time, the way people actually talk on the \
phone. No stage directions, no narration, no quotation marks.
- Answer what was actually said to you. If the agent asked a question, answer \
that question.
- Do not volunteer information nobody asked for, and do not do the agent's job \
for it. If it forgets to ask for something it needs, let it forget.
- Do not be unusually patient or unusually helpful. You are a customer, not a \
tester.
{traits}
- When you have got what you called for, or the agent has clearly refused or \
cannot help, reply with exactly {done} and nothing else.

Never say {done} merely because the agent asked a question or the conversation \
is going slowly. Only when it is genuinely finished."""


@dataclass(frozen=True)
class Persona:
    """Who is calling, what they want, and what they know."""

    goal: str
    identity: str = "An ordinary customer."
    facts: dict[str, str] = field(default_factory=dict)
    traits: tuple[str, ...] = ()
    max_turns: int = 8
    opening: str | None = None

    def turn_budget(self) -> int:
        """Turns the caller may take, never above the ceiling."""
        return max(1, min(self.max_turns, MAX_TURNS_CEILING))

    def system_prompt(self) -> str:
        facts = (
            "\n".join(f"- {key}: {value}" for key, value in self.facts.items())
            or "- (nothing in particular)"
        )
        traits = "".join(f"\n- {trait}" for trait in self.traits)
        return CALLER_SYSTEM.format(
            identity=self.identity,
            goal=self.goal,
            facts=facts,
            traits=traits,
            done=DONE,
        )


def _strip_done(text: str) -> tuple[str, bool]:
    """Split a reply into what was said and whether the caller is finished.

    The sentinel is honoured even when the model wraps it in a sentence, which
    it sometimes does; the surrounding words are discarded because they are the
    model narrating rather than the caller speaking.
    """
    if DONE not in text:
        return text.strip(), False
    remainder = text.replace(DONE, "").strip()
    return remainder, True


class AdaptiveCaller:
    """Generates the caller's side of a conversation, one turn at a time."""

    def __init__(self, persona: Persona, client: Any, model: str) -> None:
        self.persona = persona
        self._client = client
        self._model = model
        self._history: list[dict[str, Any]] = []
        # Set once the caller signals it is done. A final utterance can still
        # accompany that signal - "great, thanks, bye" - so the flag is read
        # after the turn is delivered, not instead of it.
        self.finished = False

    @property
    def turns_taken(self) -> int:
        return sum(1 for entry in self._history if entry["role"] == "assistant")

    def hear(self, agent_utterance: str) -> None:
        """Record what the agent said.

        Roles are swapped relative to the agent's own view: from the caller's
        side, the agent is the one being listened to.
        """
        self._history.append({"role": "user", "content": agent_utterance or "(silence)"})

    async def speak(self) -> str | None:
        """The caller's next utterance, or None when it is finished."""
        if self.finished:
            return None

        if self.turns_taken >= self.persona.turn_budget():
            logger.info("caller_turn_budget_reached", turns=self.turns_taken)
            return None

        if not self._history and self.persona.opening:
            # A scripted opening keeps the first turn reproducible, which makes
            # a comparison between two agent configurations start identically.
            self._history.append({"role": "assistant", "content": self.persona.opening})
            return self.persona.opening

        # The Messages API needs the conversation to start with a user turn.
        messages = self._history or [{"role": "user", "content": "(the line connects)"}]

        response = await self._client.messages.create(
            model=self._model,
            max_tokens=300,
            system=self.persona.system_prompt(),
            messages=messages,
        )

        spoken = "".join(
            str(block.text) for block in response.content if getattr(block, "type", "") == "text"
        )
        utterance, finished = _strip_done(spoken)

        self.finished = finished
        if not utterance:
            # The sentinel arrived alone: nothing left to say.
            return None

        self._history.append({"role": "assistant", "content": utterance})
        return utterance


__all__ = [
    "DONE",
    "MAX_TURNS_CEILING",
    "AdaptiveCaller",
    "Persona",
]
