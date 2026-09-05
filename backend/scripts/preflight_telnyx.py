#!/usr/bin/env python
"""Everything that can be checked before a real call, checked.

Run this against a deployed instance before dialling the number. It exists
because the inbound path fails silently: a misconfigured host produces an answer
document that parses, a webhook that logs healthy, and a caller who hears
nothing. Each check below is one thing that has to be true for audio to reach
the agent and a row to land, in the order the call itself will exercise them.

    uv run python scripts/preflight_telnyx.py                # local settings
    uv run python scripts/preflight_telnyx.py --url https://voice.example.com

Checks that talk to the deployment use ``--url`` (or ``PUBLIC_URL``); checks
that read the database and Redis use this process's own settings, so run it
where those are reachable. Nothing here places a call, writes a row, or spends
carrier money. Exit status is 0 only when every check passed.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

BACKEND = Path(__file__).resolve().parent.parent

# E.164: a leading +, a non-zero country digit, up to fifteen digits in total.
E164 = re.compile(r"^\+[1-9]\d{1,14}$")

# The bridge closes with this when the agent id in the stream URL matches no
# agent. Reaching it proves TLS, the proxy's websocket upgrade, routing and the
# app's database session all work, without touching a real agent.
AGENT_NOT_FOUND = 4004


class Level(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class Result:
    level: Level
    check: str
    detail: str


def ok(check: str, detail: str) -> Result:
    return Result(Level.PASS, check, detail)


def warn(check: str, detail: str) -> Result:
    return Result(Level.WARN, check, detail)


def fail(check: str, detail: str) -> Result:
    return Result(Level.FAIL, check, detail)


def check_credentials() -> list[Result]:
    """The keys, and the one setting that quietly disables signature checking."""
    from app.core.config import settings

    results: list[Result] = []

    # TELNYX_API_KEY is only needed for outbound calls and the number-purchase
    # flow, so its absence does not stop an inbound call landing.
    if settings.TELNYX_API_KEY:
        results.append(ok("TELNYX_API_KEY", "set"))
    else:
        results.append(warn("TELNYX_API_KEY", "unset — inbound works, outbound does not"))

    if settings.TELNYX_PUBLIC_KEY:
        results.append(ok("TELNYX_PUBLIC_KEY", "set"))
    else:
        results.append(fail("TELNYX_PUBLIC_KEY", "unset — see DEBUG below"))

    if settings.DEBUG and not settings.TELNYX_PUBLIC_KEY:
        results.append(
            fail(
                "DEBUG",
                "true with no TELNYX_PUBLIC_KEY: webhook signatures are not "
                "verified and anyone can post a call to this instance",
            )
        )
    elif settings.DEBUG:
        results.append(warn("DEBUG", "true — turn it off before a real number points here"))
    else:
        results.append(ok("DEBUG", "false"))

    return results


def check_public_url(url: str | None) -> list[Result]:
    """The host the carrier is handed, which is what goes wrong in practice."""
    if not url:
        return [
            fail(
                "PUBLIC_URL",
                "unset — the stream URL then falls back to the request's own host, "
                "which behind a proxy is the internal address Telnyx cannot reach",
            )
        ]
    if url.startswith("http://"):
        if _is_loopback(url):
            return [warn("PUBLIC_URL", f"{url} is a local rehearsal, not a host Telnyx can reach")]
        return [
            warn(
                "PUBLIC_URL",
                f"{url} is cleartext; the stream URL is still emitted as wss:// and "
                "the TLS handshake will fail",
            )
        ]
    if not url.startswith("https://"):
        return [fail("PUBLIC_URL", f"{url} has no scheme")]
    return [ok("PUBLIC_URL", url)]


async def check_migrations() -> list[Result]:
    """One Alembic head, and the database actually at it."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import text

    from app.db.session import engine

    # Resolved against the script rather than the working directory, so this
    # reports on the deployment it ships with wherever it is run from.
    try:
        config = Config(str(BACKEND / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND / "migrations"))
        heads = ScriptDirectory.from_config(config).get_heads()
    except Exception as exc:  # a migration tree that will not load is a failed check
        return [fail("alembic heads", f"{type(exc).__name__}: {exc}")]

    if len(heads) != 1:
        return [fail("alembic heads", f"{len(heads)} heads: {', '.join(heads)}")]

    try:
        async with engine.connect() as connection:
            current = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    except Exception as exc:  # any failure here is a failed check
        return [fail("database", f"{type(exc).__name__}: {exc}")]

    if current != heads[0]:
        return [
            ok("database", "reachable"),
            fail("migrations", f"at {current}, head is {heads[0]} — run `alembic upgrade head`"),
        ]
    return [ok("database", "reachable"), ok("migrations", f"at head {heads[0]}")]


async def check_redis() -> list[Result]:
    from app.db.redis import get_redis

    try:
        client = await get_redis()
        await client.ping()
    except Exception as exc:  # any failure here is a failed check
        return [fail("redis", f"{type(exc).__name__}: {exc}")]
    return [ok("redis", "reachable")]


async def check_agent() -> list[Result]:
    """An active agent whose number is in the shape the webhook looks up by."""
    from sqlalchemy import select

    from app.db.session import AsyncSessionLocal
    from app.models.agent import Agent

    try:
        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    select(Agent.id, Agent.name, Agent.phone_number_id, Agent.is_active).where(
                        Agent.phone_number_id.isnot(None)
                    )
                )
            ).all()
    except Exception as exc:  # any failure here is a failed check
        return [fail("agent", f"{type(exc).__name__}: {exc}")]

    if not rows:
        return [fail("agent", "no agent has a phone_number_id set")]

    results: list[Result] = []
    reachable = 0
    for agent_id, name, number, is_active in rows:
        label = f"{name} ({agent_id})"
        if not E164.match(number or ""):
            results.append(
                fail("agent", f"{label}: phone_number_id {number!r} is not E.164 (+15551234567)")
            )
        elif not is_active:
            results.append(fail("agent", f"{label}: {number} but is_active is false (closes 4003)"))
        else:
            reachable += 1
            results.append(ok("agent", f"{label}: {number}, active"))

    if not reachable:
        results.append(fail("agent", "no agent is dialable"))
    return results


async def check_agent_openai_key() -> list[Result]:
    """The key the call uses, which is not the one in the environment.

    `OPENAI_API_KEY` in settings is read by other things; the telephony bridge
    reads `user_settings.openai_api_key` for the agent's *workspace*, with no
    fallback to user-level settings and none to the environment. An env-only
    check would pass while every call died at `initialize` with "API key not
    configured for this workspace", which is the kind of green tick this script
    exists not to give.
    """
    from sqlalchemy import select

    from app.api.settings import get_user_api_keys
    from app.api.telephony import get_agent_workspace_id
    from app.core.auth import user_id_to_uuid
    from app.db.session import AsyncSessionLocal
    from app.models.agent import Agent

    try:
        async with AsyncSessionLocal() as session:
            agents = (
                await session.execute(
                    select(Agent.id, Agent.name, Agent.user_id).where(
                        Agent.phone_number_id.isnot(None), Agent.is_active.is_(True)
                    )
                )
            ).all()

            if not agents:
                return []

            results: list[Result] = []
            for agent_id, name, user_id in agents:
                label = f"{name} ({agent_id})"
                workspace_id = await get_agent_workspace_id(agent_id, session)
                user_settings = await get_user_api_keys(
                    user_id_to_uuid(user_id), session, workspace_id=workspace_id
                )
                if not user_settings:
                    results.append(
                        fail("openai key", f"{label}: workspace has no settings row at all")
                    )
                    continue

                provider = getattr(user_settings, "openai_provider", "openai") or "openai"
                if provider == "azure":
                    have = bool(
                        user_settings.azure_openai_endpoint and user_settings.azure_openai_api_key
                    )
                    detail = (
                        "azure endpoint and key set" if have else "azure endpoint or key missing"
                    )
                else:
                    have = bool(user_settings.openai_api_key)
                    detail = "set" if have else "missing — add it in Settings > Workspace API Keys"
                results.append(
                    ok("openai key", f"{label}: {provider}, {detail}")
                    if have
                    else fail("openai key", f"{label}: {provider}, {detail}")
                )
            return results
    except Exception as exc:  # any failure here is a failed check
        return [fail("openai key", f"{type(exc).__name__}: {exc}")]


async def check_reachable(url: str) -> list[Result]:
    """The carrier has to reach the webhook host over TLS from outside."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(f"{url.rstrip('/')}/health")
    except Exception as exc:  # any failure here is a failed check
        return [fail("public host", f"GET {url}/health: {type(exc).__name__}: {exc}")]

    if response.status_code != 200:  # noqa: PLR2004
        return [fail("public host", f"GET {url}/health returned {response.status_code}")]
    return [ok("public host", f"{url}/health → 200")]


def _is_loopback(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host in {"localhost", "127.0.0.1", "::1"}


async def check_stream_url(url: str) -> list[Result]:
    """The check no other one covers: the media stream URL, actually opened.

    A random agent id is used on purpose. The bridge closes it with 4004 before
    it loads any config or opens a realtime session, so this proves the whole
    path — TLS, the proxy's websocket upgrade, routing, the app's database
    session — without touching a real agent or leaving anything behind.
    """
    import websockets
    from websockets.exceptions import ConnectionClosed, InvalidStatus

    origin = url.rstrip("/")
    if _is_loopback(origin):
        # Only so this script can be rehearsed against a local instance before
        # it is pointed at the deployment. Every other host gets wss, which is
        # what the answer document will carry and what Telnyx will dial.
        origin = origin.replace("http://", "ws://")
    origin = origin.replace("http://", "wss://").replace("https://", "wss://")
    stream_url = f"{origin}/ws/telephony/telnyx/{uuid.uuid4()}?direction=inbound"

    try:
        async with websockets.connect(stream_url, open_timeout=10) as socket:
            try:
                await asyncio.wait_for(socket.recv(), timeout=10)
            except ConnectionClosed as closed:
                if closed.rcvd and closed.rcvd.code == AGENT_NOT_FOUND:
                    return [ok("media stream", f"{stream_url} → 4004 (the bridge answered)")]
                code = closed.rcvd.code if closed.rcvd else "no close frame"
                return [warn("media stream", f"connected but closed with {code}")]
    except InvalidStatus as exc:
        return [
            fail(
                "media stream",
                f"{stream_url} → HTTP {exc.response.status_code}: the proxy is not "
                "forwarding the websocket upgrade",
            )
        ]
    except Exception as exc:  # any failure here is a failed check
        return [fail("media stream", f"{stream_url}: {type(exc).__name__}: {exc}")]

    return [warn("media stream", "connected and stayed open, which 4004 should have prevented")]


def check_tracing() -> list[Result]:
    """Optional, and reported as such — a call without a trace is still a call."""
    from app.core.config import settings

    if not settings.OTEL_ENABLED:
        return [warn("tracing", "OTEL_ENABLED is false — the call will produce no spans")]
    if not settings.OTEL_EXPORTER_OTLP_ENDPOINT:
        return [fail("tracing", "OTEL_ENABLED is true with no OTEL_EXPORTER_OTLP_ENDPOINT")]
    return [ok("tracing", settings.OTEL_EXPORTER_OTLP_ENDPOINT)]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=None,
        help="Public base URL of the deployment. Defaults to PUBLIC_URL.",
    )
    parser.add_argument(
        "--skip-remote",
        action="store_true",
        help="Skip the checks that talk to the deployed host.",
    )
    args = parser.parse_args()

    # The modules under test log their own failures, tracebacks included. This
    # script reports every one of them itself, and a report buried in a stack
    # trace is a report nobody reads.
    logging.disable(logging.ERROR)

    from app.core.config import settings

    url = args.url or settings.PUBLIC_URL

    results: list[Result] = []
    results += check_credentials()
    results += check_public_url(url)
    results += await check_migrations()
    results += await check_redis()
    results += await check_agent()
    results += await check_agent_openai_key()
    if url and not args.skip_remote:
        results += await check_reachable(url)
        results += await check_stream_url(url)
    results += check_tracing()

    marks = {Level.PASS: "  ok  ", Level.WARN: " warn ", Level.FAIL: " FAIL "}
    width = max(len(r.check) for r in results)
    for result in results:
        print(f"[{marks[result.level]}] {result.check.ljust(width)}  {result.detail}")

    failures = [r for r in results if r.level is Level.FAIL]
    warnings = [r for r in results if r.level is Level.WARN]
    print()
    if failures:
        print(f"{len(failures)} check(s) failed. The call will not work. Fix these first.")
        return 1
    print(f"All checks passed ({len(warnings)} warning(s)). Dial the number.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
