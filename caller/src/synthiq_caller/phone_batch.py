"""The phone leg inside a batch, and the checks to run before the first call.

A batch over the phone is the same batch as over a socket: the same scenario
files, the same guards, reset, read-back and chains, and run files of the same
shape. Three things differ, and they live here so ``batch.py`` does not have to
know about telephones:

* One listening server serves every call in the batch (``PhoneExchange``).
* The carrier's call id is not the agent platform's, so each call's trace is
  looked up by number and time (``traces/lookup.py``).
* Each side of each call is written to its own sound file.

Calls go out one at a time. A real call is placed only to a number on the
operator's list (``numbers.py``).

Nothing here has been run against a carrier. See ``caller/docs/PHONE-LEG.md``.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import websockets

from synthiq_caller.leg import Leg
from synthiq_caller.legs.twilio_media import PhoneConfig, PhoneExchange, TwilioMediaLeg
from synthiq_caller.numbers import ALLOWED_ENV, allowed_numbers, is_allowed, is_e164
from synthiq_caller.phone_server import PHONE_TURNS, build_transcriber, check_public_url
from synthiq_caller.traces.base import TraceReader
from synthiq_caller.traces.lookup import ConversationFinder, LookupTraceReader, PhoneCall
from synthiq_caller.transcribe import Transcriber
from synthiq_caller.turns import TurnSettings
from synthiq_caller.twilio_rest import CallControl, Credentials, MissingCredentials, TwilioRest

AUDIO_DIR = "audio"
TRACE_LOOKUPS = ("elevenlabs-number-time",)


def environment(env_file: str | None) -> dict[str, str]:
    """The process environment, with an env file filling in what it lacks."""
    env = dict(os.environ)
    if env_file:
        for line in Path(env_file).read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            name = name.strip()
            if sep and name and not name.startswith("#") and not env.get(name):
                env[name] = value.strip().strip('"').strip("'")
    return env


def phone_leg_factory(
    *,
    config: PhoneConfig,
    public_url: Callable[[], str],
    control: Callable[[], CallControl],
    auth_token: str,
    transcriber: Callable[[], Transcriber],
    exchange: PhoneExchange,
    finder: ConversationFinder | None,
    inner_reader: TraceReader | None,
) -> Callable[[], tuple[Leg, TraceReader | None]]:
    """A leg, and the reader of its trace, for each call of a batch.

    Every call gets its own leg, token and transcriber; the exchange and the
    carrier's control client are shared.
    """

    def make() -> tuple[Leg, TraceReader | None]:
        leg = TwilioMediaLeg(
            config, control(), auth_token=auth_token, transcriber=transcriber(), exchange=exchange
        )
        leg.public_url = public_url().rstrip("/")
        reader: TraceReader | None = None
        if finder is not None and inner_reader is not None:
            reader = LookupTraceReader(
                finder, inner_reader, lambda ref: PhoneCall.from_log(ref, asdict(leg.log))
            )
        return leg, reader

    return make


@dataclass
class PhoneBatch:
    """What ``batch --leg phone`` hands to the batch runner, and has to close afterwards."""

    make_leg: Callable[[], tuple[Leg, TraceReader | None]]
    exchange: PhoneExchange
    rest: TwilioRest
    turn_settings: TurnSettings

    async def aclose(self) -> None:
        await self.exchange.close()
        await self.rest.aclose()


def _stop(message: str) -> SystemExit:
    return SystemExit(f"batch --leg phone: {message}")


def checked_target(
    to_number: str, allowed_argument: str, env: Mapping[str, str]
) -> tuple[str, ...]:
    """The allow-list, having confirmed the number to dial is on it. Raises ``SystemExit``."""
    allowed = allowed_numbers(allowed_argument, env)
    if not allowed:
        raise _stop(
            "no numbers are allowed. List the numbers this caller may dial with "
            f"--allowed-numbers (a file, or a comma-separated list) or {ALLOWED_ENV}"
        )
    if not is_allowed(to_number, allowed):
        raise _stop(f"{to_number} is not on the list of numbers this caller may dial")
    return allowed


def open_phone_batch(args: argparse.Namespace, env: Mapping[str, str], out: Path) -> PhoneBatch:
    """Check everything that can be checked without a call, then build the batch's parts."""
    wanted = (("--to", args.to), ("--from", args.from_number), ("--public-url", args.public_url))
    missing = [flag for flag, value in wanted if not value]
    if missing:
        raise _stop(f"missing {', '.join(missing)}")
    if args.max_parallel != 1:
        raise _stop("calls over the phone go out one at a time; leave --max-parallel at 1")
    allowed = checked_target(args.to, args.allowed_numbers, env)
    public_url = check_public_url(args.public_url)
    try:
        credentials = Credentials.from_env(env)
    except MissingCredentials as error:
        raise _stop(str(error)) from error
    build_transcriber(args, env)  # fail now, not on the first call, when its key is missing

    finder: ConversationFinder | None = None
    inner: TraceReader | None = None
    if not args.no_trace:
        key = env.get("ELEVENLABS_API_KEY", "").strip()
        if not key:
            raise _stop(
                "ELEVENLABS_API_KEY is not set, and the trace lookup needs it. "
                "Pass --no-trace to place calls that will not be graded"
            )
        from synthiq_caller.traces.elevenlabs import ElevenLabsTraceReader
        from synthiq_caller.traces.lookup import ElevenLabsNumberTimeFinder

        finder = ElevenLabsNumberTimeFinder(
            key,
            agent_id=args.agent,
            window_s=args.trace_window,
            match_caller_number=not args.trace_ignore_caller_number,
        )
        inner = ElevenLabsTraceReader(key)

    rest = TwilioRest(credentials)
    exchange = PhoneExchange(args.host, args.port)
    config = PhoneConfig(
        to_number=args.to,
        from_number=args.from_number,
        public_url=public_url,
        listen_host=args.host,
        listen_port=args.port,
        record=not args.no_record,
        allowed_to=allowed,
        audio_dir="" if args.no_audio else str(out / AUDIO_DIR),
    )
    make = phone_leg_factory(
        config=config,
        public_url=lambda: public_url,
        control=lambda: rest,
        auth_token=credentials.auth_token,
        transcriber=lambda: build_transcriber(args, env),
        exchange=exchange,
        finder=finder,
        inner_reader=inner,
    )
    return PhoneBatch(make, exchange, rest, PHONE_TURNS)


# ----- preflight -------------------------------------------------------------


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    # A failed check that is not fatal is a warning: the call can still be placed.
    fatal: bool = True


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


async def phone_preflight(
    *,
    env: Mapping[str, str],
    to_number: str,
    from_number: str,
    public_url: str,
    allowed_argument: str = "",
    host: str = "0.0.0.0",  # noqa: S104 - the carrier connects in
    port: int = 8765,
    transcriber: str = "deepgram",
    trace: bool = True,
    twilio_client: httpx.AsyncClient | None = None,
    probe_client: httpx.AsyncClient | None = None,
) -> list[Check]:
    """Everything that can be checked without placing a call. Places no call.

    It reads one of the account's numbers from the carrier (a read, not a call),
    and it asks the public address for a probe that this process answers itself.
    """
    checks: list[Check] = []

    def add(name: str, ok: bool, detail: str, *, fatal: bool = True) -> None:
        checks.append(Check(name, ok, detail, fatal))

    credentials: Credentials | None = None
    try:
        credentials = Credentials.from_env(env)
        add("carrier credentials", True, "account id and auth token are set")
    except MissingCredentials as error:
        add("carrier credentials", False, str(error))
    if transcriber == "deepgram":
        present = bool(env.get("DEEPGRAM_API_KEY", "").strip())
        add(
            "transcriber key",
            present,
            "DEEPGRAM_API_KEY is set" if present else "DEEPGRAM_API_KEY is not set",
        )
    if trace:
        present = bool(env.get("ELEVENLABS_API_KEY", "").strip())
        add(
            "trace lookup key",
            present,
            "ELEVENLABS_API_KEY is set"
            if present
            else "ELEVENLABS_API_KEY is not set: calls would be placed but not graded",
        )

    for label, number in (("number to dial", to_number), ("caller number", from_number)):
        add(
            f"{label} is written in full",
            is_e164(number),
            number if is_e164(number) else f"{number!r} is not in +<country><number> form",
        )
    allowed = allowed_numbers(allowed_argument, env)
    if not allowed:
        add("number to dial is allowed", False, "the list of numbers this caller may dial is empty")
    else:
        listed = is_allowed(to_number, allowed)
        add(
            "number to dial is allowed",
            listed,
            f"{to_number} is on the list ({len(allowed)} number(s))"
            if listed
            else f"{to_number} is not on the list; it will not be dialled",
        )

    if credentials is not None:
        rest = TwilioRest(credentials, client=twilio_client)
        try:
            held = await rest.find_number(from_number)
        except Exception as error:  # noqa: BLE001 - reported as a failed check, with its cause
            add("caller number is on the account", False, f"could not be read: {error}")
        else:
            if held is None:
                add(
                    "caller number is on the account",
                    False,
                    f"{from_number} is not one of this account's numbers "
                    "(a caller ID that was only verified is not looked up)",
                )
            else:
                add("caller number is on the account", True, from_number)
                voice_url = str(held.get("voice_url") or "")
                points_here = bool(voice_url) and _host(voice_url) == _host(public_url)
                add(
                    "caller number's own webhook points at this caller",
                    points_here,
                    voice_url
                    if points_here
                    else f"it points at {voice_url or '(nothing)'}. Outbound calls do not use "
                    "it; it matters only if something rings this number back",
                    fatal=False,
                )
        finally:
            if twilio_client is None:
                await rest.aclose()

    exchange = PhoneExchange(host, port)
    try:
        await exchange.start()
    except OSError as error:
        add("this machine can listen", False, f"port {port} could not be opened: {error}")
        return checks
    base = public_url.rstrip("/")
    probe = f"{base}/probe/{exchange.probe_token}"
    client = probe_client or httpx.AsyncClient(timeout=15)
    try:
        try:
            answer = await client.get(probe)
            reached = answer.status_code == 200 and PhoneExchange.PROBE_REPLY in answer.text
            add(
                "public address reaches this caller",
                reached,
                base if reached else f"{base} answered {answer.status_code}, not this process",
            )
        except Exception as error:  # noqa: BLE001
            add("public address reaches this caller", False, f"{base} did not answer: {error!r}")
        parts = urlsplit(probe)
        socket_url = f"{'wss' if parts.scheme == 'https' else 'ws'}://{parts.netloc}{parts.path}"
        try:
            async with websockets.connect(socket_url, open_timeout=15) as socket:
                upgraded = await socket.recv() == PhoneExchange.PROBE_REPLY
            add(
                "public address carries a media stream",
                upgraded,
                "a websocket connects through it"
                if upgraded
                else "a websocket connected but did not reach this process",
            )
        except Exception as error:  # noqa: BLE001
            add(
                "public address carries a media stream",
                False,
                f"a websocket could not connect through it: {error!r}",
            )
    finally:
        if probe_client is None:
            await client.aclose()
        await exchange.close()
    return checks


def render_preflight(checks: list[Check]) -> tuple[str, int]:
    """The checks as text, and the exit code: 1 when any fatal check failed."""
    lines = ["phone preflight (no call is placed)"]
    for check in checks:
        mark = "ok  " if check.ok else ("FAIL" if check.fatal else "warn")
        lines.append(f"  [{mark}] {check.name}: {check.detail}")
    failed = [c for c in checks if not c.ok and c.fatal]
    lines.append(
        "ready for a first call" if not failed else f"not ready: {len(failed)} check(s) failed"
    )
    return "\n".join(lines), 1 if failed else 0
