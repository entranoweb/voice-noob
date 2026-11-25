"""The two ways the rig reaches the system under test: a command, or an address.

Shared by the reset (``reset.py``), the read-back (``readback.py``), the
prerequisite checker (``prereq.py``) and the batch's hooks. None of them knows
what the system is; the operator supplies the command or the address.

A command is never handed to a shell. The call's reference comes from the
platform under test, and a chain can carry a value the agent spoke into a later
call's facts; a shell would parse either of them as part of the command line
(``cmd.exe`` substitutes ``%NAME%`` before it reads the line). So a command is
split into a program and its arguments once, here, and what the call supplies
reaches the program only as separate arguments or in its environment. For a
pipe or a redirect, put it in a script and name the script.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

# What a call reference, a run id or any other id may be made of before it is
# given to a command, as an argument or in the environment.
SAFE_ID = re.compile(r"[A-Za-z0-9._:\-]{1,200}")
# Characters ``cmd.exe`` gives a meaning to. A batch file is run by ``cmd.exe``
# whatever started it, and it expands ``%NAME%`` itself.
_CMD_SPECIAL = re.compile(r'[&|<>^%"!\r\n]')
_BATCH = (".bat", ".cmd")


class ExternalFailed(RuntimeError):
    """The command or the address did not answer cleanly. Never holds a secret."""


def check_address(url: str) -> None:
    """Refuse an address that would send the secret header in the clear."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("the address must be an http or https URL")
    if parts.scheme == "http" and parts.hostname not in LOCAL_HOSTS:
        raise ValueError("the address must use https unless it is this machine")


def safe_id(value: str) -> bool:
    """Whether an id from outside is fit to hand to a command."""
    return bool(SAFE_ID.fullmatch(value))


def split_command(command: str | Sequence[str]) -> list[str]:
    """A command as a program and its arguments. A list is taken as it stands.

    A string is split on spaces outside quotes, the way a shell would, and then
    nothing else a shell does is done to it: no variables, no pipes, no ``&&``.
    """
    if not isinstance(command, str):
        return [str(part) for part in command]
    if os.name == "nt":
        # Backslashes are path separators here, not escapes.
        parts = shlex.split(command, posix=False)
        return [
            part[1:-1] if len(part) >= 2 and part[0] == part[-1] and part[0] in "\"'" else part
            for part in parts
        ]
    return shlex.split(command)


def _resolved(argv: list[str]) -> list[str]:
    """``argv`` with its program found on the path (``npm`` is ``npm.cmd`` on Windows)."""
    found = shutil.which(argv[0])
    return [found, *argv[1:]] if found else argv


def run_blocking(
    command: str | Sequence[str], env: dict[str, str], timeout_s: float
) -> subprocess.CompletedProcess[str]:
    """Run the operator's command without a shell and return how it ended.

    Raises ``ExternalFailed`` when it cannot be run safely or at all; the exit
    code is the caller's to read.
    """
    if not command:
        raise ExternalFailed("no command is configured")
    try:
        argv = split_command(command)
    except ValueError as error:
        raise ExternalFailed(f"command could not be read: {error}") from error
    if not argv:
        raise ExternalFailed("no command is configured")
    argv = _resolved(argv)
    if os.name == "nt" and argv[0].lower().endswith(_BATCH):
        # A batch file is read by cmd.exe, which would parse these as commands.
        unsafe = sorted(
            name
            for name, value in env.items()
            if name.startswith("SYNTHIQ_") and _CMD_SPECIAL.search(value)
        )
        unsafe += [f"argument {i}" for i, a in enumerate(argv[1:], 1) if _CMD_SPECIAL.search(a)]
        if unsafe:
            raise ExternalFailed(
                f"command is a batch file and {unsafe} hold characters cmd.exe would run; "
                "call a program, not a batch file"
            )
    try:
        return subprocess.run(  # noqa: S603 - a list, never a shell; see the module docstring
            argv,
            shell=False,
            env={**os.environ, **env},
            timeout=timeout_s,
            capture_output=True,
            text=True,
            # Not the platform's legacy code page: a "£" or a typographic
            # apostrophe printed as UTF-8 must come back as itself.
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as error:
        raise ExternalFailed(f"command ran past {timeout_s:.0f}s") from error
    except OSError as error:
        raise ExternalFailed(f"command could not start: {error}") from error


async def run_command(command: str | Sequence[str], env: dict[str, str], timeout_s: float) -> str:
    """Run the operator's command and return what it printed. Exit 0 or it failed."""
    done = await asyncio.to_thread(run_blocking, command, env, timeout_s)
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip()[-200:]
        raise ExternalFailed(f"command exited {done.returncode}: {tail}")
    return done.stdout


async def call_address(
    url: str,
    body: dict[str, Any],
    *,
    secret: str = "",
    header: str = "X-Synthiq-Secret",
    method: str = "POST",
    timeout_s: float = 60.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.Response:
    """Send ``body`` as JSON with the secret header. Any answer but 2xx is a failure."""
    headers = {header: secret} if secret else {}
    try:
        async with httpx.AsyncClient(timeout=timeout_s, transport=transport) as client:
            response = await client.request(method, url, headers=headers, json=body)
    except httpx.TimeoutException as error:
        raise ExternalFailed(f"call ran past {timeout_s:.0f}s") from error
    except httpx.HTTPError as error:
        raise ExternalFailed(f"call failed: {type(error).__name__}") from error
    if not response.is_success:
        raise ExternalFailed(f"call answered {response.status_code}")
    return response
