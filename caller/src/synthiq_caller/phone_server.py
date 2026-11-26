"""Command line for one scenario over a real telephone call.

    synthiq-caller-phone --public-url https://caller.example.com \\
        --from +441234567890 --to +441234567891 \\
        --scenario scenario.json --clips <dir> --out runs/

Twilio credentials come from the environment (``TWILIO_ACCOUNT_SID``,
``TWILIO_AUTH_TOKEN``, optionally ``TWILIO_API_KEY_SID`` and
``TWILIO_API_KEY_SECRET``) and are never printed or written to the output.

``--dry-run`` places no call and needs no credentials: an in-process stand-in
for Twilio connects to the same media endpoint, and the frames that would have
gone to the carrier are counted and printed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from synthiq_caller.batch import ALLOWED
from synthiq_caller.clips import ClipBank
from synthiq_caller.director import Director, RunRecord
from synthiq_caller.jsonio import next_index, write_new_json
from synthiq_caller.legs.fake_twilio import FakeTwilio
from synthiq_caller.legs.twilio_media import CallNotConnected, PhoneConfig, TwilioMediaLeg
from synthiq_caller.manifestcsv import ManifestError
from synthiq_caller.numbers import ALLOWED_ENV, allowed_numbers, is_allowed
from synthiq_caller.scenario import Scenario, load_scenario
from synthiq_caller.transcribe import (
    DeepgramTranscriber,
    NullTranscriber,
    ScriptedTranscriber,
    Transcriber,
)
from synthiq_caller.turns import TurnSettings
from synthiq_caller.twilio_rest import Credentials, MissingCredentials, TwilioRest

# On a phone line the agent's text arrives after it has been spoken (it comes
# from a transcriber), so there is no audio to wait for once the text is in.
PHONE_TURNS = TurnSettings(audio_start_wait_s=0.8)


class UsageError(SystemExit):
    def __init__(self, message: str) -> None:
        print(f"synthiq-caller-phone: {message}", file=sys.stderr)
        super().__init__(2)


def open_bank(args: argparse.Namespace, scenario: Scenario) -> ClipBank:
    """The clip bank for a call, refused up front if the scenario names a clip it may not play.

    The bank enforces the same rule at playback, so a call cannot reach a clip any
    other way. Checking here stops a real call being placed that would fail half way.
    """
    try:
        bank = ClipBank(
            args.clips,
            extra=tuple(args.clips_extra),
            allow_unknown_provenance=args.allow_unknown_provenance,
            allow_missing_consent=args.allow_missing_consent,
        )
    except ManifestError as error:
        raise UsageError(f"clips: {error}") from error
    played = [clip for step in scenario.steps for clip in step.clips]
    if scenario.fallback is not None:
        played += list(scenario.fallback.clips)
    refused = [r for r in (bank.refusal(c) for c in dict.fromkeys(played)) if r]
    if refused:
        raise UsageError(f"the call was not placed; clips not allowed: {refused[:3]}")
    return bank


def check_public_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment:
        raise UsageError(
            "--public-url must be the https address Twilio can reach this server on, "
            "for example https://caller.example.com"
        )
    return url.rstrip("/")


def agent_lines_for_dry_run(scenario: Scenario, path: str | None) -> list[str]:
    """Agent lines for the stand-in: a file if given, else the scenario's own patterns.

    A plain pattern such as ``date of birth`` matches itself, which is enough to
    walk the scenario's steps in order and then reach its hang-up line.
    """
    if path:
        lines = [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines()]
        return [line for line in lines if line]
    lines = [step.when.pattern for step in scenario.steps]
    if scenario.hangup_after is not None:
        lines.append(scenario.hangup_after.pattern)
    return lines


def build_transcriber(args: argparse.Namespace, env: Mapping[str, str]) -> Transcriber:
    if args.transcriber == "deepgram":
        key = env.get("DEEPGRAM_API_KEY", "").strip()
        if not key:
            raise UsageError("--transcriber deepgram needs DEEPGRAM_API_KEY in the environment")
        return DeepgramTranscriber(key, language=args.language)
    if args.transcriber == "scripted":
        if not args.agent_lines:
            raise UsageError("--transcriber scripted needs --agent-lines <file>")
        lines = Path(args.agent_lines).read_text(encoding="utf-8").splitlines()
        return ScriptedTranscriber([line.strip() for line in lines if line.strip()])
    return NullTranscriber()


def _write(out: Path, payload: dict[str, Any]) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    # One more than the highest number in use: a stored run is never written over.
    index = next_index(out, "phone-run") + 1
    path = out / f"phone-run-{index:03d}.json"
    write_new_json(path, payload)
    return path


def _summary(record: RunRecord, leg: TwilioMediaLeg) -> list[str]:
    lines = [
        f"call: {record.call_ref or '-'}   ended: {record.ended}   {record.duration_s}s",
        f"statuses: {', '.join(leg.log.statuses) or '-'}",
        f"frames sent: {leg.log.frames_sent} ({leg.log.frames_sent * 20} ms of audio), "
        f"silent frames not sent: {leg.log.frames_skipped_silent}",
    ]
    for said in record.said:
        what = f"keys {said.dtmf}" if said.dtmf else ", ".join(said.clips) or "(silent)"
        lines.append(f"  {said.t:6.1f}s  {what}  ({said.seconds}s)  <- {said.answering[-60:]!r}")
    return lines


async def run_real(args: argparse.Namespace, scenario: Scenario, env: Mapping[str, str]) -> int:
    credentials = Credentials.from_env(env)
    allowed = allowed_numbers(args.allowed_numbers, env)
    if not is_allowed(args.to, allowed):
        raise UsageError(
            f"{args.to} is not on the list of numbers this caller may dial. List them with "
            f"--allowed-numbers (a file, or a comma-separated list) or {ALLOWED_ENV}"
        )
    # The same guard a batch applies: a call that can change the system under
    # test is placed only when the operator has said this level of change is allowed.
    if scenario.mutates not in ALLOWED[args.allow_mutating]:
        raise UsageError(
            f"mutation guard: this scenario's 'mutates' is {scenario.mutates!r} and "
            f"--allow-mutating is {args.allow_mutating!r}; the call was not placed"
        )
    bank = open_bank(args, scenario)
    public_url = check_public_url(args.public_url or "")
    transcriber = build_transcriber(args, env)
    rest = TwilioRest(credentials)
    leg = TwilioMediaLeg(
        PhoneConfig(
            to_number=args.to,
            from_number=args.from_number,
            public_url=public_url,
            listen_host=args.host,
            listen_port=args.port,
            record=not args.no_record,
            allowed_to=allowed,
        ),
        rest,
        auth_token=credentials.auth_token,
        transcriber=transcriber,
    )
    try:
        record = await Director(scenario, leg, bank, turn_settings=PHONE_TURNS).run()
        recordings: list[dict[str, Any]] = []
        if record.call_ref and not args.no_record:
            try:
                recordings = await rest.list_recordings(record.call_ref)
            except Exception as error:  # noqa: BLE001 - a missing recording must not lose the run
                recordings = [{"error": repr(error)}]
    except CallNotConnected as error:
        print(f"the call did not connect: {error}", file=sys.stderr)
        return 1
    finally:
        await rest.aclose()
    path = _write(
        Path(args.out),
        {"record": record.to_json(), "phone": asdict(leg.log), "recordings": recordings},
    )
    print("\n".join(_summary(record, leg)))
    print(f"written: {path}")
    return 0


async def run_dry(args: argparse.Namespace, scenario: Scenario) -> int:
    lines = agent_lines_for_dry_run(scenario, args.agent_lines)
    # Patient enough that a pause inside a recorded answer is not taken for its end.
    fake = FakeTwilio(agent_lines=[0.6] * len(lines), caller_quiet_s=1.0, patience_s=8.0)
    leg = TwilioMediaLeg(
        PhoneConfig(
            to_number=args.to or "+440000000000",
            from_number=args.from_number or "+440000000001",
            listen_host="127.0.0.1",
            listen_port=0,
            record=not args.no_record,
        ),
        fake,
        auth_token="dry-run",  # noqa: S106 - no request is signed in a dry run
        transcriber=ScriptedTranscriber(lines),
    )
    port = await leg.start_server()
    leg.public_url = f"http://127.0.0.1:{port}"
    bank = open_bank(args, scenario)
    record = await Director(scenario, leg, bank, turn_settings=PHONE_TURNS).run()
    await fake.wait_finished()
    print("DRY RUN: no call was placed and nothing was sent to Twilio.")
    request = fake.created[0]
    print(f"would dial {request['to']} from {request['from']}, record={request['record']}")
    print(f"TwiML: {request['twiml'].replace(leg.stream_url(), '<stream url>')}")
    print("\n".join(_summary(record, leg)))
    # Natural pauses inside one answer are not separate utterances.
    speech = fake.caller_speech(merge_gap_s=0.5)
    print(f"caller audio the far end would hear: {len(speech)} utterance(s)")
    for start, end in speech:
        print(f"  {start:6.2f}s to {end:6.2f}s")
    if args.out:
        path = _write(
            Path(args.out),
            {"dry_run": True, "record": record.to_json(), "phone": asdict(leg.log)},
        )
        print(f"written: {path}")
    return 0


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="synthiq-caller-phone")
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--clips", required=True)
    parser.add_argument("--clips-extra", action="append", default=[], help="a further clip bank")
    parser.add_argument(
        "--allow-unknown-provenance",
        action="store_true",
        help="play clips whose manifest does not say if they are real or synthetic",
    )
    parser.add_argument(
        "--allow-missing-consent",
        action="store_true",
        help="play real clips with no lawful basis or consent record (special-category data)",
    )
    parser.add_argument("--out", default="")
    parser.add_argument("--public-url", default="")
    parser.add_argument("--from", dest="from_number", default="")
    parser.add_argument("--to", default="")
    parser.add_argument(
        "--allowed-numbers",
        default="",
        help="the only numbers that may be dialled: a file, one per line, or a comma-separated "
        "list. A real call is not placed without it",
    )
    parser.add_argument(
        "--allow-mutating",
        choices=sorted(ALLOWED),
        default="none",
        help="how much a real call may change on the system under test; a scenario that "
        "changes more, or does not say, is not placed",
    )
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - the carrier connects in
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--transcriber", choices=["none", "scripted", "deepgram"], default="deepgram"
    )
    parser.add_argument("--agent-lines", default=None, help="one agent line per row")
    parser.add_argument("--language", default="en-GB")
    parser.add_argument("--no-record", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    environment = os.environ if env is None else env

    scenario = load_scenario(args.scenario)
    if args.dry_run:
        return asyncio.run(run_dry(args, scenario))

    missing = [
        flag
        for flag, value in (("--from", args.from_number), ("--to", args.to), ("--out", args.out))
        if not value
    ]
    if missing:
        raise UsageError(f"missing {', '.join(missing)}")
    try:
        return asyncio.run(run_real(args, scenario, environment))
    except MissingCredentials as error:
        raise UsageError(str(error)) from error


if __name__ == "__main__":
    sys.exit(main())
