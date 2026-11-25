"""Telephone numbers: comparing them, and the list of numbers the caller may dial.

The phone leg dials real numbers. A mistyped digit reaches a stranger, and a
copied command can reach a live clinic line, so a real call is only placed to a
number the operator has listed beforehand. The list is never guessed and never
defaulted: no list, no call.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path

ALLOWED_ENV = "SYNTHIQ_CALLER_ALLOWED_NUMBERS"
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def digits(number: str) -> str:
    """The number's digits only, without an international ``00`` prefix."""
    found = re.sub(r"\D", "", number or "")
    return found[2:] if found.startswith("00") else found


def is_e164(number: str) -> bool:
    return bool(_E164.match(number or ""))


def same_number(one: str, other: str) -> bool:
    """Whether two written forms are the same line.

    Exact digits match. Failing that, the last ten digits match when both are
    at least that long, which is how ``07400 123456`` and ``+44 7400 123456``
    are the same phone. Shorter numbers must match exactly.
    """
    a, b = digits(one), digits(other)
    if not a or not b:
        return False
    if a == b:
        return True
    return len(a) >= 10 and len(b) >= 10 and a[-10:] == b[-10:]


def allowed_numbers(argument: str, env: Mapping[str, str]) -> tuple[str, ...]:
    """The numbers the operator has said may be dialled.

    ``argument`` is a file with one number per line (``#`` starts a comment), or
    a comma-separated list. The ``SYNTHIQ_CALLER_ALLOWED_NUMBERS`` variable adds
    to it, comma-separated.
    """
    found: list[str] = []
    if argument:
        path = Path(argument)
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                entry = line.split("#", 1)[0].strip()
                if entry:
                    found.append(entry)
        else:
            found.extend(part.strip() for part in argument.split(",") if part.strip())
    found.extend(part.strip() for part in env.get(ALLOWED_ENV, "").split(",") if part.strip())
    return tuple(dict.fromkeys(found))


def is_allowed(number: str, allowed: Iterable[str]) -> bool:
    """Exact digits only: a near match is not permission to dial."""
    wanted = digits(number)
    return bool(wanted) and any(digits(entry) == wanted for entry in allowed)
