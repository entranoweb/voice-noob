"""Settings read the env file SETTINGS_ENV_FILE names, and none when it is empty.

The suite runs with it empty (tests/conftest.py). Before that, a developer's own
backend/.env changed what tests saw: the quick start writes a local PUBLIC_URL
there, and the inbound-call test that checks the stream address failed on it.

Settings are built once, at import, so each case runs in its own interpreter.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
PRINT_PUBLIC_URL = "from app.core.config import settings; print(settings.PUBLIC_URL)"


def _public_url_seen_from(directory: Path, **overrides: str) -> str:
    env = {
        **os.environ,
        "PYTHONPATH": str(BACKEND),
        "SECRET_KEY": "test-only-signing-key-never-used-outside-the-test-suite",
        "CREDENTIAL_ENCRYPTION_KEYS": "test:dGVzdC1vbmx5LWtleS0wMTIzNDU2Nzg5YWJjZGVmISE=",
        **overrides,
    }
    env.pop("PUBLIC_URL", None)
    if "SETTINGS_ENV_FILE" not in overrides:
        env.pop("SETTINGS_ENV_FILE", None)
    result = subprocess.run(  # noqa: S603 - fixed interpreter and fixed code
        [sys.executable, "-c", PRINT_PUBLIC_URL],
        cwd=directory,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return result.stdout.strip()


@pytest.fixture
def directory_with_env_file(tmp_path: Path) -> Path:
    (tmp_path / ".env").write_text("PUBLIC_URL=https://from-the-file.example\n", encoding="utf-8")
    return tmp_path


def test_the_env_file_is_read_by_default(directory_with_env_file: Path) -> None:
    assert _public_url_seen_from(directory_with_env_file) == "https://from-the-file.example"


def test_an_empty_setting_reads_no_env_file(directory_with_env_file: Path) -> None:
    assert _public_url_seen_from(directory_with_env_file, SETTINGS_ENV_FILE="") == "None"


def test_the_suite_itself_reads_no_env_file() -> None:
    from app.core.config import settings

    assert settings.model_config.get("env_file") is None
