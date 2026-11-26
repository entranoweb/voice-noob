"""Redaction helpers for log output.

Logs outlive the call they describe and are read by people and vendors who
were never meant to see a caller's number, an email address, what was said, or
what a tool was asked to do. Nothing in ``app`` should put those values in a
log record; use these helpers instead so the rule lives in one place.

Ids (agent, call, record, user) are not personal data and are logged as is.
"""

from __future__ import annotations

import logging
import re
import sys
import traceback
from collections.abc import Mapping
from typing import Any

_VISIBLE_TAIL = 2
_PHONE_IN_TEXT = re.compile(r"\+?\d[\d\s().\-]{5,}\d")
# Digit runs that are not phone numbers and must survive masking: UUIDs, ISO
# dates and times, and IPv4 addresses.
_KEEP_IN_TEXT = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
    r"|\d{1,3}(?:\.\d{1,3}){3}(?![\d.])"
)
_TEXT_OR_KEEP = re.compile(f"(?P<keep>{_KEEP_IN_TEXT.pattern})|(?P<phone>{_PHONE_IN_TEXT.pattern})")
# SQLAlchemy and DBAPI messages append the statement and its bound parameters.
_SQL_TAIL = re.compile(r"\s*\[(?:SQL|parameters):.*", re.DOTALL)
# Free text is cut to this many characters before any pattern runs on it: the
# patterns are linear, but this runs on the event loop and a log line has no
# business being larger.
_MAX_SCRUBBED_CHARS = 8000
_DB_MODULES = ("sqlalchemy", "asyncpg", "psycopg", "aiosqlite", "sqlite3")
_EMAIL_IN_TEXT = re.compile(r"(?<![\w.+\-])[\w.+\-]+@([\w\-]+(?:\.[\w\-]+)+)")


def redact_phone(value: object) -> str | None:
    """Mask every digit of a phone number except the last two.

    ``+15550100042`` becomes ``+*********42``. Separators and a leading ``+``
    are kept so the shape of the value is still visible. ``None`` stays
    ``None``; a value with two digits or fewer is masked entirely.
    """
    if value is None:
        return None
    text = str(value)
    total = sum(ch.isdigit() for ch in text)
    keep_from = total - _VISIBLE_TAIL if total > _VISIBLE_TAIL else total
    seen = 0
    out: list[str] = []
    for ch in text:
        if ch.isdigit():
            out.append(ch if seen >= keep_from else "*")
            seen += 1
        else:
            out.append(ch)
    return "".join(out)


def redact_email(value: object) -> str | None:
    """Drop the local part of an email address and keep the domain."""
    if value is None:
        return None
    text = str(value)
    if "@" not in text:
        return "***"
    return "***@" + text.rsplit("@", 1)[1]


def describe_value(value: object) -> str:
    """Describe a value by type and size only, never by content."""
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (str, bytes, list, tuple, set, dict)):
        return f"{type(value).__name__}(len={len(value)})"
    return type(value).__name__


def describe_text(value: object) -> str:
    """Log free text (a transcript, a message body) as its length only."""
    return describe_value(value if value is None else str(value))


def redact_arguments(arguments: object) -> dict[str, str]:
    """Summarise tool arguments as ``{key: type(len=n)}``. Values never appear."""
    if isinstance(arguments, Mapping):
        return {str(key): describe_value(val) for key, val in arguments.items()}
    return {"<arguments>": describe_value(arguments)}


def redact_mapping(values: Mapping[str, Any]) -> dict[str, str]:
    """Same shape as ``redact_arguments``, for query strings and payloads."""
    return redact_arguments(values)


def scrub_text(text: object) -> str:
    """Mask phone numbers and email addresses found inside free text.

    For strings that cannot be dropped (an upstream error body, an exception
    message) but may echo what was sent. This is a safety net, not a licence
    to log caller text: transcripts and tool values should not reach it.
    Everything from ``[SQL:`` or ``[parameters:`` on is dropped, since that is
    where a database error carries the bound values. UUIDs, ISO timestamps and
    IPv4 addresses are left alone.
    """
    out = str(text)
    if len(out) > _MAX_SCRUBBED_CHARS:
        out = out[:_MAX_SCRUBBED_CHARS] + " [truncated]"
    out = _SQL_TAIL.sub(" [sql removed]", out)
    out = _EMAIL_IN_TEXT.sub(lambda m: "***@" + m.group(1), out)
    return _TEXT_OR_KEEP.sub(lambda m: m.group("keep") or redact_phone(m.group("phone")) or "", out)


def _is_database_error(exc: BaseException) -> bool:
    """True for SQLAlchemy and DB driver errors, whose text carries bound values."""
    return any((cls.__module__ or "").split(".")[0] in _DB_MODULES for cls in type(exc).__mro__)


def describe_exception(exc: BaseException) -> str:
    """``ExcType: message`` safe to log: masked, and type only for database errors."""
    name = type(exc).__name__
    if _is_database_error(exc):
        return name
    return f"{name}: {scrub_text(exc)}"


_UNLOGGABLE = "[unloggable]"
_MAX_DEPTH = 8


def redact_event_dict(_logger: Any, _method: str, event_dict: Any) -> Any:
    """structlog processor: scrub every value in the record.

    Backstop for call sites that were missed. Put it after ``format_exc_info``
    so the rendered ``exception`` text is covered, and before the renderer.
    It never raises: a value it cannot walk (a cycle, a hostile ``__str__``)
    becomes ``"[unloggable]"``, because a logging call must not fail a request.
    """
    out: dict[str, Any] = {}
    for key, val in event_dict.items():
        try:
            out[key] = _scrub_value(val, 0)
        except Exception:
            out[key] = _UNLOGGABLE
    return out


def _scrub_value(value: Any, depth: int) -> Any:  # noqa: PLR0911
    """Return a JSON-safe, scrubbed copy: primitives stay, everything else is text."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return scrub_text(value)
    if depth >= _MAX_DEPTH:
        return _UNLOGGABLE
    if isinstance(value, Mapping):
        return {str(k): _scrub_value(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_scrub_value(v, depth + 1) for v in value]
    if isinstance(value, BaseException):
        return describe_exception(value)
    if isinstance(value, (bytes, bytearray)):
        return scrub_text(bytes(value[:_MAX_SCRUBBED_CHARS]).decode("utf-8", "replace"))
    return scrub_text(str(value))


def _scrub_record(record: logging.LogRecord) -> None:
    """Scrub a stdlib record in place: message, arguments and traceback text."""
    if isinstance(record.msg, str):
        record.msg = scrub_text(record.msg)
    else:
        record.msg = scrub_text(str(record.msg))
    if record.args:
        record.args = _scrub_args(record.args)
    if record.exc_info and record.exc_info[1] is not None:
        # Render now, scrubbed, and drop exc_info: Formatter.format reuses
        # exc_text instead of formatting the raw traceback itself.
        text = "".join(traceback.format_exception(*record.exc_info))
        record.exc_text = scrub_text(text[-_MAX_SCRUBBED_CHARS:])
        record.exc_info = None
    elif record.exc_text:
        record.exc_text = scrub_text(record.exc_text)


def _scrub_args(args: Any) -> Any:
    if isinstance(args, Mapping):
        return {
            k: _scrub_value(v, 1) if not isinstance(v, (int, float)) else v for k, v in args.items()
        }
    # %d and %s both accept the original number, so only text is rewritten.
    return tuple(v if isinstance(v, (int, float)) else _scrub_value(v, 1) for v in args)


_factory_installed = False


def install_stdlib_redaction() -> None:
    """Scrub every stdlib ``logging`` record, whoever emitted it.

    ``redact_event_dict`` only sees structlog events. uvicorn, SQLAlchemy,
    httpx and the modules that use ``logging.getLogger`` bypass it, and a
    session error logged that way prints the statement's bound parameters. The
    handlers belong to the server, so rather than configure one this wraps the
    record factory (https://docs.python.org/3/library/logging.html#logging.setLogRecordFactory):
    every record is scrubbed as it is created, before any handler sees it.
    Safe to call more than once.
    """
    global _factory_installed
    if _factory_installed:
        return
    original = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = original(*args, **kwargs)
        try:
            _scrub_record(record)
        except Exception:
            record.msg, record.args, record.exc_info = _UNLOGGABLE, None, None
        return record

    logging.setLogRecordFactory(factory)
    _factory_installed = True


def log_exception(
    log: Any,
    event: str,
    exc: BaseException | None = None,
    *,
    level: str = "error",
    **fields: Any,
) -> None:
    """Log a caught exception without putting its message in the record.

    ``log.exception`` sets ``exc_info`` and structlog renders the whole
    traceback, which ends in ``ExcType: message``. Carrier and CRM client
    errors put request URLs, response bodies and the numbers and emails in
    them in that message, and a call site cannot redact text that a renderer
    adds later. So this logs at error level *without* ``exc_info``: the
    exception type, the message through :func:`scrub_text` (omitted for database errors, whose
    message carries the bound SQL parameters), and the file, line
    and function where it was raised, which is what the traceback was for.

    ``level`` is the log method to use (``warning`` for failures the call survives).

    Call it from inside ``except``; ``exc`` defaults to the exception being
    handled. Leave framework-internal exceptions on plain ``log.exception``.
    """
    exc = exc if exc is not None else sys.exc_info()[1]
    if exc is None:
        getattr(log, level)(event, **fields)
        return
    where = None
    tb = exc.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    if tb is not None:
        code = tb.tb_frame.f_code
        where = f"{code.co_filename.rsplit('/', 1)[-1]}:{tb.tb_lineno} in {code.co_name}"
    message = None if _is_database_error(exc) else scrub_text(exc)
    getattr(log, level)(event, error_type=type(exc).__name__, error=message, where=where, **fields)
