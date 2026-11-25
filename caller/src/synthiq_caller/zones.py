"""Time zones, with an error a person can act on when the machine has none.

Python reads time zones from the operating system's database. Windows has no
such database, so there the ``tzdata`` package must be installed (this package
depends on it on Windows). Without it every zone lookup fails with an error
that names the zone and not the cause, and a booking time cannot be graded.
"""

from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones


class NoZoneDatabase(RuntimeError):
    """This machine has no time zone database at all."""


class UnknownZone(ValueError):
    """The database is there and has no zone of this name."""


@lru_cache(maxsize=1)
def database_present() -> bool:
    try:
        return bool(available_timezones())
    except Exception:  # noqa: BLE001 - any failure to list zones means there are none to use
        return False


@lru_cache(maxsize=64)
def time_zone(name: str) -> ZoneInfo:
    """The zone called ``name``. Says which of the two things is wrong when it fails."""
    try:
        return ZoneInfo(name)
    except Exception as error:  # noqa: BLE001 - told apart below
        if not database_present():
            raise NoZoneDatabase(
                "this machine has no time zone database, so appointment times cannot be "
                "read: install the 'tzdata' package (pip install tzdata)"
            ) from error
        raise UnknownZone(f"no such time zone: {name!r}") from error
