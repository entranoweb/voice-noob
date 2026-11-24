"""Who may embed an agent, and what a session token proves.

The widget is public, so these rules are the whole of what stands between an
agent's public id and its owner's tools and model credits. Each test names the
request that used to be served and is not any more.
"""

from __future__ import annotations

import time

import pytest

from app.core.config import settings
from app.core.embed_session import (
    DIRECT,
    embedding_site,
    mint_embed_session,
    session_rate_key,
    site_allowed,
    tools_allowed,
    verify_embed_session,
)
from app.core.stream_token import StreamTokenVerdict, mint_stream_token

AGENT = "3f0c2a52-6d0e-4a0b-9d4e-0b7f6f1f2a11"
OTHER_AGENT = "9a1b2c3d-0000-4000-8000-aaaaaaaaaaaa"


@pytest.fixture(autouse=True)
def deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.synthiq.example")
    monkeypatch.setattr(settings, "CORS_ORIGINS", ["https://app.synthiq.example"])
    monkeypatch.setattr(settings, "EMBED_ALLOW_ANY_ORIGIN_WHEN_UNSET", False)
    monkeypatch.setattr(settings, "EMBED_TOOLS_DEFAULT_ENABLED", False)
    monkeypatch.setattr(settings, "EMBED_SESSION_MAX_SECONDS", 900)


class TestWhichSiteIsEmbedding:
    def test_a_script_on_the_customers_page_is_named_by_its_origin(self) -> None:
        assert embedding_site("https://shop.example.com", None) == "shop.example.com"

    def test_an_iframe_served_by_our_app_is_named_by_the_page_around_it(self) -> None:
        site = embedding_site("https://app.synthiq.example", "https://shop.example.com")
        assert site == "shop.example.com"

    def test_a_same_origin_get_with_no_origin_header_uses_the_parent(self) -> None:
        assert embedding_site(None, "https://shop.example.com/pricing") == "shop.example.com"

    def test_the_widget_page_opened_on_its_own_is_direct(self) -> None:
        assert embedding_site("https://app.synthiq.example", None) == DIRECT
        assert embedding_site(None, None) == DIRECT

    def test_framed_by_our_own_dashboard_is_direct(self) -> None:
        assert (
            embedding_site("https://app.synthiq.example", "https://app.synthiq.example") == DIRECT
        )

    def test_a_third_party_origin_cannot_borrow_a_friendlier_parent(self) -> None:
        """The browser's Origin wins over a header the page chose to send."""
        site = embedding_site("https://evil.example", "https://shop.example.com")
        assert site == "evil.example"


class TestTheAllowList:
    def test_an_empty_list_admits_no_other_site(self) -> None:
        """It used to admit every site."""
        assert site_allowed("shop.example.com", []) is False
        assert site_allowed("shop.example.com", None) is False

    def test_the_override_restores_the_old_meaning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "EMBED_ALLOW_ANY_ORIGIN_WHEN_UNSET", True)
        assert site_allowed("shop.example.com", []) is True

    def test_the_override_does_not_widen_a_list_that_is_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "EMBED_ALLOW_ANY_ORIGIN_WHEN_UNSET", True)
        assert site_allowed("evil.example", ["shop.example.com"]) is False

    def test_the_widget_page_itself_is_always_served(self) -> None:
        assert site_allowed(DIRECT, []) is True

    def test_exact_and_wildcard_patterns(self) -> None:
        allowed = ["shop.example.com", "*.partner.example"]
        assert site_allowed("shop.example.com", allowed) is True
        assert site_allowed("a.partner.example", allowed) is True
        assert site_allowed("partner.example", allowed) is False
        assert site_allowed("evilpartner.example", allowed) is False
        assert site_allowed("shop.example.com.evil.example", allowed) is False


class TestTheSessionToken:
    def test_a_minted_session_verifies_for_its_agent_and_site(self) -> None:
        session = mint_embed_session(agent_id=AGENT, site="shop.example.com")
        check = verify_embed_session(session.token, agent_id=AGENT, site="shop.example.com")
        assert check.ok
        assert check.session_id == session.session_id
        assert check.expires_at > time.time()

    def test_it_is_refused_for_another_agent(self) -> None:
        session = mint_embed_session(agent_id=AGENT, site="shop.example.com")
        check = verify_embed_session(session.token, agent_id=OTHER_AGENT, site="shop.example.com")
        assert check.verdict is StreamTokenVerdict.BAD_SIGNATURE

    def test_it_is_refused_from_another_site(self) -> None:
        """A token lifted from one page is no use from another."""
        session = mint_embed_session(agent_id=AGENT, site="shop.example.com")
        check = verify_embed_session(session.token, agent_id=AGENT, site="evil.example")
        assert check.verdict is StreamTokenVerdict.BAD_SIGNATURE

    def test_it_expires_at_the_session_cap(self) -> None:
        session = mint_embed_session(agent_id=AGENT, site=DIRECT, now=time.time() - 901)
        check = verify_embed_session(session.token, agent_id=AGENT, site=DIRECT)
        assert check.verdict is StreamTokenVerdict.EXPIRED

    def test_a_tampered_token_is_refused(self) -> None:
        session = mint_embed_session(agent_id=AGENT, site=DIRECT)
        head, _, last = session.token.rpartition(".")
        forged = f"{head}.{'A' if last[0] != 'A' else 'B'}{last[1:]}"
        assert verify_embed_session(forged, agent_id=AGENT, site=DIRECT).ok is False

    @pytest.mark.parametrize("token", [None, "", "no-dots", "only.one"])
    def test_missing_and_malformed_tokens_are_refused(self, token: str | None) -> None:
        assert verify_embed_session(token, agent_id=AGENT, site=DIRECT).ok is False

    def test_a_carrier_stream_token_is_not_an_embed_session(self) -> None:
        """Same signing primitive, different purpose: neither stands in for the other."""
        carrier = mint_stream_token(provider="twilio", agent_id=AGENT, call_id="CA1")
        assert verify_embed_session(f"CA1.{carrier}", agent_id=AGENT, site=DIRECT).ok is False

    def test_the_rate_limit_key_never_contains_the_signature(self) -> None:
        session = mint_embed_session(agent_id=AGENT, site=DIRECT)
        key = session_rate_key(session.token)
        assert session.session_id in key
        assert session.token.rpartition(".")[2] not in key
        assert session_rate_key(None) == "embed-session:none"


class TestToolsSwitch:
    def test_tools_are_off_unless_the_owner_turned_them_on(self) -> None:
        assert tools_allowed({}) is False
        assert tools_allowed(None) is False
        assert tools_allowed({"allow_tools": True}) is True

    def test_an_explicit_off_beats_the_deployment_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "EMBED_TOOLS_DEFAULT_ENABLED", True)
        assert tools_allowed({}) is True
        assert tools_allowed({"allow_tools": False}) is False
