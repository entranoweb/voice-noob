"""What a clip's manifest row says about where the clip came from, and what may be done with it.

A clip is a recording of a person. These columns, all optional on read, are the
bank's own record of where each one came from. They are written by a person or
by ``clipbank build`` from what that person supplied; nothing here infers a
speaker or looks at the audio.

==================  ============================================================
``source_kind``     ``call`` or ``session`` (a recording session)
``source_ref``      the call's or session's id on the platform it came from
``speaker_ref``     an opaque id ``spk_<8-32 lowercase letters and digits>``
``provenance``      ``real`` (a recording of a person) or ``synthetic``
``lawful_basis``    one of the UK GDPR Article 6(1) bases (``LAWFUL_BASES``)
``consent_ref``     where the consent record is held (a pointer, never the record)
``recorded_on``     ``YYYY-MM-DD``
``retain_until``    ``YYYY-MM-DD``; the clip may not be used after this day
``label_confirmed_by``  opaque reviewer id ``rev_...``, who listened and confirmed
``label_confirmed_on``  ``YYYY-MM-DD``
==================  ============================================================

A manifest written before these columns existed still loads. A missing
``provenance`` means "provenance unknown", which is not "real": a batch will not
play such a clip unless it was told to.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

SOURCE_KINDS = ("call", "session")
PROVENANCES = ("real", "synthetic")
UNKNOWN = "unknown"

# UK GDPR Article 6(1)(a) to (f), by the word this tool uses for each.
LAWFUL_BASES = (
    "consent",
    "contract",
    "legal_obligation",
    "vital_interests",
    "public_task",
    "legitimate_interests",
)

SPEAKER_PREFIX = "spk"
REVIEWER_PREFIX = "rev"
OPERATOR_PREFIX = "op"

_BODY = r"(?=[a-z0-9]*[0-9])(?=[a-z0-9]*[a-z])[a-z0-9]{8,32}"
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{2,79}$")
# Ten digits in a row is a telephone number, whatever surrounds it.
_PHONE_RUN = re.compile(r"\d{10,}")


def opaque_id_problem(value: str, prefix: str, what: str) -> str:
    """Why ``value`` is not an acceptable opaque id with ``prefix``; empty when it is.

    An opaque id is ``<prefix>_<body>``: lowercase letters and digits, at least
    one of each, 8 to 32 of them. That shape cannot hold a spaced name or a
    telephone number, and a bare name (``jane``) has no prefix. It cannot stop
    someone typing ``spk_jane1985``: mint ids at random and keep the key that
    links them to a person outside the bank.
    """
    if _PHONE_RUN.search(value.replace(" ", "")):
        return f"{what} {value!r} looks like a phone number; use an opaque id"
    if not re.fullmatch(rf"{prefix}_{_BODY}", value):
        return (
            f"{what} {value!r} is not an opaque id: write {prefix}_ and 8 to 32 random "
            "lowercase letters and digits (at least one of each), never a name or a number"
        )
    return ""


def reference_problem(value: str, what: str) -> str:
    """Why ``value`` is not a plain pointer to a record held elsewhere; empty when it is."""
    if "@" in value or _PHONE_RUN.search(value.replace(" ", "")):
        return f"{what} {value!r} looks like an address or a phone number; give a record id"
    if not _TOKEN.fullmatch(value):
        return f"{what} {value!r} must be an id of 3 to 80 characters with no spaces"
    return ""


def date_problem(value: str, what: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError:
        return f"{what} {value!r} is not a date, YYYY-MM-DD"
    return ""


@dataclass(frozen=True)
class Consent:
    """The provenance fields of one clip, as read. Values are kept as written."""

    source_kind: str = ""
    source_ref: str = ""
    speaker_ref: str = ""
    provenance: str = ""
    lawful_basis: str = ""
    consent_ref: str = ""
    recorded_on: str = ""
    retain_until: str = ""
    label_confirmed_by: str = ""
    label_confirmed_on: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, str | None]) -> Consent:
        def cell(name: str) -> str:
            return (row.get(name) or "").strip()

        return cls(**{name: cell(name) for name in FIELDS})

    @property
    def kind(self) -> str:
        """``real``, ``synthetic`` or ``unknown``. A description after the word is allowed.

        ``real-call`` is read as ``real`` (a capture tool may write that). Any
        other word is unknown, and ``problems`` says so.
        """
        word = self.provenance.lower()
        if word.startswith("synthetic"):
            return "synthetic"
        if word == "real" or word.startswith(("real-", "real ", "real:")):
            return "real"
        return UNKNOWN

    def expired(self, today: date) -> bool:
        """True when ``retain_until`` is a valid date before ``today``."""
        try:
            return date.fromisoformat(self.retain_until) < today
        except ValueError:
            return False

    def problems(self) -> list[str]:
        """Every field that is written and wrong, or missing where another needs it."""
        found: list[str] = []

        def note(text: str) -> None:
            if text:
                found.append(text)

        if self.source_kind and self.source_kind not in SOURCE_KINDS:
            found.append(f"source_kind {self.source_kind!r} is not one of {list(SOURCE_KINDS)}")
        if self.source_ref:
            note(reference_problem(self.source_ref, "source_ref"))
        if self.speaker_ref:
            note(opaque_id_problem(self.speaker_ref, SPEAKER_PREFIX, "speaker_ref"))
        if self.provenance and self.kind == UNKNOWN:
            found.append(f"provenance {self.provenance!r} is not one of {list(PROVENANCES)}")
        if self.lawful_basis and self.lawful_basis not in LAWFUL_BASES:
            found.append(f"lawful_basis {self.lawful_basis!r} is not one of {list(LAWFUL_BASES)}")
        if self.consent_ref:
            note(reference_problem(self.consent_ref, "consent_ref"))
        if self.lawful_basis == "consent" and not self.consent_ref:
            found.append("lawful_basis is consent but there is no consent_ref")
        for name in ("recorded_on", "retain_until", "label_confirmed_on"):
            if getattr(self, name):
                note(date_problem(getattr(self, name), name))
        if (
            self.recorded_on
            and self.retain_until
            and not date_problem(self.recorded_on, "")
            and not date_problem(self.retain_until, "")
            and date.fromisoformat(self.retain_until) < date.fromisoformat(self.recorded_on)
        ):
            found.append("retain_until is before recorded_on")
        if self.label_confirmed_by:
            note(opaque_id_problem(self.label_confirmed_by, REVIEWER_PREFIX, "label_confirmed_by"))
            if not self.label_confirmed_on:
                found.append("label_confirmed_by is given without label_confirmed_on")
        elif self.label_confirmed_on:
            found.append("label_confirmed_on is given without label_confirmed_by")
        return found

    def gaps(self) -> list[str]:
        """What a real recording should carry and does not. Not an error; a thing to fill in."""
        if self.kind != "real":
            return []
        wanted = ("speaker_ref", "lawful_basis", "recorded_on", "retain_until")
        return [name for name in wanted if not getattr(self, name)]


FIELDS = tuple(Consent.__dataclass_fields__)
