"""Unit tests for the log redaction helpers."""

import structlog

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
    assert out["count"] == 15550100042  # non-strings are left alone
