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
from synthiq_caller.regions import REGIONS, UnknownRegion, api_for, region_setting
from synthiq_caller.scenario import parse_scenario, read_scenario_file
from synthiq_caller.target import TargetError, parse_target

DEFAULT_KEY_ENV = "ELEVENLABS_API_KEY"
# The region-to-address mapping lives in ``regions.py``, the one place it is kept.

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
    # "fake": what a run on the fake leg needs. "live": what only a call to a real
    # platform needs (a key, a region, an address that answers, ffmpeg for ``ingest``).
    needed_by: str = "fake"

    def line(self) -> str:
        tag = "" if self.needed_by == "fake" else "  [live leg]"
        return f"{self.status:<8} {self.name:<16} {self.detail}{tag}"


LIVE_NEEDS = ("ffmpeg", "key", "region")


def _ignored(gitignore: Path, entry: str) -> bool:
    if not gitignore.is_file():
        return False
    lines = {ln.strip() for ln in gitignore.read_text(encoding="utf-8").splitlines()}
    return entry in lines or entry.rstrip("/") in lines


def _check_scenarios(scenarios: Path) -> Check:
    """Each ``*.json`` is read with the loader a run uses: a file that only holds ``{`` fails."""
    files = sorted(scenarios.glob("*.json")) if scenarios.is_dir() else []
    if not files:
        return Check("scenarios", "FAIL", f"0 file(s) in {scenarios.name}/")
    bad: list[str] = []
    for path in files:
        try:
            data = read_scenario_file(path)
            if "chain" not in data:
                parse_scenario(data, None, path.stem)
        except Exception as error:  # noqa: BLE001 - any reason the loader refuses it
            bad.append(
                f"{path.name}: {str(error).splitlines()[0] if str(error) else type(error).__name__}"
            )
    if bad:
        return Check(
            "scenarios", "FAIL", f"{len(bad)} of {len(files)} do not load: " + "; ".join(bad)
        )
    return Check("scenarios", "ok", f"{len(files)} file(s) in {scenarios.name}/, each loads")


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
    checks.append(_check_scenarios(directory / "scenarios"))
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
        return Check("network", "FAIL", f"{api} did not answer: {type(error).__name__}", "live")
    return Check(
        "network", "ok", f"{api} answered (HTTP {response.status_code}); no key was sent", "live"
    )


def doctor(
    *,
    directory: Path | None = None,
    region: str = "",
    key_env: str = DEFAULT_KEY_ENV,
    fake_only: bool = False,
    check_network: bool = False,
    environ: dict[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[Check]:
    """One line per check, each tagged with the leg that needs it.

    A missing key, an unknown region, an address that does not answer and a missing ffmpeg
    stop only a live leg, so they are tagged ``live`` and ``verdicts`` does not count them
    against the fake leg. ``fake_only`` leaves them out of the verdict by not making them;
    it never turns one into a pass.
    """
    env = os.environ if environ is None else environ
    checks: list[Check] = []
    if shutil.which("ffmpeg"):
        checks.append(Check("ffmpeg", "ok", "found on PATH", "live"))
    else:
        needed = "needed to cut clips from a captured call (`ingest`)"
        status = "warn" if fake_only else "FAIL"
        checks.append(Check("ffmpeg", status, f"not on PATH; {needed}", "live"))
    if fake_only:
        checks.append(Check("key", "skipped", "the fake leg needs no key", "live"))
    elif env.get(key_env):
        checks.append(Check("key", "ok", f"{key_env} is set (value not shown)", "live"))
    else:
        checks.append(Check("key", "FAIL", f"{key_env} is not set in the environment", "live"))
    api: str | None
    chosen = region_setting(region, env)  # the flag, else ELEVENLABS_REGION, else global
    try:
        api = api_for(chosen)
    except UnknownRegion as error:
        api = None
        checks.append(Check("region", "FAIL", str(error)))  # a mistake in the setting
    else:
        checks.append(Check("region", "ok", f"{chosen}: {api}", "live"))
    if directory is not None:
        checks.extend(_check_folder(directory))
    if not check_network or fake_only:
        checks.append(Check("network", "skipped", "not asked (pass --check-network)", "live"))
    elif api is not None:
        checks.append(_check_network(api, transport))
    return checks


def verdicts(checks: list[Check], *, fake_only: bool) -> tuple[bool, str, bool]:
    """(fake leg ready, the live leg's one-line state, live leg ready).

    The live leg is ready only when the key, the region and ffmpeg were each checked and
    passed, and nothing else it needs failed. A check that was skipped or never made
    leaves it not ready (or "not checked" under ``--fake-only``); it is never reported
    ready on the strength of a check nobody ran. Even ready, that is a statement about
    this machine's settings: only a call shows the platform accepts the key.
    """
    fake_ready = not any(c.status == "FAIL" and c.needed_by == "fake" for c in checks)
    if fake_only:
        return fake_ready, "not checked (--fake-only)", False
    live = {c.name: c for c in checks if c.needed_by == "live"}
    problems = [name for name, c in live.items() if c.status == "FAIL"]
    problems += [name for name in LIVE_NEEDS if name not in live or live[name].status != "ok"]
    if problems:
        return fake_ready, "NOT READY (" + ", ".join(dict.fromkeys(problems)) + ")", False
    network = live.get("network")
    note = "" if network is not None and network.status == "ok" else "; the address was not tried"
    return fake_ready, "settings present, nothing was called" + note, True


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
    fake_ready, live_state, live_ready = verdicts(checks, fake_only=args.fake_only)
    if args.directory:
        print(f"\nfake leg: {'ready' if fake_ready else 'NOT READY'}")
    elif fake_ready:
        print("\nfake leg: not checked (no folder given; pass the folder `init` made)")
    else:
        print("\nfake leg: NOT READY")
    print(f"live leg: {live_state}")
    if not fake_ready:
        failed = [c for c in checks if c.status == "FAIL" and c.needed_by == "fake"]
        print(f"\n{len(failed)} check(s) failed.")
        return 1
    if args.live and not live_ready:
        print("\n--live was given and the live leg is not ready.")
        return 1
    print("\nThis says the machine is set up, not that any agent works.")
    return 0


def register(sub: Any) -> None:
    """Add ``init`` and ``doctor`` to the command line's subcommands."""
    ini = sub.add_parser("init", help="scaffold a target folder that runs on the fake leg")
    ini.add_argument("directory")
    ini.add_argument("--force", action="store_true", help="replace the files init writes")
    ini.set_defaults(handler=run_init)
    doc = sub.add_parser("doctor", help="check this machine and, if given, a target folder")
    doc.add_argument("directory", nargs="?", default="", help="a folder made by `init`")
    doc.add_argument(
        "--region",
        default="",
        help=f"one of {', '.join(REGIONS)}; default: ELEVENLABS_REGION, else global",
    )
    doc.add_argument("--key-env", default=DEFAULT_KEY_ENV, help="variable holding the API key")
    doc.add_argument("--fake-only", action="store_true", help="skip what only a real target needs")
    doc.add_argument(
        "--live",
        action="store_true",
        help="exit 1 unless the live leg is ready too (a missing key then fails)",
    )
    doc.add_argument(
        "--check-network",
        action="store_true",
        help="ask whether the platform's address answers (sends no key); off by default",
    )
    doc.set_defaults(handler=run_doctor)
