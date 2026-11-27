"""One place where a command's known failures become a message and exit 2.

Exit status of the commands: 0 passed, 1 a gate or threshold was not met (a
regression, an incomplete batch, a failed check), 2 the command could not run
(a file could not be read, a scenario was invalid, the network or the agent's
trace was unavailable, bad arguments). 2 is never a pass and never a gate
result: a pipeline that tests only for 0 fails on it, and one that tells a
regression (1) from "did not run" (2) can.

``SYNTHIQ_DEBUG=1`` lets the original exception through, traceback and all.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable

import httpx

from synthiq_caller.external import ExternalFailed
from synthiq_caller.legs.twilio_media import CallNotConnected
from synthiq_caller.manifestcsv import ManifestError
from synthiq_caller.readback import ReadBackFailed
from synthiq_caller.reset import ResetFailed
from synthiq_caller.scenario import ScenarioError
from synthiq_caller.target import TargetError
from synthiq_caller.traces.common import TraceUnavailableError
from synthiq_caller.traces.elevenlabs import TraceNotFinal
from synthiq_caller.traces.lookup import TraceNotFound
from synthiq_caller.twilio_rest import MissingCredentials
from synthiq_caller.zones import NoZoneDatabase

# KeyboardInterrupt and SystemExit are BaseException, not listed, never caught.
KNOWN_ERRORS: tuple[type[BaseException], ...] = (
    ScenarioError,
    ManifestError,
    TargetError,
    ExternalFailed,
    ReadBackFailed,
    ResetFailed,
    CallNotConnected,
    TraceUnavailableError,
    TraceNotFinal,
    TraceNotFound,
    MissingCredentials,
    NoZoneDatabase,
    ValueError,
    OSError,
    httpx.HTTPError,
)
EXIT_COULD_NOT_RUN = 2


def guarded(run: Callable[[], int], *, command: str = "synthiq-caller") -> int:
    """Run ``run``; a known failure prints ``ERROR: <message>`` to stderr and returns 2."""
    try:
        return run()
    except KNOWN_ERRORS as error:
        if os.environ.get("SYNTHIQ_DEBUG") == "1":
            raise
        message = str(error) or type(error).__name__
        print(f"ERROR: {message}", file=sys.stderr)
        print(
            f"{command}: not run to the end (exit 2, which is neither a pass nor a gate "
            "result). Set SYNTHIQ_DEBUG=1 for the traceback.",
            file=sys.stderr,
        )
        return EXIT_COULD_NOT_RUN
