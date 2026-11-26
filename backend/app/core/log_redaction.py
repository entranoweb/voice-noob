"""Redaction helpers for log output.

Logs outlive the call they describe and are read by people and vendors who
were never meant to see a caller's number, an email address, what was said, or
what a tool was asked to do. Nothing in ``app`` should put those values in a
log record; use these helpers instead so the rule lives in one place.

Ids (agent, call, record, user) are not personal data and are logged as is.
"""

from __future__ import annotations

import logging
import math
import re
import sys
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
_SQL_MARKER = re.compile(r"\[(?:SQL|parameters):")
# Free text is bounded before any pattern runs on it, because this runs on the
# event loop. The text is scrubbed first and cut afterwards (see scrub_text);
# the margin lets a number or address that straddles the cut be seen whole.
_MAX_SCRUBBED_CHARS = 8000
_WINDOW_MARGIN = 300
# Escapes that hide a character from the patterns: %2B in a URL, \u0040 or \x40
# in JSON or a repr. Only ASCII is decoded; that is all a number or address uses.
_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})|\\u([0-9A-Fa-f]{4})|\\x([0-9A-Fa-f]{2})")
_ASCII_LIMIT = 0x80
_MIN_MASKED_INT_DIGITS = 7
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


def _decoded_view(raw: str) -> tuple[str, list[tuple[int, int]] | None]:
    """The text with ASCII escapes decoded, and where each character came from.

    Returns ``(raw, None)`` when there is nothing to decode. Otherwise the second
    item has, for each decoded character, the ``(start, end)`` span it occupies in
    ``raw``, so a match in the decoded text can be masked at its raw position.
    """
    if "%" not in raw and "\\" not in raw:
        return raw, None
    chars: list[str] = []
    spans: list[tuple[int, int]] = []
    pos = 0
    for m in _ESCAPE.finditer(raw):
        code = int(next(g for g in m.groups() if g), 16)
        if code >= _ASCII_LIMIT:
            continue
        for i in range(pos, m.start()):
            chars.append(raw[i])
            spans.append((i, i + 1))
        chars.append(chr(code))
        spans.append((m.start(), m.end()))
        pos = m.end()
    for i in range(pos, len(raw)):
        chars.append(raw[i])
        spans.append((i, i + 1))
    return "".join(chars), spans


def _scrub_all(raw: str) -> str:
    """Mask phones and emails in ``raw``, raw or percent/unicode-escaped; no cutting."""
    decoded, spans = _decoded_view(raw)
    edits: list[tuple[int, int, str]] = []
    limit = len(decoded)
    sql = _SQL_MARKER.search(decoded)
    if sql:
        limit = sql.start()
        while limit > 0 and decoded[limit - 1].isspace():
            limit -= 1
    head = decoded[:limit]
    emails = [(m.start(), m.end(), "***@" + m.group(1)) for m in _EMAIL_IN_TEXT.finditer(head)]
    edits.extend(emails)
    for m in _TEXT_OR_KEEP.finditer(head):
        if m.group("phone") is None:
            continue
        if any(m.start() < e_end and e_start < m.end() for e_start, e_end, _ in emails):
            continue
        edits.append((m.start(), m.end(), redact_phone(m.group("phone")) or ""))
    if sql:
        edits.append((limit, len(decoded), " [sql removed]"))
    if not edits:
        return raw
    edits.sort()
    out: list[str] = []
    cursor = 0
    for start, end, replacement in edits:
        raw_start = start if spans is None else spans[start][0]
        raw_end = end if spans is None else (spans[end - 1][1] if end > start else raw_start)
        if end == len(decoded) and replacement == " [sql removed]":
            raw_end = len(raw)
        out.append(raw[cursor:raw_start])
        out.append(replacement)
        cursor = raw_end
    out.append(raw[cursor:])
    return "".join(out)


def _drop_trailing_partial(text: str) -> str:
    """Remove a final token that the cut may have split."""
    end = len(text)
    while end > 0 and not text[end - 1].isspace():
        end -= 1
    return text[:end]


def scrub_text(text: object) -> str:
    """Mask phone numbers and email addresses found inside free text.

    For strings that cannot be dropped (an upstream error body, an exception
    message, an access-log line) but may echo what was sent. This is a safety
    net, not a licence to log caller text: transcripts and tool values should
    not reach it.

    * Escaped forms are covered: the text is also read with ``%2B``-style and
      ``\\u0040`` / ``\\x40`` escapes decoded, and a match there is masked at
      its raw position (a double-encoded ``%252B`` is not decoded).
    * Everything from ``[SQL:`` or ``[parameters:`` on is dropped, since that is
      where a database error carries the bound values.
    * UUIDs, ISO timestamps and IPv4 addresses are left alone.
    * Long text is scrubbed first and cut afterwards, at 8000 characters, with
      the last token dropped and a ``[truncated]`` marker, so a cut can never
      leave half a number or address behind.
    """
    raw = str(text)
    window = raw[: _MAX_SCRUBBED_CHARS + _WINDOW_MARGIN]
    out = _scrub_all(window)
    cut = len(raw) > len(window)
    at_boundary = False
    if len(out) > _MAX_SCRUBBED_CHARS:
        at_boundary = out[_MAX_SCRUBBED_CHARS].isspace()
        out = out[:_MAX_SCRUBBED_CHARS]
        cut = True
    if cut:
        if not at_boundary:
            out = _drop_trailing_partial(out)
        out += " [truncated]"
    return out


def scrub_traceback(text: str) -> str:
    """Scrub a rendered traceback, keeping its end, where the message is."""
    limit = _MAX_SCRUBBED_CHARS + _WINDOW_MARGIN
    if len(text) > limit:
        text = text[-limit:]
        start = 0
        while start < len(text) and not text[start].isspace():
            start += 1
        text = "[truncated] " + text[start:].lstrip()
    return _scrub_all(text)


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
# Attributes every LogRecord has; anything else came from ``extra=``.
_STANDARD_RECORD_KEYS = frozenset(vars(logging.makeLogRecord({}))) | {
    "message",
    "asctime",
    "taskName",
}


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
            out[scrub_text(key) if isinstance(key, str) else key] = _scrub_value(val, 0)
        except Exception:
            out[key] = _UNLOGGABLE
    return out


def _scrub_value(value: Any, depth: int) -> Any:  # noqa: PLR0911
    """Return a JSON-safe, scrubbed copy: text and containers are walked, the rest stringified."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        # A number logged as a number is still a number: 15550100042 prints in
        # full. Only a long run of integer digits is masked; counts, ports, ids
        # and ratios are left alone.
        digits = len(str(int(abs(value)))) if math.isfinite(value) else 0
        if digits >= _MIN_MASKED_INT_DIGITS:
            return redact_phone(int(value))
        return value
    if isinstance(value, str):
        return scrub_text(value)
    if depth >= _MAX_DEPTH:
        return _UNLOGGABLE
    if isinstance(value, Mapping):
        return {scrub_text(k): _scrub_value(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_scrub_value(v, depth + 1) for v in value]
    if isinstance(value, BaseException):
        return describe_exception(value)
    if isinstance(value, (bytes, bytearray)):
        return scrub_text(
            bytes(value[: _MAX_SCRUBBED_CHARS + _WINDOW_MARGIN]).decode("utf-8", "replace")
        )
    return scrub_text(str(value))


def _scrub_record(record: logging.LogRecord) -> None:
    """Scrub a stdlib record in place: message, arguments, ``extra=`` and stack.

    The message is formatted here, once, and the arguments dropped, so a number
    passed as ``%d`` is masked as text too. The traceback is not touched: it
    stays on ``exc_info`` and is scrubbed when a formatter renders it.
    """
    try:
        message = record.getMessage()
    except Exception:
        message = f"{record.msg} {record.args}"
    record.msg = scrub_text(message)
    record.args = None
    if record.stack_info:
        record.stack_info = scrub_traceback(record.stack_info)
    for key in [k for k in vars(record) if k not in _STANDARD_RECORD_KEYS]:
        try:
            setattr(record, key, _scrub_value(getattr(record, key), 1))
        except Exception:
            setattr(record, key, _UNLOGGABLE)


_installed = False


def install_stdlib_redaction() -> None:
    """Scrub every stdlib ``logging`` record, whoever emitted it.

    ``redact_event_dict`` only sees structlog events. uvicorn, SQLAlchemy,
    httpx and the modules that use ``logging.getLogger`` bypass it, and a
    session error logged that way prints the statement's bound parameters, and
    uvicorn's access log prints the raw request line. The handlers belong to the
    server, so this does not configure one. It does two things, both global:

    * wraps ``Logger.makeRecord`` (which calls the record factory, then copies
      ``extra=`` onto the record) so each record's message, arguments,
      ``extra=`` attributes and stack are scrubbed before any handler sees it;
    * wraps ``logging.Formatter.formatException`` so a traceback is scrubbed
      whenever any formatter renders it. ``exc_info`` stays on the record, so
      handlers that read it (Sentry's logging integration) still get the live
      exception; those receive the raw value by design and are the owner's to
      configure.

    Not covered: a record built by ``logging.makeLogRecord`` or whose ``msg`` is
    changed after creation, and a Formatter subclass that overrides
    ``formatException``. Safe to call more than once.
    """
    global _installed
    if _installed:
        return
    original_make = logging.Logger.makeRecord

    def make_record(self: logging.Logger, *args: Any, **kwargs: Any) -> logging.LogRecord:
        # Logger.makeRecord, not the record factory: ``extra=`` is copied onto
        # the record after the factory returns, so only this sees it.
        record = original_make(self, *args, **kwargs)
        try:
            _scrub_record(record)
        except Exception:
            record.msg, record.args = _UNLOGGABLE, None
        return record

    logging.Logger.makeRecord = make_record  # type: ignore[method-assign]

    original_format = logging.Formatter.formatException

    def format_exception(self: logging.Formatter, ei: Any) -> str:
        return scrub_traceback(original_format(self, ei))

    logging.Formatter.formatException = format_exception  # type: ignore[method-assign]
    _installed = True


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
