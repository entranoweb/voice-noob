"""Session tokens and origin rules for the public embed widget.

The widget is public by design: an anonymous visitor on a customer's site talks
to that customer's agent. What it must not be is a free, unlimited way to run an
agent's tools and spend its owner's model credits for anyone who has read the
agent's public id out of a page source.

A session token is what every call after the first has to carry. It is minted by
an HTTP endpoint that has checked where the widget is embedded, it is signed with
the same primitive as a carrier's stream token (``app.core.stream_token``, under
its own provider name so neither can stand in for the other), and it binds:

* the agent, so a session for one agent does not work on another;
* a random session id, which rate limits key on;
* the embedding site it was minted for, so a token lifted from one page is no
  use from another;
* an expiry, which is also the hard cap on how long a session can run.

What the origin check is and is not. A browser reports the embedding site
truthfully and a script does not have to, so the allow-list stops other websites
putting someone else's agent in front of their own visitors. It does not stop a
scripted client. That is what the rate limits, the session cap and the per-agent
tool switch are for, and why none of them trusts the origin.
"""

from __future__ import annotations

import fnmatch
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import structlog

from app.core.config import settings
from app.core.public_url import frontend_base_url
from app.core.stream_token import StreamTokenVerdict, mint_stream_token, verify_stream_token

logger = structlog.get_logger()

EMBED_PROVIDER = "embed"
SESSION_HEADER = "X-Embed-Session"
PARENT_ORIGIN_HEADER = "X-Embed-Parent-Origin"
# Close codes for the embed socket, matching the telephony sockets' meaning.
EMBED_SESSION_REJECTED = 4401
EMBED_SESSION_EXPIRED = 4408

# The widget page itself, opened directly with no embedding site around it.
DIRECT = "direct"


def _host(origin: str | None) -> str:
    if not origin:
        return ""
    try:
        return (urlparse(origin.strip()).hostname or "").lower()
    except ValueError:
        return ""


def _is_first_party(origin: str | None) -> bool:
    """Whether ``origin`` is this deployment's own web app."""
    host = _host(origin)
    if not host:
        return False
    candidates = [frontend_base_url(), *settings.CORS_ORIGINS]
    return any(host == _host(candidate) for candidate in candidates if candidate != "*")


def embedding_site(origin: str | None, parent_origin: str | None) -> str:
    """The site the widget is embedded in, as far as a browser will tell us.

    There are two shapes. A script on the customer's page calls this API
    cross-origin, and the browser's ``Origin`` header names the customer's site.
    Or the widget runs in an iframe served by this deployment's own web app, in
    which case ``Origin`` names the web app (or is absent on a same-origin GET)
    and the page reports the site around it in ``X-Embed-Parent-Origin``.

    Returns the host, or ``DIRECT`` when the widget page was opened on its own.
    """
    if origin and not _is_first_party(origin):
        return _host(origin)
    # Framed by this deployment's own web app (the dashboard preview) counts as
    # opened directly: there is no other site involved.
    if not _host(parent_origin) or _is_first_party(parent_origin):
        return DIRECT
    return _host(parent_origin)


def site_allowed(site: str, allowed_domains: list[str] | None) -> bool:
    """Whether ``site`` may embed an agent with this allow-list.

    The widget page opened directly is always served: it is this deployment's own
    page, and it is how the dashboard previews an agent.

    An empty list means no other site may embed the agent. It used to mean every
    site could, which made the allow-list something an owner had to know to fill
    in before it protected anything. ``EMBED_ALLOW_ANY_ORIGIN_WHEN_UNSET`` restores
    the old meaning for a deployment that needs time to configure its agents, and
    says so each time it is the reason a request was served.
    """
    if site == DIRECT:
        return True

    patterns = [p.strip().lower() for p in (allowed_domains or []) if p and p.strip()]
    if not patterns:
        if settings.EMBED_ALLOW_ANY_ORIGIN_WHEN_UNSET:
            logger.warning(
                "embed_origin_allowed_by_override",
                site=site,
                detail=(
                    "This agent has no allowed domains and "
                    "EMBED_ALLOW_ANY_ORIGIN_WHEN_UNSET is true, so any site may "
                    "embed it. Set the agent's allowed domains and turn this off."
                ),
            )
            return True
        return False

    for pattern in patterns:
        if pattern.startswith("*."):
            # "*.example.com" covers subdomains, not "example.com" itself and not
            # "badexample.com".
            if fnmatch.fnmatch(site, f"*{pattern[1:]}"):
                return True
        elif site == pattern:
            return True
    return False


def tools_allowed(embed_settings: dict[str, Any] | None) -> bool:
    """Whether embedded sessions for this agent may call its tools.

    Off unless the owner turned it on (``embed_settings.allow_tools``). A tool
    call from the widget acts with the owner's integrations on input from an
    anonymous visitor, and that is a decision, not a default.
    """
    value = (embed_settings or {}).get("allow_tools")
    if isinstance(value, bool):
        return value
    return bool(settings.EMBED_TOOLS_DEFAULT_ENABLED)


@dataclass(frozen=True)
class EmbedSession:
    session_id: str
    token: str
    expires_in: int


def _binding(session_id: str, site: str) -> str:
    return f"{session_id}|{site}"


def mint_embed_session(*, agent_id: str, site: str, now: float | None = None) -> EmbedSession:
    """A session for one agent, embedded in one site, for a bounded time."""
    session_id = secrets.token_urlsafe(18)
    ttl = max(1, int(settings.EMBED_SESSION_MAX_SECONDS))
    signed = mint_stream_token(
        provider=EMBED_PROVIDER,
        agent_id=agent_id,
        call_id=_binding(session_id, site),
        ttl_seconds=ttl,
        now=now,
    )
    return EmbedSession(session_id=session_id, token=f"{session_id}.{signed}", expires_in=ttl)


@dataclass(frozen=True)
class EmbedSessionCheck:
    verdict: StreamTokenVerdict
    session_id: str = ""
    expires_at: int = 0

    @property
    def ok(self) -> bool:
        return self.verdict is StreamTokenVerdict.OK


def verify_embed_session(
    token: str | None, *, agent_id: str, site: str, now: float | None = None
) -> EmbedSessionCheck:
    """Whether ``token`` is a live session for this agent from this site."""
    if not token:
        return EmbedSessionCheck(StreamTokenVerdict.MISSING)
    session_id, separator, signed = token.partition(".")
    if not separator or not session_id or not signed:
        return EmbedSessionCheck(StreamTokenVerdict.MALFORMED)
    result = verify_stream_token(
        signed,
        provider=EMBED_PROVIDER,
        agent_id=agent_id,
        call_id=_binding(session_id, site),
        now=now,
    )
    if not result.ok:
        return EmbedSessionCheck(result.verdict)
    expires_at = int(signed.split(".", 1)[0])
    return EmbedSessionCheck(StreamTokenVerdict.OK, session_id=session_id, expires_at=expires_at)


def session_rate_key(token: str | None) -> str:
    """A rate-limit key for a session token that never stores the token itself."""
    session_id = (token or "").partition(".")[0]
    return f"embed-session:{session_id[:48]}" if session_id else "embed-session:none"


__all__ = [
    "DIRECT",
    "EMBED_SESSION_EXPIRED",
    "EMBED_SESSION_REJECTED",
    "PARENT_ORIGIN_HEADER",
    "SESSION_HEADER",
    "EmbedSession",
    "EmbedSessionCheck",
    "embedding_site",
    "mint_embed_session",
    "session_rate_key",
    "site_allowed",
    "tools_allowed",
    "verify_embed_session",
]
