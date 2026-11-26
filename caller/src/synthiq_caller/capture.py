"""Capture a live call into a case directory, in one step.

    synthiq-caller capture --trace elevenlabs --conversation <id> --out cases/<name>/

Two read-only GETs against the platform (the conversation and its recording)
and five files in ``--out``:

* ``conversation.json``  the platform's answer, as received
* ``audio.<ext>``        the recording, the extension chosen from its bytes
* ``transcript.txt``     the platform's own transcript, one turn per line
* ``trace.json``         the call read as a normalised trace
* ``capture.json``       what was fetched, when, from where, with hashes

The audio is a recording of a real person and the transcript is a machine's
reading of it. Keep ``--out`` outside any repository. The API key is read from
the environment (``ELEVENLABS_API_KEY`` unless ``--key-env`` names another
variable); it is never printed and never written.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import subprocess
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

import httpx

from synthiq_caller.jsonio import to_text, write_json
from synthiq_caller.regions import DEFAULT_REGION, UnknownRegion, api_for, region_setting
from synthiq_caller.traces.elevenlabs import API, ElevenLabsTraceReader, parse_conversation

CAPTURE_FILE = "capture.json"
CONVERSATION_FILE = "conversation.json"
TRACE_FILE = "trace.json"
TRANSCRIPT_FILE = "transcript.txt"
AUDIO_STEM = "audio"
DEFAULT_KEY_ENV = "ELEVENLABS_API_KEY"
SUPPORTED = ("elevenlabs",)

TRANSCRIPT_HEADER = (
    "# The platform's machine transcript. It is a guess at what was said, not the truth of it.\n"
    "# Times are whole seconds from the start of the call.\n"
)


class CaptureError(Exception):
    """The capture could not be made; the message is fit to print."""


def sniff_audio(data: bytes) -> str:
    """The file extension for ``data``, from its first bytes alone. Headers are not trusted.

    An unrecognised recording is kept as ``bin`` rather than named after a guess.
    """
    head = data[:16]
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "mp3"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"fLaC":
        return "flac"
    if head[4:8] == b"ftyp":
        return "m4a"
    return "bin"


def render_transcript(conversation: dict[str, Any]) -> str:
    lines = [TRANSCRIPT_HEADER]
    for turn in conversation.get("transcript") or []:
        text = turn.get("message")
        if not text:
            continue
        at = turn.get("time_in_call_secs")
        stamp = (
            f"{int(at) // 60:02d}:{int(at) % 60:02d}" if isinstance(at, int | float) else "??:??"
        )
        lines.append(f"[{stamp}] {turn.get('role', '?')}: {text}")
    return "\n".join(lines) + "\n"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rig_version() -> str:
    try:
        return metadata.version("synthiq-voice-caller")
    except metadata.PackageNotFoundError:
        return "unknown"


def _refuse_inside_repository(out: Path) -> None:
    """A case holds a real person's voice: never write it where git could pick it up.

    Outside any work tree is fine. Inside one, only a path git itself says is
    ignored (``git check-ignore``) is allowed.
    """
    here = out.resolve()
    start = next((p for p in (here, *here.parents) if p.is_dir()), Path("/"))
    try:
        inside = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return  # no git on this machine: no work tree to be in
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return
    ignored = subprocess.run(
        ["git", "-C", str(start), "check-ignore", "-q", str(here / CAPTURE_FILE)],
        capture_output=True,
    )
    if ignored.returncode != 0:
        raise CaptureError(
            f"{out} is inside a git work tree and is not ignored by it. A capture holds a "
            "recording of a person; nothing was fetched or written. Choose a directory "
            "outside the repository, or one git ignores (cases/ under caller/ is)."
        )


def _scrub(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def _written_names() -> set[str]:
    return {CAPTURE_FILE, CONVERSATION_FILE, TRACE_FILE, TRANSCRIPT_FILE}


def _check_target(out: Path, force: bool) -> None:
    if out.exists() and not out.is_dir():
        raise CaptureError(f"{out} exists and is not a directory")
    if out.is_dir() and any(out.iterdir()):
        if not force:
            raise CaptureError(
                f"{out} already holds files; nothing was fetched or written. "
                "Use --force to replace a capture, or choose another directory."
            )
        # Only the files a capture writes are replaced; anything else stays.
        for path in out.iterdir():
            if path.name in _written_names() or path.stem == AUDIO_STEM:
                path.unlink()


async def _fetch(
    reader: ElevenLabsTraceReader, conversation_id: str
) -> tuple[dict[str, Any], bytes | None, str]:
    """The conversation, and its audio (None with the reason when it could not be had)."""
    conversation = await reader.fetch_raw(conversation_id)
    if conversation.get("has_audio") is False:
        return conversation, None, "the platform says this conversation has no audio"
    try:
        return conversation, await reader.fetch_audio(conversation_id), ""
    except httpx.HTTPStatusError as error:
        return (
            conversation,
            None,
            f"the audio request was refused: HTTP {error.response.status_code}",
        )
    except httpx.HTTPError as error:
        return conversation, None, f"the audio request failed: {type(error).__name__}"


def capture(
    conversation_id: str,
    out: Path,
    api_key: str,
    *,
    api: str = API,
    force: bool = False,
    wait_s: float = 60.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Fetch and write one case. Returns the ``capture.json`` document."""
    if not api_key:
        raise CaptureError("no API key: set the environment variable named by --key-env")
    if not conversation_id.strip() or "/" in conversation_id:
        raise CaptureError(f"not a conversation id: {conversation_id!r}")
    _refuse_inside_repository(out)
    _check_target(out, force)
    reader = ElevenLabsTraceReader(api_key, api=api, wait_s=wait_s, transport=transport)
    try:
        conversation, audio, audio_gap = asyncio.run(_fetch(reader, conversation_id))
    except httpx.HTTPStatusError as error:
        raise CaptureError(
            f"the platform refused the request: HTTP {error.response.status_code}"
        ) from None
    except (httpx.HTTPError, RuntimeError) as error:
        raise CaptureError(_scrub(f"{type(error).__name__}: {error}", api_key)) from None

    out.mkdir(parents=True, exist_ok=True)
    trace = parse_conversation(conversation, conversation_id)
    conversation_text = to_text(conversation)
    (out / CONVERSATION_FILE).write_text(conversation_text, encoding="utf-8", newline="\n")
    (out / TRANSCRIPT_FILE).write_text(
        render_transcript(conversation), encoding="utf-8", newline="\n"
    )
    write_json(out / TRACE_FILE, trace.to_json())
    audio_name = ""
    if audio is not None:
        audio_name = f"{AUDIO_STEM}.{sniff_audio(audio)}"
        (out / audio_name).write_bytes(audio)
    document: dict[str, Any] = {
        "format": "synthiq-caller-capture",
        "version": 1,
        "source": "elevenlabs",
        "conversation_id": conversation_id,
        "agent_id": conversation.get("agent_id", ""),
        "status": conversation.get("status", ""),
        "api": api,
        "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rig_version": _rig_version(),
        "platform_has_audio": conversation.get("has_audio"),
        "audio": {
            "file": audio_name,
            "captured": audio is not None,
            "reason_missing": audio_gap,
            "bytes": len(audio) if audio is not None else 0,
            "sha256": _sha256(audio) if audio is not None else "",
        },
        "conversation_sha256": _sha256(conversation_text.encode("utf-8")),
        "contains_personal_data": True,
    }
    write_json(out / CAPTURE_FILE, document)
    return document


def run(args: argparse.Namespace) -> int:
    if args.trace not in SUPPORTED:
        print(f"capture: unsupported --trace {args.trace!r} (supported: {', '.join(SUPPORTED)})")
        return 2
    key = os.environ.get(args.key_env, "")
    out = Path(args.out)
    region = region_setting(args.region)
    if args.region or region != DEFAULT_REGION:
        try:
            address = api_for(region)
        except UnknownRegion as error:
            print(f"capture: {error}")
            return 2
        if args.api not in (API, address):
            print(f"capture: --region {region} and --api name different addresses; give one")
            return 2
        args.api = address
    try:
        document = capture(
            args.conversation, out, key, api=args.api, force=args.force, wait_s=args.wait
        )
    except CaptureError as error:
        print(f"capture: {_scrub(str(error), key)}")
        return 2
    print(f"captured {document['conversation_id']} -> {out}")
    for name in (CONVERSATION_FILE, TRANSCRIPT_FILE, TRACE_FILE, CAPTURE_FILE):
        print(f"  {name}")
    audio = document["audio"]
    if audio["captured"]:
        print(f"  {audio['file']}  ({audio['bytes']} bytes)")
    else:
        print(f"  AUDIO NOT CAPTURED: {audio['reason_missing']}. `ingest` cannot cut clips.")
    print("The transcript is a machine's reading, not the truth of what was said.")
    return 0 if audio["captured"] else 1


def register(sub: Any) -> None:
    """Add the ``capture`` command to the command line's subcommands."""
    parser = sub.add_parser("capture", help="fetch a live call's audio, trace and transcript")
    parser.add_argument("--trace", required=True, help=f"platform ({', '.join(SUPPORTED)})")
    parser.add_argument("--conversation", required=True, help="the platform's conversation id")
    parser.add_argument("--out", required=True, help="case directory to write, e.g. cases/<name>/")
    parser.add_argument(
        "--api", default=API, help="API address (a regional host, if the account has one)"
    )
    parser.add_argument(
        "--region",
        default="",
        help="the account's region, instead of --api (`doctor --region` lists the addresses); "
        "default: $ELEVENLABS_REGION, else global",
    )
    parser.add_argument("--key-env", default=DEFAULT_KEY_ENV, help="variable holding the API key")
    parser.add_argument(
        "--wait", type=float, default=60.0, help="seconds to wait for a final record"
    )
    parser.add_argument("--force", action="store_true", help="replace an existing capture")
    parser.set_defaults(handler=run)
    from synthiq_caller.ingest import register as register_ingest

    register_ingest(sub)
