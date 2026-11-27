"""Runtime knobs: log level, DB pool, gunicorn recycling and Sentry.

Settings are plain pydantic models here (built with explicit values), so no test
reads the environment of the machine it runs on.
"""

import logging
import runpy
from pathlib import Path
from typing import Any

import pytest
import structlog
from pydantic import ValidationError

from app.core.config import Settings

BACKEND = Path(__file__).resolve().parents[2]
GUNICORN_CONF = BACKEND / "gunicorn.conf.py"
COMPOSE = BACKEND.parent / "docker-compose.prod.yml"
DEPLOYMENT = BACKEND.parent / "DEPLOYMENT.md"

PHONE = "+15550100042"


def _settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)


# ---------------------------------------------------------------- settings


def test_defaults_are_info_and_a_small_pool() -> None:
    s = _settings()
    assert s.effective_log_level == logging.INFO
    assert (s.DB_POOL_SIZE, s.DB_MAX_OVERFLOW) == (10, 5)
    assert pytest.approx(0.1) == s.SENTRY_TRACES_SAMPLE_RATE


def test_log_level_parses_case_insensitively_and_beats_debug() -> None:
    assert _settings(LOG_LEVEL="warning").effective_log_level == logging.WARNING
    assert _settings(LOG_LEVEL="DEBUG").effective_log_level == logging.DEBUG
    assert _settings(DEBUG=True, LOG_LEVEL="error").effective_log_level == logging.ERROR


def test_debug_without_log_level_still_means_debug() -> None:
    assert _settings(DEBUG=True).effective_log_level == logging.DEBUG


def test_unknown_log_level_is_refused() -> None:
    with pytest.raises(ValidationError, match="LOG_LEVEL"):
        _settings(LOG_LEVEL="loud")


def test_pool_settings_parse_and_reject_nonsense() -> None:
    s = _settings(DB_POOL_SIZE=20, DB_MAX_OVERFLOW=0)
    assert (s.DB_POOL_SIZE, s.DB_MAX_OVERFLOW) == (20, 0)
    with pytest.raises(ValidationError):
        _settings(DB_POOL_SIZE=0)
    with pytest.raises(ValidationError):
        _settings(DB_MAX_OVERFLOW=-1)


@pytest.mark.parametrize("rate", [-0.1, 1.5])
def test_traces_sample_rate_must_be_a_fraction(rate: float) -> None:
    with pytest.raises(ValidationError):
        _settings(SENTRY_TRACES_SAMPLE_RATE=rate)


def test_the_engine_is_built_from_the_pool_settings() -> None:
    from app.core.config import settings
    from app.db.session import engine

    assert engine.pool.size() == settings.DB_POOL_SIZE  # type: ignore[attr-defined]
    assert engine.pool._max_overflow == settings.DB_MAX_OVERFLOW  # type: ignore[attr-defined]


# ---------------------------------------------------------------- log level


@pytest.fixture
def restore_structlog() -> Any:
    yield
    structlog.reset_defaults()


def test_configured_level_decides_what_is_printed(
    capsys: pytest.CaptureFixture[str], restore_structlog: None
) -> None:
    from app.core.logging_setup import configure_logging

    configure_logging(logging.INFO)
    log = structlog.get_logger()
    log.debug("frame_event")
    log.info("call_started", call_to=PHONE)
    out = capsys.readouterr().out
    assert "call_started" in out
    assert "frame_event" not in out
    # #66's redaction is still in the chain.
    assert PHONE not in out

    configure_logging(logging.WARNING)
    structlog.get_logger().info("lifecycle_line")
    assert "lifecycle_line" not in capsys.readouterr().out


def test_per_frame_lines_are_debug_not_info() -> None:
    source = (BACKEND / "app/api/telephony_ws.py").read_text()
    for event in ("sending_audio_to_twilio", "realtime_event_received"):
        idx = source.index(f'"{event}"')
        call = source[source.rfind("log.", 0, idx) : idx]
        assert call.startswith("log.debug("), f"{event} must be logged at debug"


# ---------------------------------------------------------------- gunicorn


def _gunicorn(monkeypatch: pytest.MonkeyPatch, **env: str) -> dict[str, Any]:
    for name in ("GUNICORN_MAX_REQUESTS", "GUNICORN_MAX_REQUESTS_JITTER"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return runpy.run_path(str(GUNICORN_CONF))


def test_gunicorn_does_not_recycle_workers_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    conf = _gunicorn(monkeypatch)
    assert conf["max_requests"] == 0


def test_gunicorn_max_requests_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conf = _gunicorn(monkeypatch, GUNICORN_MAX_REQUESTS="5000", GUNICORN_MAX_REQUESTS_JITTER="50")
    assert (conf["max_requests"], conf["max_requests_jitter"]) == (5000, 50)


def test_gunicorn_workers_empty_means_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # docker-compose.prod.yml passes GUNICORN_WORKERS="" when it is unset.
    monkeypatch.setenv("GUNICORN_WORKERS", "")
    assert 1 <= runpy.run_path(str(GUNICORN_CONF))["workers"] <= 8


def test_gunicorn_rejects_a_negative_max_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="GUNICORN_MAX_REQUESTS"):
        _gunicorn(monkeypatch, GUNICORN_MAX_REQUESTS="-1")


# ---------------------------------------------------------------- sentry


class _FakeSettings:
    SENTRY_DSN = "https://key@example.test/1"
    SENTRY_ENVIRONMENT = "production"
    SENTRY_TRACES_SAMPLE_RATE = 0.1


def test_sentry_init_kwargs_turn_the_data_off() -> None:
    from app.core.sentry import scrub_event, sentry_init_kwargs

    kwargs = sentry_init_kwargs(_FakeSettings())  # type: ignore[arg-type]
    assert kwargs["send_default_pii"] is False
    assert kwargs["include_local_variables"] is False
    assert kwargs["max_request_body_size"] == "never"
    assert kwargs["traces_sample_rate"] == pytest.approx(0.1)
    assert kwargs["dsn"] == "https://key@example.test/1"
    assert kwargs["environment"] == "production"
    assert kwargs["before_send"] is scrub_event
    assert kwargs["before_send_transaction"] is scrub_event


def test_init_sentry_passes_those_kwargs_to_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    import sentry_sdk

    from app.core.sentry import init_sentry

    seen: dict[str, Any] = {}
    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: seen.update(kw))
    assert init_sentry(_FakeSettings()) is True  # type: ignore[arg-type]
    assert seen["send_default_pii"] is False
    assert seen["include_local_variables"] is False


def test_init_sentry_is_off_without_a_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    import sentry_sdk

    from app.core.sentry import init_sentry

    class NoDsn(_FakeSettings):
        SENTRY_DSN = None

    monkeypatch.setattr(sentry_sdk, "init", lambda **_: pytest.fail("init must not run"))
    assert init_sentry(NoDsn()) is False  # type: ignore[arg-type]


def _sample_event() -> dict[str, Any]:
    return {
        "event_id": "0f3b8c1e5d6a4c7f8a9b0c1d2e3f4a5b",
        "message": f"call to {PHONE} failed for pat@example.test",
        "request": {
            "url": "https://api.example.test/api/v1/telephony/twilio/voice",
            "method": "POST",
            "query_string": f"token=abc&To={PHONE}",
            "data": {"To": PHONE, "Body": "my date of birth is 1 Jan 1980"},
            "cookies": {"session": "s3cret"},
            "headers": {
                "Authorization": "Bearer abc",
                "Cookie": "session=s3cret",
                "X-Twilio-Signature": "sig",
                "telnyx-signature-ed25519": "sig2",
                "Telnyx-Timestamp": "1700000000",
                "X-Api-Key": "k",
                "Content-Type": "application/json",
                "User-Agent": "TwilioProxy/1.1",
            },
        },
        "user": {"id": "u1", "email": "pat@example.test", "ip_address": "203.0.113.9"},
        "exception": {
            "values": [
                {
                    "type": "RuntimeError",
                    "value": f"dial {PHONE} refused",
                    "stacktrace": {
                        "frames": [
                            {"function": "dial", "lineno": 10, "vars": {"to": PHONE}},
                        ]
                    },
                }
            ]
        },
        "breadcrumbs": {"values": [{"message": f"to={PHONE}", "data": {"to": PHONE}}]},
        "extra": {"caller": PHONE, "n": 3},
    }


def test_scrubber_removes_bodies_and_secret_headers() -> None:
    from app.core.sentry import scrub_event

    out = scrub_event(_sample_event(), {})
    assert out is not None
    request = out["request"]
    assert "data" not in request
    assert "cookies" not in request
    assert "query_string" not in request
    headers = {k.lower() for k in request["headers"]}
    assert not headers & {
        "authorization",
        "cookie",
        "x-twilio-signature",
        "telnyx-signature-ed25519",
        "telnyx-timestamp",
        "x-api-key",
    }
    assert request["headers"]["Content-Type"] == "application/json"
    assert request["url"] == "https://api.example.test/api/v1/telephony/twilio/voice"


def test_scrubber_masks_phones_and_emails_everywhere_and_drops_locals() -> None:
    import json

    from app.core.sentry import scrub_event

    out = scrub_event(_sample_event(), {})
    assert out is not None
    blob = json.dumps(out)
    assert PHONE not in blob
    assert "pat@example.test" not in blob
    assert "1 Jan 1980" not in blob
    assert "203.0.113.9" not in blob
    assert "vars" not in out["exception"]["values"][0]["stacktrace"]["frames"][0]
    # Structure the issue grouping depends on survives.
    assert out["event_id"] == "0f3b8c1e5d6a4c7f8a9b0c1d2e3f4a5b"
    assert out["exception"]["values"][0]["type"] == "RuntimeError"
    assert out["exception"]["values"][0]["stacktrace"]["frames"][0]["lineno"] == 10
    assert out["extra"]["n"] == 3


def test_scrubber_never_raises_and_never_drops_the_event() -> None:
    from app.core.sentry import scrub_event

    cyclic: dict[str, Any] = {"request": {"headers": None, "data": 1}}
    cyclic["self"] = cyclic
    assert scrub_event(cyclic, {}) is not None


# ---------------------------------------------------------------- deployment files


def test_compose_passes_every_new_variable() -> None:
    text = COMPOSE.read_text()
    for name in (
        "LOG_LEVEL",
        "DB_POOL_SIZE",
        "DB_MAX_OVERFLOW",
        "GUNICORN_MAX_REQUESTS",
        "GUNICORN_WORKERS",
        "SENTRY_TRACES_SAMPLE_RATE",
        "POSTGRES_MAX_CONNECTIONS",
    ):
        assert f"{name}:" in text or f"max_connections=${{{name}" in text, name


def test_deployment_documents_every_new_variable() -> None:
    text = DEPLOYMENT.read_text()
    for name in (
        "LOG_LEVEL",
        "DB_POOL_SIZE",
        "DB_MAX_OVERFLOW",
        "GUNICORN_MAX_REQUESTS",
        "SENTRY_TRACES_SAMPLE_RATE",
        "POSTGRES_MAX_CONNECTIONS",
    ):
        assert f"`{name}`" in text, name


def test_default_pool_fits_postgres_default_compose_limit() -> None:
    """workers x (size + overflow) must stay under max_connections, with headroom."""
    s = _settings()
    workers = 8  # gunicorn.conf.py caps the default at 8
    import re

    match = re.search(r"POSTGRES_MAX_CONNECTIONS:-(\d+)", COMPOSE.read_text())
    assert match is not None
    assert workers * (s.DB_POOL_SIZE + s.DB_MAX_OVERFLOW) <= int(match.group(1)) - 20
