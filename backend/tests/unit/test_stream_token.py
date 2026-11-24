"""The token that admits a carrier's media stream to an agent.

Each test names one way an attacker, or a mistake, could present a token that
should not open the socket. The socket-level behaviour is covered where the
sockets are tested; these pin the primitive.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.core.stream_token import (
    DEFAULT_TTL_SECONDS,
    StreamTokenVerdict,
    mint_stream_token,
    verify_stream_token,
)

AGENT = "3f1c6f0e-7c1e-4b9a-9d55-0a1b2c3d4e5f"
OTHER_AGENT = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
CALL = "v3:a-real-looking-call-identifier"
NOW = 1_800_000_000.0
# Digits that are not ASCII digits: str.isdigit() accepts them and int() parses
# them, so an expiry written this way must be refused before it is read.
FULLWIDTH_EXPIRY = "".join(chr(code) for code in (0xFF11, 0xFF12, 0xFF13)) + ".sig"


def _mint(**overrides: object) -> str:
    fields: dict[str, object] = {"provider": "telnyx", "agent_id": AGENT, "call_id": CALL}
    fields.update(overrides)
    return mint_stream_token(now=NOW, **fields)  # type: ignore[arg-type]


def _verify(token: str | None, *, at: float = NOW + 1, **overrides: object) -> StreamTokenVerdict:
    fields: dict[str, object] = {"provider": "telnyx", "agent_id": AGENT, "call_id": CALL}
    fields.update(overrides)
    return verify_stream_token(token, now=at, **fields).verdict  # type: ignore[arg-type]


class TestAdmitted:
    def test_a_fresh_token_for_this_agent_and_call_is_admitted(self) -> None:
        assert _verify(_mint()) is StreamTokenVerdict.OK

    def test_it_is_safe_to_put_in_a_url_or_an_xml_attribute_unescaped(self) -> None:
        """It travels in a query string on one carrier and an attribute on the other."""
        token = _mint()

        assert all(c.isalnum() or c in "._-" for c in token), token

    def test_it_does_not_carry_the_key_or_the_identifiers(self) -> None:
        token = _mint()

        assert settings.SECRET_KEY not in token
        assert AGENT not in token
        assert CALL not in token


class TestRefused:
    @pytest.mark.parametrize("token", [None, ""])
    def test_no_token(self, token: str | None) -> None:
        assert _verify(token) is StreamTokenVerdict.MISSING

    @pytest.mark.parametrize(
        "token",
        ["garbage", "1800000100", "abc.def", "1.2.3", ".sig", FULLWIDTH_EXPIRY, "-5.sig"],
    )
    def test_a_malformed_token(self, token: str) -> None:
        assert _verify(token) is StreamTokenVerdict.MALFORMED

    def test_an_expired_token(self) -> None:
        token = _mint()

        assert _verify(token, at=NOW + DEFAULT_TTL_SECONDS - 1) is StreamTokenVerdict.OK
        assert _verify(token, at=NOW + DEFAULT_TTL_SECONDS) is StreamTokenVerdict.EXPIRED
        assert _verify(token, at=NOW + 3600) is StreamTokenVerdict.EXPIRED

    def test_a_token_with_its_expiry_pushed_out(self) -> None:
        """The expiry is signed, so extending it is the same as forging it."""
        expires_at, signature = _mint().split(".")
        stretched = f"{int(expires_at) + 86_400}.{signature}"

        assert _verify(stretched) is StreamTokenVerdict.BAD_SIGNATURE

    def test_a_tampered_signature(self) -> None:
        expires_at, signature = _mint().split(".")
        flipped = ("A" if signature[0] != "A" else "B") + signature[1:]

        assert _verify(f"{expires_at}.{flipped}") is StreamTokenVerdict.BAD_SIGNATURE

    def test_another_agents_token(self) -> None:
        """The hole this closes: a token for one agent must not open another."""
        token = _mint(agent_id=OTHER_AGENT)

        assert _verify(token) is StreamTokenVerdict.BAD_SIGNATURE

    def test_a_token_carried_to_another_call(self) -> None:
        """The call identifier in the stream URL is a write key on its own
        (DECISIONS §33), so the token is bound to it too."""
        token = _mint()

        assert _verify(token, call_id="v3:someone-elses-call") is StreamTokenVerdict.BAD_SIGNATURE

    def test_a_token_minted_for_the_other_carrier(self) -> None:
        token = _mint(provider="twilio")

        assert _verify(token) is StreamTokenVerdict.BAD_SIGNATURE

    def test_fields_cannot_be_shifted_across_their_boundaries(self) -> None:
        """("ab", "c") and ("a", "bc") must not sign to the same bytes."""
        token = _mint(agent_id="agent-ab", call_id="c")

        verdict = _verify(token, agent_id="agent-a", call_id="bc")

        assert verdict is StreamTokenVerdict.BAD_SIGNATURE

    def test_a_token_signed_under_another_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """What a deployment whose instances do not share SECRET_KEY looks like,
        and what an attacker without the key is limited to."""
        token = _mint()
        monkeypatch.setattr(settings, "SECRET_KEY", "a-different-signing-key-entirely")

        assert _verify(token) is StreamTokenVerdict.BAD_SIGNATURE

    def test_a_forgery_learns_nothing_about_expiry(self) -> None:
        """A bad signature is reported as such even when the expiry it carries
        has passed, so probing cannot map which expiries are live."""
        expires_at, _ = _mint().split(".")

        verdict = _verify(f"{expires_at}.not-the-signature", at=NOW + 10_000)

        assert verdict is StreamTokenVerdict.BAD_SIGNATURE
