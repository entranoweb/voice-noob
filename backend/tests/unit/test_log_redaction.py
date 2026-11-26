"""Unit tests for the log redaction helpers."""

import pytest
import structlog

from app.core import log_redaction
from app.core.log_redaction import (
    describe_text,
    describe_value,
    redact_arguments,
    redact_email,
    redact_phone,
    scrub_text,
)

NUMBER = "+15550100042"


def test_phone_keeps_only_last_two_digits() -> None:
    assert redact_phone(NUMBER) == "+*********42"


def test_phone_keeps_separators_and_handles_none_and_short() -> None:
    assert redact_phone("(555) 010-0042") == "(***) ***-**42"
    assert redact_phone(None) is None
    assert redact_phone("12") == "**"
    assert redact_phone("") == ""


def test_email_drops_local_part() -> None:
    assert redact_email("jane.doe@example.com") == "***@example.com"
    assert redact_email("not-an-email") == "***"
    assert redact_email(None) is None


def test_arguments_log_keys_types_and_lengths_only() -> None:
    out = redact_arguments(
        {"to": NUMBER, "body": "secret words", "count": 3, "nested": {"a": 1}, "none": None}
    )
    assert out == {
        "to": "str(len=12)",
        "body": "str(len=12)",
        "count": "int",
        "nested": "dict(len=1)",
        "none": "None",
    }
    assert NUMBER not in repr(out)
    assert "secret" not in repr(out)


def test_arguments_tolerates_non_mapping() -> None:
    assert redact_arguments('{"to": "x"}') == {"<arguments>": "str(len=11)"}
    assert redact_arguments(None) == {"<arguments>": "None"}


def test_describe_text_is_length_only() -> None:
    assert describe_text("hello there") == "str(len=11)"
    assert describe_value(True) == "bool"


def test_scrub_text_masks_numbers_and_emails_inside_prose() -> None:
    out = scrub_text(f"bad request for {NUMBER} / jane@example.com: invalid")
    assert NUMBER not in out
    assert "jane" not in out
    assert "+*********42" in out
    assert "***@example.com" in out
    assert out.endswith(": invalid")


def _fail(exc: Exception) -> None:
    raise exc


def test_log_exception_logs_type_masked_message_and_place_but_no_traceback() -> None:
    from structlog.testing import capture_logs

    from app.core.log_redaction import log_exception

    with capture_logs() as records:
        try:
            _fail(ValueError(f"bad {NUMBER} for jane@example.com"))
        except ValueError as exc:
            log_exception(structlog.get_logger(), "thing_failed", exc, call_id="c1")

    (record,) = records
    assert record["event"] == "thing_failed"
    assert record["log_level"] == "error"
    assert record["error_type"] == "ValueError"
    assert record["call_id"] == "c1"
    assert "test_log_redaction.py" in record["where"]
    assert "exc_info" not in record
    assert NUMBER not in repr(record)
    assert "jane" not in repr(record)


def test_log_exception_uses_the_exception_being_handled_by_default() -> None:
    from structlog.testing import capture_logs

    from app.core.log_redaction import log_exception

    with capture_logs() as records:
        try:
            _fail(RuntimeError("boom"))
        except RuntimeError:
            log_exception(structlog.get_logger(), "thing_failed")

    assert records[0]["error_type"] == "RuntimeError"


# --- review round 3 -------------------------------------------------------

from sqlalchemy.exc import DBAPIError  # noqa: E402
from structlog.testing import capture_logs  # noqa: E402

from app.core.log_redaction import (  # noqa: E402
    describe_exception,
    log_exception,
    redact_event_dict,
)

_DB_MESSAGE = (
    "(asyncpg.exceptions.UniqueViolationError) duplicate key\n"
    "[SQL: INSERT INTO contacts (first_name, notes) VALUES ($1, $2)]\n"
    "[parameters: ('Pat Example', 'she said her card ends 4242')]\n"
    "(Background on this error at: https://sqlalche.me/e/20/gkpj)"
)


def _db_error() -> DBAPIError:
    return DBAPIError(
        "INSERT INTO contacts", ("Pat Example", "private note"), Exception("duplicate key")
    )


def test_scrub_text_drops_sql_and_parameters() -> None:
    out = scrub_text(_DB_MESSAGE)
    assert "Pat Example" not in out
    assert "4242" not in out
    assert "INSERT INTO" not in out
    assert "duplicate key" in out


def test_scrub_text_leaves_uuids_timestamps_and_ips_alone() -> None:
    text = (
        "call 3fa85f64-5717-4562-b3fc-2c963f66afa6 at 2026-11-26T16:47:12.123456Z "
        "date 2026-11-26 from 192.168.100.200 number +15550100042"
    )
    out = scrub_text(text)
    assert "3fa85f64-5717-4562-b3fc-2c963f66afa6" in out
    assert "2026-11-26T16:47:12.123456Z" in out
    assert "2026-11-26 " in out
    assert "192.168.100.200" in out
    assert "+15550100042" not in out
    assert "+*********42" in out


def test_log_exception_logs_only_the_type_for_database_errors() -> None:
    with capture_logs() as records:
        try:
            raise _db_error()
        except DBAPIError as exc:
            log_exception(structlog.get_logger(), "persist_failed", exc)

    text = repr(records)
    assert records[0]["error_type"] == "DBAPIError"
    assert records[0]["error"] is None
    assert "Pat Example" not in text
    assert "private note" not in text
    assert "exc_info" not in text


def test_describe_exception_masks_and_drops_database_text() -> None:
    assert describe_exception(ValueError("bad +15550100042")) == "ValueError: bad +*********42"
    assert describe_exception(_db_error()) == "DBAPIError"


def test_processor_scrubs_event_error_and_rendered_exception() -> None:
    event = {
        "event": "failed for pat.example@example.invalid",
        "error": "call +15550100042",
        "exception": "Traceback...\nValueError: x\n" + _DB_MESSAGE,
        "nested": {"to": ["+15550100042"]},
        "count": 15550100042,
    }
    out = redact_event_dict(None, "error", event)
    text = repr(out)
    assert "pat.example" not in text
    assert "+15550100042" not in repr({k: v for k, v in out.items() if k != "count"})
    assert "Pat Example" not in text
    assert "15550100042" not in repr(out)  # a phone logged as an int is masked too


# --- review round 4 -------------------------------------------------------


def _run_with_deadline(fn, seconds: float) -> None:  # type: ignore[no-untyped-def]
    """Run ``fn`` in a thread; fail (not hang) if it takes longer than ``seconds``."""
    import threading

    box: list[BaseException] = []

    def target() -> None:
        try:
            fn()
        except BaseException as exc:
            box.append(exc)

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), (
        f"still running after {seconds}s: a regression made it super-linear"
    )
    if box:
        raise box[0]


def test_scrub_text_is_linear_on_hostile_input() -> None:
    import time

    for hostile in (
        "a" * 1_000_000,
        "1 " * 500_000,
        "x." * 500_000,
        "+1" + " " * 1_000_000,
        "%2" * 500_000,
    ):
        started = time.perf_counter()
        _run_with_deadline(lambda h=hostile: scrub_text(h), 5)
        assert time.perf_counter() - started < 0.5, hostile[:6]


def test_scrub_text_truncates_with_a_marker() -> None:
    assert scrub_text("a" * 20_000).endswith("[truncated]")


def test_processor_survives_cycles_and_hostile_objects() -> None:
    cycle: dict[str, object] = {}
    cycle["self"] = cycle

    class Hostile:
        def __str__(self) -> str:
            raise RuntimeError("no")

    out = redact_event_dict(None, "error", {"event": "x", "cycle": cycle, "obj": Hostile()})
    assert out["event"] == "x"
    assert "[unloggable]" in repr(out["cycle"])
    assert out["obj"] == "[unloggable]"


def test_processor_converts_non_primitives_to_scrubbed_text() -> None:
    out = redact_event_dict(
        None,
        "error",
        {
            "raw": b"call +15550100042",
            "tags": {"+15550100042"},
            "err": ValueError("mail pat.example@example.invalid"),
            "db": _db_error(),
            "n": 3,
        },
    )
    text = repr(out)
    assert "15550100042" not in text
    assert "pat.example" not in text
    assert "private note" not in text
    assert out["db"] == "DBAPIError"
    assert out["n"] == 3


# --- review round 5: encoded values, truncation, numbers, extras ----------


@pytest.mark.parametrize(
    "line",
    [
        "GET /v1/calls?To=%2B1%20555%20010%200042 HTTP/1.1",
        "GET /v1/users?email=user%40example.com HTTP/1.1",
        "GET /v1/users?email=user@example.com HTTP/1.1",
        r'{"to": "\u002b15550100042"}',
        r'{"email": "user\u0040example.com"}',
        "to=%2B15550100042&x=1",
    ],
)
def test_scrub_text_masks_percent_and_unicode_escaped_values(line: str) -> None:
    out = scrub_text(line)
    assert "user" not in out.replace("users", "")
    assert "0100042" not in out
    assert "010%200042" not in out
    assert "example.com" not in out or "***@" in out
    assert "42" in out or "***@" in out or "x=1" in out


def test_scrub_text_keeps_the_rest_of_an_escaped_line() -> None:
    out = scrub_text("GET /v1/calls?To=%2B15550100042&page=2 HTTP/1.1")
    assert out.endswith("&page=2 HTTP/1.1")
    assert out.startswith("GET /v1/calls?To=")


def test_truncation_never_leaves_part_of_a_number_or_address() -> None:
    cap = log_redaction._MAX_SCRUBBED_CHARS
    for pad, secret in (
        (cap - 6, "+155501 00042"),
        (cap - 12, "user@example.com"),
        (cap - 3, "+15550100042"),
    ):
        out = scrub_text("a " * (pad // 2) + "a" * (pad % 2) + " " + secret + " tail")
        assert out.endswith("[truncated]")
        assert "+15550" not in out.replace("+*", "")
        assert "user@" not in out
        assert "@example" not in out
        assert "155501" not in out


def test_traceback_tail_slice_drops_the_leading_partial_token() -> None:
    from app.core.log_redaction import scrub_traceback

    text = "x" * 20_000 + " +15550 100042 end"
    out = scrub_traceback(text)
    assert "100042" not in out.replace("*", "") or "+*" in out
    cut = scrub_traceback("1" * 20_000 + " a@b.example\nRuntimeError: bad")
    assert cut.startswith("[truncated]")
    assert "1111" not in cut


@pytest.mark.parametrize("value", [15550100042, 15550100042.0, -15550100042])
def test_numbers_that_could_be_phones_are_masked(value: float) -> None:
    out = redact_event_dict(None, "info", {"to": value, "nested": {"n": value}})
    assert "15550100042" not in repr(out)


def test_short_numbers_are_left_alone() -> None:
    out = redact_event_dict(None, "info", {"port": 8000, "count": 3, "ratio": 0.5})
    assert out == {"port": 8000, "count": 3, "ratio": 0.5}


def test_dict_keys_are_scrubbed() -> None:
    out = redact_event_dict(None, "info", {"by_email": {"pat@example.com": 1}})
    assert "pat@example.com" not in repr(out)


def test_extra_attributes_and_stack_info_are_scrubbed(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    from app.core.log_redaction import install_stdlib_redaction

    install_stdlib_redaction()
    caplog.set_level(logging.DEBUG)
    logging.getLogger("lib.extra").warning(
        "hello",
        extra={"caller": "+15550100042", "who": {"email": "pat@example.com"}},
        stack_info=True,
    )
    rec = next(r for r in caplog.records if r.name == "lib.extra")
    assert "15550100042" not in repr(rec.caller)  # type: ignore[attr-defined]
    assert "pat@example.com" not in repr(rec.who)  # type: ignore[attr-defined]


def _boom(message: str) -> None:
    raise RuntimeError(message)


def test_formatter_scrubs_a_traceback_but_exc_info_survives() -> None:
    import logging

    from app.core.log_redaction import install_stdlib_redaction

    install_stdlib_redaction()
    try:
        _boom("lookup failed for pat@example.com +15550100042")
    except RuntimeError:
        rec = logging.getLogger("lib.tb").makeRecord(
            "lib.tb", logging.ERROR, __file__, 1, "x", (), __import__("sys").exc_info()
        )
    assert rec.exc_info is not None  # handlers such as Sentry still get the exception
    rendered = logging.Formatter().format(rec)
    assert "RuntimeError" in rendered
    assert "pat@example.com" not in rendered
    assert "15550100042" not in rendered
