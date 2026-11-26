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
if mode != "empty":
    with open(out, "a") as dump:
        dump.write("DBNAME " + os.environ.get("FAKE_DUMP_DB", os.environ.get("PGDATABASE", "")) + "\\n")
"""

FAKE_PG_RESTORE = f"""#!{sys.executable}
import sys
path = sys.argv[-1]
if "--list" not in sys.argv:
    sys.exit(0)  # a restore into the scratch database: nothing to do
lines = []
for n, line in enumerate(open(path).read().splitlines()):
    if line.startswith("DBNAME "):
        lines.insert(0, ";     dbname: " + line.split(" ", 1)[1])
    if line.startswith("TABLE "):
        lines.append(f"{{n}}; 1259 1000{{n}} TABLE public {{line.split()[1]}} postgres")
print("\\n".join(lines))
"""

# Answers the queries the script makes. FAKE_LIVE / FAKE_RESTORED are JSON
# {table: rows} for the live database and for the restore_check_* scratch one.
FAKE_PSQL = f"""#!{sys.executable}
import json, os, sys
sql = sys.argv[sys.argv.index("-c") + 1]
db = os.environ.get("PGDATABASE", "")
if sql.startswith(("CREATE DATABASE", "DROP DATABASE")):
    sys.exit(0)
key = "FAKE_RESTORED" if db.startswith("restore_check_") else "FAKE_LIVE"
counts = json.loads(os.environ.get(key, '{{"users": 3, "alembic_version": 1}}'))
if "count(*)" not in sql:
    print("rev_1")
else:
    for name, n in counts.items():
        print(f"{{name}}\\t{{n}}")
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
    for name, body in (
        ("pg_dump", FAKE_PG_DUMP),
        ("pg_restore", FAKE_PG_RESTORE),
        ("psql", FAKE_PSQL),
    ):
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


def test_a_migrated_but_empty_database_is_refused_unless_asked(
    script: ModuleType,
    fakes: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FAKE_LIVE", json.dumps({"users": 0, "alembic_version": 1}))
    assert run(script, tmp_path) == script.EXIT_FAILURE
    assert "no rows outside alembic_version" in capsys.readouterr().err
    assert dumps(tmp_path) == []
    assert run(script, tmp_path, "--allow-empty") == 0
    assert len(dumps(tmp_path)) == 1


def test_a_dump_from_another_database_is_refused(
    script: ModuleType,
    fakes: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FAKE_DUMP_DB", "other_app")
    assert run(script, tmp_path) == script.EXIT_FAILURE
    assert "other_app" in capsys.readouterr().err
    assert dumps(tmp_path) == []


def check(script: ModuleType, dump: Path, *extra: str) -> int:
    return int(script.main(["restore-check", str(dump), *extra]))


def test_restore_check_passes_when_the_copy_matches(
    script: ModuleType, fakes: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(script, tmp_path) == 0
    (name,) = dumps(tmp_path)
    assert check(script, tmp_path / "out" / name) == 0
    assert "restore check ok" in capsys.readouterr().out


def test_restore_check_of_an_empty_dump_fails_even_with_allow_drift(
    script: ModuleType,
    fakes: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(script, tmp_path) == 0
    (name,) = dumps(tmp_path)
    monkeypatch.setenv("FAKE_LIVE", json.dumps({"users": 100, "alembic_version": 1}))
    monkeypatch.setenv("FAKE_RESTORED", json.dumps({"users": 0, "alembic_version": 1}))
    assert check(script, tmp_path / "out" / name, "--allow-drift") == script.EXIT_FAILURE
    assert "no rows outside alembic_version" in capsys.readouterr().err
    assert check(script, tmp_path / "out" / name) == script.EXIT_FAILURE


def test_restore_check_refuses_another_applications_dump(
    script: ModuleType,
    fakes: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PGDATABASE", "other_app")
    assert run(script, tmp_path) == 0  # that application's own backup is fine
    (name,) = dumps(tmp_path)
    monkeypatch.setenv("PGDATABASE", "appdb")
    assert check(script, tmp_path / "out" / name) == script.EXIT_FAILURE
    assert "other_app" in capsys.readouterr().err


def _freeze_the_clock(script: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]  # noqa: ARG003
            return datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    monkeypatch.setattr(script, "datetime", Frozen)


def test_a_losing_backup_does_not_delete_the_winners_file(
    script: ModuleType,
    fakes: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Run B starts and finishes while run A's partial exists; A then loses cleanly."""
    _freeze_the_clock(script, monkeypatch)
    out = tmp_path / "out"
    real = script.verify_dump
    state = {"inner": False}

    def verify_while_the_other_runs(runner, path, database):  # type: ignore[no-untyped-def]
        if not state["inner"]:
            state["inner"] = True
            assert script.main(["backup", "--dir", str(out)]) == 0
        return real(runner, path, database)

    monkeypatch.setattr(script, "verify_dump", verify_while_the_other_runs)
    assert run(script, tmp_path) == script.EXIT_FAILURE  # the outer run finds the name taken
    assert "already exists" in capsys.readouterr().err
    assert dumps(tmp_path) == ["synthiq-appdb-20260101T120000Z.dump"]
    files = [
        a
        for line in fakes.read_text().splitlines()
        for a in json.loads(line)["argv"]
        if a.startswith("--file=")
    ]
    assert len(files) == 2
    assert files[0] != files[1]  # each run wrote its own partial
