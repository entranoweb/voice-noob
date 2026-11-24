"""A stream token admits one connection, and nothing when that cannot be shown.

Before this a token was good for as many streams as anyone opened inside its two
minutes. The claim is what makes it single-use, and the last test is the one
that matters most: when the store is down the answer is no.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core import stream_token_use
from app.core.stream_token import mint_stream_token
from app.core.stream_token_use import TokenClaim, claim_stream_token

AGENT = "3f0c2a52-6d0e-4a0b-9d4e-0b7f6f1f2a11"


def _token(call_id: str = "CA1") -> str:
    return mint_stream_token(provider="twilio", agent_id=AGENT, call_id=call_id)


@pytest.mark.asyncio
async def test_the_first_presentation_claims_the_token() -> None:
    assert await claim_stream_token(_token()) is TokenClaim.CLAIMED


@pytest.mark.asyncio
async def test_a_replay_inside_the_tokens_life_is_refused() -> None:
    token = _token()
    assert (await claim_stream_token(token)).ok
    replay = await claim_stream_token(token)
    assert replay is TokenClaim.REPLAYED
    assert replay.ok is False


@pytest.mark.asyncio
async def test_claiming_one_calls_token_leaves_another_calls_alone() -> None:
    assert (await claim_stream_token(_token("CA1"))).ok
    assert (await claim_stream_token(_token("CA2"))).ok


@pytest.mark.asyncio
async def test_the_claim_expires_with_the_token(stream_token_claim_store: Any) -> None:
    token = _token()
    await claim_stream_token(token, ttl_seconds=120)
    keys = await stream_token_claim_store.keys("stream-token-used:*")
    assert len(keys) == 1
    assert 0 < await stream_token_claim_store.ttl(keys[0]) <= 120


@pytest.mark.asyncio
async def test_the_token_itself_is_never_a_key(stream_token_claim_store: Any) -> None:
    token = _token()
    await claim_stream_token(token)
    keys = await stream_token_claim_store.keys("*")
    assert all(token not in key for key in keys)


@pytest.mark.asyncio
async def test_when_the_store_is_down_the_stream_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: an outage must not quietly reopen the replay window."""

    async def _down() -> Any:
        raise ConnectionError("redis is unreachable")

    monkeypatch.setattr(stream_token_use, "_store", _down)

    claim = await claim_stream_token(_token())
    assert claim is TokenClaim.STORE_UNAVAILABLE
    assert claim.ok is False
