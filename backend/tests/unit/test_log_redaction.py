"""Unit tests for the log redaction helpers."""

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
