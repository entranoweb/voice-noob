"""The trusted-proxy setting: what gunicorn is told to believe X-Forwarded-For from."""

import runpy
from pathlib import Path

import pytest

from app.core.proxy import DEFAULT_TRUSTED_PROXY_IPS, trusted_proxy_ips

GUNICORN_CONF = Path(__file__).resolve().parents[2] / "gunicorn.conf.py"


def test_default_trusts_loopback_only() -> None:
    assert trusted_proxy_ips(None) == DEFAULT_TRUSTED_PROXY_IPS == "127.0.0.1,::1"
    assert trusted_proxy_ips("") == DEFAULT_TRUSTED_PROXY_IPS
    assert trusted_proxy_ips("   ") == DEFAULT_TRUSTED_PROXY_IPS


def test_accepts_addresses_and_networks() -> None:
    assert trusted_proxy_ips("10.0.0.5, 172.18.0.0/16,::1") == "10.0.0.5,172.18.0.0/16,::1"


@pytest.mark.parametrize(
    "bad", ["not-an-ip", "10.0.0.5,proxy.internal", "10.0.0.5/33", "10.0.0.5,,"]
)
def test_malformed_entry_is_refused_not_ignored(bad: str) -> None:
    # uvicorn would turn a malformed entry into a literal that matches nothing:
    # the proxy would silently stop being trusted. Refuse at start-up instead.
    with pytest.raises(ValueError, match="TRUSTED_PROXY_IPS"):
        trusted_proxy_ips(bad)


def test_star_is_allowed_only_when_it_is_the_whole_value() -> None:
    assert trusted_proxy_ips("*") == "*"
    with pytest.raises(ValueError, match="TRUSTED_PROXY_IPS"):
        trusted_proxy_ips("10.0.0.5,*")


def test_gunicorn_config_reads_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    monkeypatch.delenv("TRUSTED_PROXY_IPS", raising=False)
    default = runpy.run_path(str(GUNICORN_CONF))
    assert default["forwarded_allow_ips"] == "127.0.0.1,::1"

    monkeypatch.setenv("TRUSTED_PROXY_IPS", "172.18.0.0/16")
    explicit = runpy.run_path(str(GUNICORN_CONF))
    assert explicit["forwarded_allow_ips"] == "172.18.0.0/16"


def test_request_log_does_not_take_its_address_from_a_client_header() -> None:
    from starlette.requests import Request

    from app.middleware.request_tracing import RequestTracingMiddleware

    scope = {
        "type": "http",
        "client": ("192.0.2.10", 5000),
        "headers": [(b"x-forwarded-for", b"203.0.113.9"), (b"x-real-ip", b"203.0.113.8")],
    }
    middleware = RequestTracingMiddleware(app=lambda *_: None)  # type: ignore[arg-type]
    assert middleware._get_client_ip(Request(scope)) == "192.0.2.10"


def test_gunicorn_config_ignores_the_ambient_forwarded_allow_ips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # FORWARDED_ALLOW_IPS=* in a platform's environment must not switch trust on
    # behind the setting's back.
    monkeypatch.delenv("TRUSTED_PROXY_IPS", raising=False)
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    conf = runpy.run_path(str(GUNICORN_CONF))
    assert conf["forwarded_allow_ips"] == "127.0.0.1,::1"
