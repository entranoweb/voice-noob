"""scripts/db_backup.py: failure semantics, retention, and where the password goes.

The postgres tools are replaced by small stand-ins on PATH, so this checks the
script's own decisions (exit status, what is kept, what is printed) in CI with
no server. The real thing, against a Postgres 17 server, is exercised by hand
and reported in the pull request that added the script.
"""

from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "db_backup.py"
PASSWORD = "s3cr3t-pw-value"

FAKE_PG_DUMP = f"""#!{sys.executable}
import json, os, sys
mode = os.environ.get("FAKE_MODE", "ok")
out = [a.split("=", 1)[1] for a in sys.argv if a.startswith("--file=")][0]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps({{"argv": sys.argv, "pgpassword": os.environ.get("PGPASSWORD"),
                           "database_url": os.environ.get("DATABASE_URL")}}) + "\\n")
if mode == "fail":
    sys.stderr.write("pg_dump: error: connection failed for password " + os.environ["PGPASSWORD"] + "\\n")
    sys.exit(1)
if mode == "empty":
    open(out, "w").close()
elif mode == "no-tables":
    open(out, "w").write("nothing\\n")
elif mode == "no-alembic":
    open(out, "w").write("TABLE users\\n")
else:
    open(out, "w").write("TABLE users\\nTABLE alembic_version\\n")
"""

FAKE_PG_RESTORE = f"""#!{sys.executable}
import sys
assert "--list" in sys.argv
path = sys.argv[-1]
lines = []
for n, line in enumerate(open(path).read().splitlines()):
    if line.startswith("TABLE "):
        lines.append(f"{{n}}; 1259 1000{{n}} TABLE public {{line.split()[1]}} postgres")
print("\\n".join(lines))
"""


@pytest.fixture
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("db_backup", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["db_backup"] = module  # dataclasses looks the module up by name
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("pg_dump", FAKE_PG_DUMP), ("pg_restore", FAKE_PG_RESTORE)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv(
        "DATABASE_URL", f"postgresql+asyncpg://app_user:{PASSWORD}@db.internal:5432/appdb"
    )
    for var in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE", "PGSSLMODE"):
        monkeypatch.delenv(var, raising=False)
    return log


def run(script: ModuleType, tmp_path: Path, *extra: str) -> int:
    return int(script.main(["backup", "--dir", str(tmp_path / "out"), *extra]))


def dumps(tmp_path: Path) -> list[str]:
    return sorted(p.name for p in (tmp_path / "out").glob("*"))


def test_backup_writes_a_dump_and_exits_zero(
    script: ModuleType, fakes: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(script, tmp_path) == 0
    (name,) = dumps(tmp_path)
    assert name.startswith("synthiq-appdb-")
    assert name.endswith(".dump")
    assert "backup ok" in capsys.readouterr().out
    assert ((tmp_path / "out" / name).stat().st_mode & 0o777) == 0o600


def test_password_is_in_the_environment_never_the_command_line_or_output(
    script: ModuleType, fakes: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(script, tmp_path)
    call = json.loads(fakes.read_text().splitlines()[0])
    assert call["pgpassword"] == PASSWORD
    assert PASSWORD not in " ".join(call["argv"])
    assert "db.internal" not in " ".join(call["argv"])  # connection is env-only too
    assert call["database_url"] is None  # the URL, password and all, is not passed on
    shown = capsys.readouterr()
    assert PASSWORD not in shown.out + shown.err


@pytest.mark.parametrize(
    ("mode", "expect"),
    [
        ("empty", "dump is empty"),
        ("no-tables", "no tables"),
        ("no-alembic", "alembic_version"),
        ("fail", "pg_dump failed"),
    ],
)
def test_a_bad_dump_fails_and_leaves_nothing_behind(
    script: ModuleType,
    fakes: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
    expect: str,
) -> None:
    monkeypatch.setenv("FAKE_MODE", mode)
    assert run(script, tmp_path) == script.EXIT_FAILURE
    shown = capsys.readouterr()
    assert expect in shown.err
    assert PASSWORD not in shown.out + shown.err  # scrubbed even from the tool's own error
    assert dumps(tmp_path) == []  # no .partial, no half-written dump


def test_a_failed_backup_prunes_nothing(
    script: ModuleType, fakes: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    old = [f"synthiq-appdb-2026010{n}T000000Z.dump" for n in range(1, 4)]
    for name in old:
        (out / name).write_text("x")
    monkeypatch.setenv("FAKE_MODE", "empty")
    assert run(script, tmp_path, "--keep", "1") == script.EXIT_FAILURE
    assert dumps(tmp_path) == old


def test_retention_keeps_the_newest_n_of_this_database_only(
    script: ModuleType, fakes: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    for n in range(1, 5):
        (out / f"synthiq-appdb-2026010{n}T000000Z.dump").write_text("x")
    (out / "synthiq-other-20260101T000000Z.dump").write_text("x")  # another database
    (out / "notes.txt").write_text("x")  # not ours
    assert run(script, tmp_path, "--keep", "2") == 0
    left = dumps(tmp_path)
    assert "notes.txt" in left
    assert "synthiq-other-20260101T000000Z.dump" in left
    mine = [n for n in left if n.startswith("synthiq-appdb-")]
    assert len(mine) == 2
    assert "synthiq-appdb-20260101T000000Z.dump" not in mine  # oldest went
    assert "synthiq-appdb-20260104T000000Z.dump" in mine  # newest old one stayed
    new = [n for n in mine if not n.startswith("synthiq-appdb-2026010")]
    assert len(new) == 1  # the dump just taken


def test_keep_must_be_positive(script: ModuleType, fakes: Path, tmp_path: Path) -> None:
    assert run(script, tmp_path, "--keep", "0") == script.EXIT_FAILURE
    assert dumps(tmp_path) == []


def test_no_database_named_is_a_failure(
    script: ModuleType, fakes: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL")
    assert run(script, tmp_path) == script.EXIT_FAILURE


def test_libpq_variables_win_over_database_url(script: ModuleType) -> None:
    conn = script.connection_from_environment(
        {
            "DATABASE_URL": "postgresql+asyncpg://u%40x:p%2Fw@h1:5433/db1?sslmode=require",
            "PGHOST": "h2",
        }
    )
    assert conn.env == {
        "PGHOST": "h2",
        "PGPORT": "5433",
        "PGUSER": "u@x",
        "PGPASSWORD": "p/w",
        "PGDATABASE": "db1",
        "PGSSLMODE": "require",
    }
