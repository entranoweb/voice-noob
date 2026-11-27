"""A known failure is a message and exit 2, never a traceback. No network."""

import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from synthiq_caller import cli, phone_server
from synthiq_caller.cli_guard import guarded

RUN = ["--leg", "elevenlabs-socket", "--agent", "a", "--clips", "c", "--out", "o"]


def test_run_with_missing_scenario_exits_2_without_traceback(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "synthiq_caller.cli", "run", str(tmp_path / "nope.json"), *RUN],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert result.stderr.startswith("ERROR:")


def test_run_with_missing_scenario_in_process(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["run", str(tmp_path / "nope.json"), *RUN]) == 2
    err = capsys.readouterr().err
    assert err.startswith("ERROR:") and "Traceback" not in err


def test_phone_server_missing_scenario_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = phone_server.main(
        ["--scenario", str(tmp_path / "nope.json"), "--clips", "c", "--dry-run"], env={}
    )
    assert code == 2
    assert "ERROR:" in capsys.readouterr().err


def _http_failure() -> int:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with httpx.Client(transport=httpx.MockTransport(refuse)) as client:
        client.get("https://example.test/trace")
    return 0


def test_httpx_error_is_a_message_and_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert guarded(_http_failure) == 2
    err = capsys.readouterr().err
    assert "ERROR: connection refused" in err and "Traceback" not in err


def test_debug_env_reraises_for_a_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNTHIQ_DEBUG", "1")
    with pytest.raises(httpx.ConnectError):
        guarded(_http_failure)


def test_keyboard_interrupt_is_never_swallowed() -> None:
    def interrupted() -> int:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        guarded(interrupted)


def test_pass_and_gate_codes_pass_through_unchanged() -> None:
    assert guarded(lambda: 0) == 0
    assert guarded(lambda: 1) == 1
