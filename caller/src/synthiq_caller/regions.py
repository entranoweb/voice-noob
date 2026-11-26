"""The one place an ElevenLabs region is turned into an API address.

Every command that talks to the platform (``capture``, ``run``, ``batch``, ``regrade``,
the phone leg's trace lookup, ``doctor``) takes the region from the same setting and maps
it here: ``--region <name>``, else the ``ELEVENLABS_REGION`` environment variable, else
``global``. An account on a residency region is a separate account: "you will need to hit
a different API URL with a different API key" (the vendor's data-residency page). The key
is read as before, from the variable that holds it; use the region's own key there.

A name that is not in ``REGIONS`` is an error, never a quiet fall back to the global host:
a request to the wrong host sends the key to a service that is not the account's.

Where each address comes from (checked 2026-11-25, no request made):

* ``eu``, ``in``, ``sg``: the vendor's data-residency page lists them.
* ``us``: that page lists no address. It comes from the vendor's official Python SDK
  (``elevenlabs-python``, ``src/elevenlabs/environment.py``: ``PRODUCTION_US``). It is not
  confirmed by the documentation.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping

REGION_ENV = "ELEVENLABS_REGION"
DEFAULT_REGION = "global"
GLOBAL_API = "https://api.elevenlabs.io/v1"

REGIONS = {
    "global": GLOBAL_API,
    "us": "https://api.us.elevenlabs.io/v1",
    "eu": "https://api.eu.residency.elevenlabs.io/v1",
    "in": "https://api.in.residency.elevenlabs.io/v1",
    "sg": "https://api.sg.residency.elevenlabs.io/v1",
}


class UnknownRegion(ValueError):
    """The region named is not one this tool knows an address for."""


def api_for(region: str) -> str:
    """The API address for ``region``. Raises ``UnknownRegion`` for any other name."""
    name = (region or DEFAULT_REGION).strip().lower()
    try:
        return REGIONS[name]
    except KeyError:
        raise UnknownRegion(f"unknown region {region!r}; one of {', '.join(REGIONS)}") from None


def region_setting(flag: str | None = "", env: Mapping[str, str] | None = None) -> str:
    """The region asked for: the flag, else ``ELEVENLABS_REGION``, else ``global``."""
    held = os.environ if env is None else env
    return (flag or "").strip() or held.get(REGION_ENV, "").strip() or DEFAULT_REGION


def api_from_args(args: argparse.Namespace, env: Mapping[str, str] | None = None) -> str:
    """The API address a command's ``--region`` (or the environment) selects.

    Exits with the message, as a command-line mistake does, when the name is unknown.
    """
    try:
        return api_for(region_setting(getattr(args, "region", ""), env))
    except UnknownRegion as error:
        raise SystemExit(str(error)) from error


def add_region_argument(parser: argparse.ArgumentParser) -> None:
    """The ``--region`` flag every command that reaches the platform takes."""
    parser.add_argument(
        "--region",
        default="",
        help=f"the account's ElevenLabs region ({', '.join(REGIONS)}); default: "
        f"${REGION_ENV}, else {DEFAULT_REGION}. A residency region needs its own API key",
    )
