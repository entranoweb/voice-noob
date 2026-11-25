"""Set a new target up: ``init`` scaffolds a folder, ``doctor`` checks the machine.

    synthiq-caller init <dir> [--force]
    synthiq-caller doctor [<dir>] [--region eu] [--key-env NAME] [--fake-only] [--check-network]

Both are small on purpose; ``docs/ONBOARDING.md`` is the guide they serve.

``init`` writes a target folder that runs end to end on the fake leg with no
network: a target profile that allows no mutation, three read-only scenarios
that each state what they expect, a scripted stand-in agent for each, and a
bank of three synthetic tones standing in for recordings. The tones are not
speech and are marked ``synthetic`` in their manifest, so every result they
touch says so. Nothing in the folder names a real person, number or host.

``doctor`` reads the environment and prints a line per check. It never prints
a key, not even part of one. It touches the network only when asked
(``--check-network``), and then sends no credentials: it asks whether the
platform's address answers at all.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import shutil
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from synthiq_caller.jsonio import to_text
from synthiq_caller.legs.fake import RATE, tone
from synthiq_caller.target import TargetError, parse_target
from synthiq_caller.traces.elevenlabs import API

DEFAULT_KEY_ENV = "ELEVENLABS_API_KEY"
# Checked on 2026-11-25 against the vendor's data-residency page
# (elevenlabs.io/docs/overview/administration/data-residency) and the official Python
# SDK's environments (elevenlabs-python, src/elevenlabs/environment.py: PRODUCTION,
# PRODUCTION_US, PRODUCTION_EU, PRODUCTION_INDIA, PRODUCTION_SINGAPORE). No request was made.
# The platform's API address for each region. ``global`` is the default one. A
# residency account must use its own region's address.
REGIONS = {
    "global": API,
    "us": "https://api.us.elevenlabs.io/v1",
    "eu": "https://api.eu.residency.elevenlabs.io/v1",
    "in": "https://api.in.residency.elevenlabs.io/v1",
    "sg": "https://api.sg.residency.elevenlabs.io/v1",
}

GITIGNORE = """\
# A recording of a person never goes in a repository. Neither does a call
# captured from a platform, or the results of running against it.
cases/
runs/
clips/
clips-*/
*.wav
*.mp3
*.ogg
*.flac
*.m4a
*.stt.json
.env
"""

ENV_EXAMPLE = """\
# Names of the variables the rig reads. Set them in your shell; never put a value
# in a file that is committed. `synthiq-caller doctor` checks they are set and
# never prints them.
ELEVENLABS_API_KEY=
# Only for `synthiq-caller upload`:
SYNTHIQ_API_TOKEN=
"""

TARGET = {
    "name": "example-target",
    "notes": "Rename this. A target profile says what is true of one system and of no other.",
    "timezone": "UTC",
    "allow_mutating": "none",
}

CLIPS = {
    "opener": "A placeholder tone standing in for the caller's opening line.",
    "dob": "A placeholder tone standing in for a date of birth.",
    "name": "A placeholder tone standing in for a full name.",
}
CLIP_COLUMNS = (
    "clip", "call", "category", "secs", "said", "question", "review",
    "checked_by_human", "provenance",
)  # fmt: skip
SYNTHETIC = "synthetic: a tone, not speech (made by init)"

SCENARIOS: dict[str, dict[str, Any]] = {
    "01-asks-opening-hours": {
        "name": "Caller asks a question the agent answers from its own knowledge",
        "mutates": "none",
        "description": "Nothing is looked up or written. Replace the clip and the pattern "
        "in agent_says with your own.",
        "steps": [
            {"when": "how can i help", "clips": ["opener"], "intended": "What are your hours?"}
        ],
        "hangup_after": "goodbye",
        "max_seconds": 60,
        "expect": {"agent_says": ["open"]},
    },
    "02-lookup-by-date-of-birth": {
        "name": "Caller gives a date of birth and the agent looks the record up",
        "mutates": "none",
        "persona": {"facts": {"date_of_birth": "2000-01-01"}},
        "steps": [
            {"when": "how can i help", "clips": ["opener"], "intended": "I have a question."},
            {
                "when": "date of birth",
                "clips": ["dob"],
                "intended": "The first of January, two thousand.",
                "field": "date_of_birth",
            },
        ],
        "hangup_after": "goodbye",
        "max_seconds": 60,
        "expect": {"tools": [{"name_contains": "lookup"}], "fields": ["date_of_birth"]},
    },
    "03-lookup-by-name": {
        "name": "Caller gives a name and the agent looks the record up",
        "mutates": "none",
        "persona": {"facts": {"name": "alex example"}},
        "steps": [
            {"when": "how can i help", "clips": ["opener"], "intended": "I have a question."},
            {
                "when": "your name|full name",
                "clips": ["name"],
                "intended": "Alex Example.",
                "field": "name",
            },
        ],
        "hangup_after": "goodbye",
        "max_seconds": 60,
        "expect": {"tools": [{"name_contains": "lookup"}], "fields": ["name"]},
    },
}

LOOKUP_OK = {"success": True}


def _script(lines: list[Any], tool: dict[str, Any] | None = None) -> dict[str, Any]:
    script: dict[str, Any] = {"lines": lines}
    if tool is not None:
        script["trace"] = {"tool_calls": [tool]}
    return script


SCRIPTS = {
    "01-asks-opening-hours": _script(
        [
            {"text": "Hello, how can I help?", "speech_s": 0.3},
            {"text": "We are open nine to five. Goodbye.", "speech_s": 0.3},
        ]
    ),
    "02-lookup-by-date-of-birth": _script(
        [
            {"text": "Hello, how can I help?", "speech_s": 0.3},
            {"text": "Could I take your date of birth?", "speech_s": 0.3, "hears": "1 Jan 2000"},
            {"text": "Thank you, goodbye.", "speech_s": 0.3},
        ],
        {
            "name": "record_lookup",
            "arguments": {"dateOfBirth": "2000-01-01"},
            "result": LOOKUP_OK,
            "ok": True,
        },
    ),
    "03-lookup-by-name": _script(
        [
            {"text": "Hello, how can I help?", "speech_s": 0.3},
            {"text": "Could I take your full name?", "speech_s": 0.3, "hears": "alex example"},
            {"text": "Thank you, goodbye.", "speech_s": 0.3},
        ],
        {
            "name": "record_lookup",
            "arguments": {"fullName": "alex example"},
            "result": LOOKUP_OK,
            "ok": True,
        },
    ),
}


class InitError(Exception):
    """The folder could not be scaffolded; the message is fit to print."""


def _wav(seconds: float) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(tone(seconds))
    return buffer.getvalue()


def _manifest() -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CLIP_COLUMNS)
    for clip, said in CLIPS.items():
        writer.writerow([clip, "", "placeholder", "0.40", said, "", "", "", SYNTHETIC])
    return out.getvalue()


def files_to_write() -> dict[str, bytes]:
    """Every file ``init`` writes, by path under the folder."""
    files: dict[str, bytes] = {
        ".gitignore": GITIGNORE.encode(),
        "env.example": ENV_EXAMPLE.encode(),
        "target.json": to_text(TARGET).encode(),
        "clips/manifest.csv": _manifest().encode(),
    }
    for clip in CLIPS:
        files[f"clips/{clip}.wav"] = _wav(0.4)
    for name, scenario in SCENARIOS.items():
        files[f"scenarios/{name}.json"] = to_text(scenario).encode()
        files[f"agent-scripts/{name}.json"] = to_text(SCRIPTS[name]).encode()
    return files


def init(directory: Path, *, force: bool = False) -> list[str]:
    """Write the target folder. Refuses, writing nothing, if a file would be replaced."""
    if directory.exists() and not directory.is_dir():
        raise InitError(f"{directory} exists and is not a directory")
    files = files_to_write()
    clash = sorted(name for name in files if (directory / name).exists())
    if clash and not force:
        raise InitError(
            f"{directory} already holds {', '.join(clash)}; nothing was written. "
            "Use --force to replace those files, or choose another directory."
        )
    for name, data in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return sorted(files)


def run_init(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    try:
        written = init(directory, force=args.force)
    except InitError as error:
        print(f"init: {error}")
        return 2
    print(f"wrote {len(written)} files to {directory}")
    for name in written:
        print(f"  {name}")
    print(
        "\nNext, from that folder (docs/ONBOARDING.md has the whole path):\n"
        "  synthiq-caller doctor . --fake-only\n"
        "  synthiq-caller validate scenarios --clips clips --target target.json\n"
        "  synthiq-caller batch scenarios --leg fake --fake-agent agent-scripts "
        "--clips clips --target target.json --out runs --settle 0\n"
        "\nThe clips are tones, not speech, and the agent is a script: that run says "
        "nothing about any real agent."
    )
    return 0


@dataclass
class Check:
    name: str
    status: str  # "ok", "FAIL", "warn" or "skipped"
    detail: str

    def line(self) -> str:
        return f"{self.status:<8} {self.name:<16} {self.detail}"


def _ignored(gitignore: Path, entry: str) -> bool:
    if not gitignore.is_file():
        return False
    lines = {ln.strip() for ln in gitignore.read_text(encoding="utf-8").splitlines()}
    return entry in lines or entry.rstrip("/") in lines


def _check_folder(directory: Path) -> list[Check]:
    checks: list[Check] = []
    target = directory / "target.json"
    if not target.is_file():
        checks.append(Check("target.json", "FAIL", f"not found in {directory} (run `init`?)"))
    else:
        try:
            import json

            parse_target(json.loads(target.read_text(encoding="utf-8")))
            checks.append(Check("target.json", "ok", "parses as a target profile"))
        except (TargetError, ValueError) as error:
            checks.append(Check("target.json", "FAIL", str(error)))
    scenarios = directory / "scenarios"
    count = len(list(scenarios.glob("*.json"))) if scenarios.is_dir() else 0
    checks.append(
        Check("scenarios", "ok" if count else "FAIL", f"{count} file(s) in {scenarios.name}/")
    )
    gitignore = directory / ".gitignore"
    missing = [e for e in ("cases/", "clips/") if not _ignored(gitignore, e)]
    checks.append(
        Check(
            ".gitignore",
            "FAIL" if missing else "ok",
            f"does not ignore {', '.join(missing)}: recordings could be committed"
            if missing
            else "ignores cases/ and clips/",
        )
    )
    return checks


def _check_network(api: str, transport: httpx.BaseTransport | None) -> Check:
    """Does the platform's address answer? No credentials are sent; any answer counts."""
    try:
        with httpx.Client(timeout=10.0, transport=transport) as client:
            response = client.get(api)
    except httpx.HTTPError as error:
        return Check("network", "FAIL", f"{api} did not answer: {type(error).__name__}")
    return Check("network", "ok", f"{api} answered (HTTP {response.status_code}); no key was sent")


def doctor(
    *,
    directory: Path | None = None,
    region: str = "global",
    key_env: str = DEFAULT_KEY_ENV,
    fake_only: bool = False,
    check_network: bool = False,
    environ: dict[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[Check]:
    env = os.environ if environ is None else environ
    checks: list[Check] = []
    if shutil.which("ffmpeg"):
        checks.append(Check("ffmpeg", "ok", "found on PATH"))
    else:
        needed = "needed to cut clips from a captured call (`ingest`)"
        checks.append(Check("ffmpeg", "warn" if fake_only else "FAIL", f"not on PATH; {needed}"))
    if fake_only:
        checks.append(Check("key", "skipped", "the fake leg needs no key"))
    elif env.get(key_env):
        checks.append(Check("key", "ok", f"{key_env} is set (value not shown)"))
    else:
        checks.append(Check("key", "FAIL", f"{key_env} is not set in the environment"))
    api = REGIONS.get(region)
    if api is None:
        checks.append(
            Check("region", "FAIL", f"unknown region {region!r}; one of {sorted(REGIONS)}")
        )
    else:
        checks.append(Check("region", "ok", f"{region}: {api}"))
    if directory is not None:
        checks.extend(_check_folder(directory))
    if not check_network or fake_only:
        checks.append(Check("network", "skipped", "not asked (pass --check-network)"))
    elif api is not None:
        checks.append(_check_network(api, transport))
    return checks


def run_doctor(args: argparse.Namespace) -> int:
    checks = doctor(
        directory=Path(args.directory) if args.directory else None,
        region=args.region,
        key_env=args.key_env,
        fake_only=args.fake_only,
        check_network=args.check_network,
    )
    for check in checks:
        print(check.line())
    failed = [c for c in checks if c.status == "FAIL"]
    if failed:
        print(f"\n{len(failed)} check(s) failed.")
        return 1
    print("\nNothing failed. This says the machine is set up, not that any agent works.")
    return 0


def register(sub: Any) -> None:
    """Add ``init`` and ``doctor`` to the command line's subcommands."""
    ini = sub.add_parser("init", help="scaffold a target folder that runs on the fake leg")
    ini.add_argument("directory")
    ini.add_argument("--force", action="store_true", help="replace the files init writes")
    ini.set_defaults(handler=run_init)
    doc = sub.add_parser("doctor", help="check this machine and, if given, a target folder")
    doc.add_argument("directory", nargs="?", default="", help="a folder made by `init`")
    doc.add_argument("--region", default="global", help=f"one of {', '.join(REGIONS)}")
    doc.add_argument("--key-env", default=DEFAULT_KEY_ENV, help="variable holding the API key")
    doc.add_argument("--fake-only", action="store_true", help="skip what only a real target needs")
    doc.add_argument(
        "--check-network",
        action="store_true",
        help="ask whether the platform's address answers (sends no key); off by default",
    )
    doc.set_defaults(handler=run_doctor)
