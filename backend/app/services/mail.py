"""Outgoing mail, behind an interface.

Two implementations exist and no others on purpose: a console one for
development and a fake that keeps messages in memory for tests. No provider is
wired in and nothing here opens a network connection. A real sender is a later,
separate decision (which provider, which sender domain); it plugs in by
implementing :class:`Mailer`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import structlog

from app.core.config import settings

logger = structlog.get_logger()


@dataclass(frozen=True)
class Mail:
    to: str
    subject: str
    text: str


class Mailer(Protocol):
    async def send(self, mail: Mail) -> None: ...


class ConsoleMailer:
    """Writes the message to the log instead of sending it.

    The text of a reset mail is a working credential, so it is printed only when
    DEBUG is on. Outside DEBUG the log says that a mail was not delivered and
    nothing else: a log line must never be enough to take over an account.
    """

    async def send(self, mail: Mail) -> None:
        if settings.DEBUG:
            logger.warning(
                "mail_not_sent_console_only", to=mail.to, subject=mail.subject, text=mail.text
            )
        else:
            logger.warning("mail_not_sent_no_mail_provider_configured", subject=mail.subject)


@dataclass
class FakeMailer:
    """Keeps every message in ``outbox``; for tests."""

    outbox: list[Mail] = field(default_factory=list)

    async def send(self, mail: Mail) -> None:
        self.outbox.append(mail)


_console = ConsoleMailer()


def get_mailer() -> Mailer:
    """The mailer routes use. Tests replace it through ``dependency_overrides``."""
    return _console
