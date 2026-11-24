"""The two ways the rig reaches the system under test: a command, or an address.

Shared by the reset (``reset.py``) and the read-back (``readback.py``). Neither
knows what the system is; the operator supplies the command or the address.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class ExternalFailed(RuntimeError):
    """The command or the address did not answer cleanly. Never holds a secret."""


def check_address(url: str) -> None:
    """Refuse an address that would send the secret header in the clear."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("the address must be an http or https URL")
    if parts.scheme == "http" and parts.hostname not in LOCAL_HOSTS:
        raise ValueError("the address must use https unless it is this machine")


async def run_command(command: str | Sequence[str], env: dict[str, str], timeout_s: float) -> str:
    """Run the operator's command and return what it printed. Exit 0 or it failed.

    A string goes through the shell; a list is run as it stands.
    """
    if not command:
        raise ExternalFailed("no command is configured")
    try:
        done = await asyncio.to_thread(
            subprocess.run,
            command,
            shell=isinstance(command, str),  # noqa: S603 - the operator's own configuration
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
