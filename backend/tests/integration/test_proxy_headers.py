"""Client address behind a reverse proxy, with a real gunicorn process.

gunicorn (UvicornWorker) hands ``forwarded_allow_ips`` to uvicorn's
ProxyHeadersMiddleware, which rewrites the client address from X-Forwarded-For
only when the connecting peer is trusted. The rate limiter and the audit trail
both read that rewritten address.

Addresses: the whole 127.0.0.0/8 block is local on Linux, so each actor gets
its own source address and the server sees real, distinct peers:

* 127.0.0.2, 127.0.0.3  clients
* 127.0.0.5             the proxy (not loopback as far as the default
                         ``127.0.0.1,::1`` trust is concerned)

The proxy here is a script acting as one: it appends the peer it saw to
X-Forwarded-For, as Traefik does, and forwards.
"""

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
PROXY_ADDR = "127.0.0.5"
CLIENT_A = "127.0.0.2"
CLIENT_B = "127.0.0.3"

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="uses 127.0.0.0/8 source addresses (Linux)"
)


def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def gunicorn_server(trusted: str | None) -> Iterator[int]:
    port = _free_port()
    env = {
        k: v for k, v in os.environ.items() if k not in ("TRUSTED_PROXY_IPS", "FORWARDED_ALLOW_IPS")
    }
    if trusted is not None:
        env["TRUSTED_PROXY_IPS"] = trusted
    env.update(GUNICORN_BIND=f"0.0.0.0:{port}", GUNICORN_WORKERS="1", GUNICORN_LOG_LEVEL="warning")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "gunicorn",
            "-c",
            "gunicorn.conf.py",
            "tests.support.proxy_probe:app",
        ],
        cwd=BACKEND,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                err = proc.stderr.read().decode() if proc.stderr else ""
                raise RuntimeError(f"gunicorn exited early: {err[-2000:]}")
            try:
                with closing(socket.create_connection(("127.0.0.1", port), timeout=0.5)):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError("gunicorn did not start")
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def _make_proxy(upstream_port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            peer = self.client_address[0]
            existing = self.headers.get("X-Forwarded-For")
            xff = f"{existing}, {peer}" if existing else peer  # append, like Traefik
            conn = http.client.HTTPConnection(
                "127.0.0.1", upstream_port, source_address=(PROXY_ADDR, 0), timeout=10
            )
            conn.request("GET", self.path, headers={"X-Forwarded-For": xff})
            resp = conn.getresponse()
            body = resp.read()
            self.send_response(resp.status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            conn.close()

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _get(
    port: int, source: str, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str | None]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, source_address=(source, 0), timeout=10)
    conn.request("GET", "/probe", headers=headers or {})
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(raw)
    except ValueError:
        return resp.status, {}


@pytest.fixture
def proxy_for() -> Iterator[Callable[[int], int]]:
    servers: list[ThreadingHTTPServer] = []

    def start(upstream: int) -> int:
        server = _make_proxy(upstream)
        servers.append(server)
        return int(server.server_address[1])

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_two_clients_through_a_trusted_proxy_get_separate_buckets(
    proxy_for: Callable[[int], int],
) -> None:
    with gunicorn_server(trusted=PROXY_ADDR) as port:
        pport = proxy_for(port)
        statuses_a = [_get(pport, CLIENT_A)[0] for _ in range(4)]
        status_b, body_b = _get(pport, CLIENT_B)
    assert statuses_a == [200, 200, 200, 429]  # A used its three
    assert status_b == 200  # B has its own bucket
    assert body_b == {"limiter_key": CLIENT_B, "audit_ip": CLIENT_B}


def test_without_the_setting_every_client_shares_the_proxys_bucket(
    proxy_for: Callable[[int], int],
) -> None:
    # The finding (H6): the proxy is not loopback, so by default nothing it
    # says is believed, and all clients are one peer.
    with gunicorn_server(trusted=None) as port:
        pport = proxy_for(port)
        results = [_get(pport, CLIENT_A)[0] for _ in range(3)]
        status_b, _ = _get(pport, CLIENT_B)
    assert results == [200, 200, 200]
    assert status_b == 429


def test_client_supplied_header_through_an_appending_proxy_cannot_pick_a_bucket(
    proxy_for: Callable[[int], int],
) -> None:
    with gunicorn_server(trusted=PROXY_ADDR) as port:
        pport = proxy_for(port)
        outcomes = []
        for i in range(4):
            status, body = _get(pport, CLIENT_A, {"X-Forwarded-For": f"203.0.113.{i}"})
            outcomes.append((status, body.get("limiter_key"), body.get("audit_ip")))
    assert [o[0] for o in outcomes] == [200, 200, 200, 429]
    for _, key, audit in outcomes[:3]:
        assert key == audit == CLIENT_A


def test_direct_clients_header_is_ignored_when_not_trusted() -> None:
    # No proxy: the client talks to gunicorn itself from 127.0.0.2, which the
    # default (loopback only) does not trust.
    with gunicorn_server(trusted=None) as port:
        outcomes = [_get(port, CLIENT_A, {"X-Forwarded-For": f"203.0.113.{i}"}) for i in range(4)]
    assert [o[0] for o in outcomes] == [200, 200, 200, 429]
    assert all(o[1]["limiter_key"] == CLIENT_A == o[1]["audit_ip"] for o in outcomes[:3])


def test_default_trusts_loopback_proxy() -> None:
    # A proxy on the same host (127.0.0.1) is the one thing trusted with no setting.
    with gunicorn_server(trusted=None) as port:
        status, body = _get(port, "127.0.0.1", {"X-Forwarded-For": "198.51.100.7"})
    assert status == 200
    assert body == {"limiter_key": "198.51.100.7", "audit_ip": "198.51.100.7"}
