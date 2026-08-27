"""Tests for the adaptive caller.

A fixed script hands the agent its next line whether or not the agent earned
it. These cover the behaviour that replaces that: reacting, stopping, and
refusing to do the agent's job for it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.services.qa.caller import (
    DONE,
    MAX_TURNS_CEILING,
    AdaptiveCaller,
    Persona,
)


def _reply(text: str) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


class _FakeClient:
    def __init__(self, *replies: str) -> None:
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs: Any) -> Any:
        self.calls.append({**kwargs, "messages": list(kwargs.get("messages", []))})
        text = self._replies.pop(0) if self._replies else "Anything else?"
        return _reply(text)


def _caller(persona: Persona, *replies: str) -> tuple[AdaptiveCaller, _FakeClient]:
    client = _FakeClient(*replies)
    return AdaptiveCaller(persona, client, "test-model"), client


class TestPersona:
    def test_the_goal_reaches_the_system_prompt(self) -> None:
        prompt = Persona(goal="Book a haircut for Tuesday").system_prompt()
        assert "Book a haircut for Tuesday" in prompt

    def test_facts_are_listed_for_the_caller_to_hand_over(self) -> None:
        prompt = Persona(goal="book", facts={"phone": "5551234567"}).system_prompt()
        assert "phone: 5551234567" in prompt

    def test_a_persona_with_no_facts_still_renders(self) -> None:
        assert "nothing in particular" in Persona(goal="book").system_prompt()

    def test_traits_are_appended_as_instructions(self) -> None:
        prompt = Persona(goal="book", traits=("You are in a hurry.",)).system_prompt()
        assert "You are in a hurry." in prompt

    def test_the_turn_budget_is_capped(self) -> None:
        """A persona cannot ask for an unbounded conversation."""
        assert Persona(goal="book", max_turns=500).turn_budget() == MAX_TURNS_CEILING

    def test_the_budget_is_at_least_one(self) -> None:
        assert Persona(goal="book", max_turns=0).turn_budget() == 1


@pytest.mark.asyncio
class TestSpeaking:
    async def test_the_first_turn_needs_no_agent_utterance(self) -> None:
        caller, _client = _caller(Persona(goal="book"), "Hi, can I book something?")
        assert await caller.speak() == "Hi, can I book something?"

    async def test_a_scripted_opening_skips_the_model(self) -> None:
        """A fixed first line makes two configurations start identically, which
        is what makes an A/B comparison a comparison."""
        caller, client = _caller(Persona(goal="book", opening="Hi, I'd like to book."))
        assert await caller.speak() == "Hi, I'd like to book."
        assert client.calls == []

    async def test_it_reacts_to_what_the_agent_said(self) -> None:
        caller, client = _caller(Persona(goal="book"), "Hello?", "It's 5551234567.")
        await caller.speak()
        caller.hear("Sure - what number can I reach you on?")
        await caller.speak()

        history = client.calls[-1]["messages"]
        assert history[-1] == {
            "role": "user",
            "content": "Sure - what number can I reach you on?",
        }

    async def test_silence_is_recorded_rather_than_dropped(self) -> None:
        """An agent that said nothing is a real event the caller reacts to."""
        caller, client = _caller(Persona(goal="book"), "Hello?", "Are you there?")
        await caller.speak()
        caller.hear("")
        await caller.speak()
        assert client.calls[-1]["messages"][-1]["content"] == "(silence)"

    async def test_the_done_sentinel_ends_the_conversation(self) -> None:
        caller, _client = _caller(Persona(goal="book"), "Hi", DONE)
        await caller.speak()
        caller.hear("You're booked.")
        assert await caller.speak() is None

    async def test_a_final_utterance_alongside_done_is_still_spoken(self) -> None:
        """ "Great, thanks, bye" is a real turn; discarding it would lose the
        agent's chance to close the call properly."""
        caller, _client = _caller(Persona(goal="book"), "Hi", f"Great, thanks! {DONE}")
        await caller.speak()
        caller.hear("You're booked.")

        assert await caller.speak() == "Great, thanks!"
        assert caller.finished is True

    async def test_nothing_more_is_said_after_finishing(self) -> None:
        caller, _client = _caller(Persona(goal="book"), "Hi", f"Bye. {DONE}")
        await caller.speak()
        caller.hear("Done.")
        await caller.speak()
        assert await caller.speak() is None

    async def test_it_stops_at_the_turn_budget(self) -> None:
        """Otherwise a caller and an agent can talk in circles until the token
        budget notices."""
        caller, _client = _caller(Persona(goal="book", max_turns=2), "one", "two", "three")

        assert await caller.speak() == "one"
        caller.hear("mm-hmm")
        assert await caller.speak() == "two"
        caller.hear("mm-hmm")
        assert await caller.speak() is None

    async def test_the_persona_prompt_is_sent_as_the_system_prompt(self) -> None:
        caller, client = _caller(Persona(goal="Book a haircut"), "Hi")
        await caller.speak()
        assert "Book a haircut" in client.calls[0]["system"]

    async def test_history_starts_with_a_user_turn(self) -> None:
        """The Messages API rejects a conversation that opens with assistant."""
        caller, client = _caller(Persona(goal="book"), "Hi")
        await caller.speak()
        assert client.calls[0]["messages"][0]["role"] == "user"
