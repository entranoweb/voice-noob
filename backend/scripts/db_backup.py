"""Back up the Postgres database, and prove a backup restores.

    python scripts/db_backup.py backup [--dir DIR] [--keep N] [--allow-empty]
    python scripts/db_backup.py restore-check FILE [--allow-drift] [--allow-empty]

Nothing runs this on a schedule. See DEPLOYMENT.md, "Database backup and
restore", for what to run, how often, and what a backup does not contain.

backup
    ``pg_dump`` in custom format into DIR (default ``$BACKUP_DIR`` or
    ``./data/backups``), written to a ``.partial`` file, checked, and only then
    renamed to ``synthiq-<database>-<UTC timestamp>.dump``. The partial name
    carries a random suffix, so two runs started in the same second never share
    a file. The check: the dump is not empty, ``pg_restore --list`` can read it,
    it was taken from the database that was asked for, it holds at least one
    table including ``alembic_version``, and the database has at least one row
    outside ``alembic_version`` (a migrated but empty database is refused unless
    ``--allow-empty``: the usual cause is the wrong database, not a new one).
    A dump that fails here is found now, not at 3 a.m. on the day it is needed. Afterwards all but the
    newest ``--keep`` (default 14) dumps of this database are deleted; nothing is
    deleted unless the new dump passed.

restore-check
    Restores FILE into a throwaway database on the same server, then compares it
    with the live database: the alembic revision and the row count of every
    table in the ``public`` schema. It refuses a dump taken from a database of
    another name than the live one, and a dump with no rows outside
    ``alembic_version`` (unless ``--allow-empty``), even with ``--allow-drift``:
    an empty restore must never read as a pass. Drops the throwaway database afterwards,
    whatever happened. Counts differ if the live database was written to after
    the dump was taken; run it straight after a backup, or pass
    ``--allow-drift`` to accept row-count differences (the revision must still
    match). The database user needs CREATEDB.

Connection. Never on a command line and never printed: the password reaches
``pg_dump`` and friends through the environment (``PGPASSWORD``). Settings come
from the libpq variables (``PGHOST``, ``PGPORT``, ``PGUSER``, ``PGPASSWORD``,
``PGDATABASE``) and, where those are not set, from ``DATABASE_URL`` (the
``+asyncpg`` driver suffix is ignored). Output from the tools is scrubbed of the
password before it is shown.

Exit status: 0 only when everything succeeded. 1 when a restore check found a
difference. 2 for any other failure.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, unquote, urlparse

if TYPE_CHECKING:
    from collections.abc import Mapping

DEFAULT_KEEP = 14
DEFAULT_DIR = "./data/backups"
BACKUP_NAME = re.compile(r"^synthiq-(?P<db>.+)-(?P<ts>\d{8}T\d{6}Z)\.dump$")
DUMP_DATABASE = re.compile(r"^;\s+dbname:\s*(?P<db>.+?)\s*$", re.IGNORECASE | re.MULTILINE)
EXIT_MISMATCH = 1
EXIT_FAILURE = 2


class BackupError(Exception):
    """Something failed; the message is safe to print."""


class MismatchError(BackupError):
    """The restored copy differs from the live database."""


@dataclass(frozen=True)
class Connection:
    env: dict[str, str]

    @property
    def database(self) -> str:
        return self.env.get("PGDATABASE", "")

    @property
    def password(self) -> str:
        return self.env.get("PGPASSWORD", "")

    def with_database(self, name: str) -> Connection:
        return Connection({**self.env, "PGDATABASE": name})


def connection_from_environment(environ: Mapping[str, str]) -> Connection:
    """libpq variables, then DATABASE_URL for what they leave unset."""
    values: dict[str, str] = {}
    url = environ.get("DATABASE_URL", "").strip()
    if url:
        parsed = urlparse(url)
        if not parsed.scheme.startswith("postgres"):
            raise BackupError("DATABASE_URL is not a postgres URL")
        if parsed.hostname:
            values["PGHOST"] = parsed.hostname
        if parsed.port:
            values["PGPORT"] = str(parsed.port)
        if parsed.username:
            values["PGUSER"] = unquote(parsed.username)
        if parsed.password:
            values["PGPASSWORD"] = unquote(parsed.password)
        if parsed.path.strip("/"):
            values["PGDATABASE"] = unquote(parsed.path.strip("/"))
        sslmode = parse_qs(parsed.query).get("sslmode")
        if sslmode:
            values["PGSSLMODE"] = sslmode[0]
    for key in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE", "PGSSLMODE"):
        if environ.get(key):
            values[key] = environ[key]
    if not values.get("PGDATABASE"):
        raise BackupError("no database named: set PGDATABASE or DATABASE_URL")
    return Connection(values)


class Runner:
    """Runs the postgres client tools with the password in the environment only."""

    def __init__(self, conn: Connection) -> None:
        self.conn = conn
        child = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL",)}
        child.update(conn.env)
        child.setdefault("PGCONNECT_TIMEOUT", "10")
        child["PGAPPNAME"] = "synthiq-db-backup"
        self._env = child

    def scrub(self, text: str) -> str:
        secret = self.conn.password
        return text.replace(secret, "***") if secret else text

    def run(
        self, argv: list[str], *, database: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = dict(self._env)
        if database:
            env["PGDATABASE"] = database
        try:
            return subprocess.run(  # noqa: S603 - fixed argv, no shell
                argv, env=env, capture_output=True, text=True, check=False
            )
        except FileNotFoundError as exc:
            raise BackupError(f"{argv[0]} is not installed or not on PATH") from exc

    def check(self, argv: list[str], what: str, *, database: str | None = None) -> str:
        done = self.run(argv, database=database)
        if done.returncode != 0:
            detail = self.scrub((done.stderr or "").strip())[-1500:]
            raise BackupError(f"{what} failed (exit {done.returncode}): {detail}")
        return done.stdout

    def psql(self, sql: str, *, database: str | None = None) -> str:
        return self.check(
            [
                "psql",
                "--no-psqlrc",
                "--no-password",
                "-v",
                "ON_ERROR_STOP=1",
                "-At",
                "-F",
                "\t",
                "-c",
                sql,
            ],
            "psql",
            database=database,
        )


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def backups_for(directory: Path, database: str) -> list[Path]:
    """This database's finished backups, oldest first."""
    found = []
    for path in directory.glob("synthiq-*.dump"):
        match = BACKUP_NAME.match(path.name)
        if match and match.group("db") == database:
            found.append(path)
    return sorted(found, key=lambda p: p.name)


def verify_dump(runner: Runner, path: Path, database: str) -> int:
    """The dump is non-empty, readable, from ``database``, and holds tables.

    Returns the table count.
    """
    if not path.exists():
        raise BackupError(f"no such file: {path.name}")
    if path.stat().st_size == 0:
        raise BackupError("the dump is empty")
    listing = runner.check(["pg_restore", "--list", str(path)], "reading the dump back")
    source = DUMP_DATABASE.search(listing)
    if source is None:
        raise BackupError("the dump does not say which database it came from")
    if source.group("db") != database:
        raise BackupError(
            f"the dump was taken from database {source.group('db')!r}, not {database!r}"
        )
    tables = re.findall(r"^\d+; \d+ \d+ TABLE public (\S+) ", listing, flags=re.MULTILINE)
    if not tables:
        raise BackupError("the dump contains no tables")
    if "alembic_version" not in tables:
        raise BackupError("the dump has no alembic_version table: not this application's database")
    return len(tables)


def data_rows(counts: Mapping[str, int]) -> int:
    """Rows that are the application's own: a migrated database has one in alembic_version."""
    return sum(n for name, n in counts.items() if name != "alembic_version")


def backup(conn: Connection, directory: Path, keep: int, *, allow_empty: bool = False) -> Path:
    if keep < 1:
        raise BackupError("--keep must be at least 1")
    runner = Runner(conn)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    final = directory / f"synthiq-{conn.database}-{stamp}.dump"
    # Unique per run: a fixed name is shared by two runs in the same second, and
    # one run's cleanup would delete the other's file.
    partial = final.with_name(f"{final.name}.{secrets.token_hex(6)}.partial")
    if final.exists():
        raise BackupError(f"{final.name} already exists; try again in a second")
    old_umask = os.umask(0o077)
    try:
        try:
            runner.check(
                ["pg_dump", "--format=custom", "--no-password", f"--file={partial}"],
                "pg_dump",
            )
            tables = verify_dump(runner, partial, conn.database)
            if not allow_empty and data_rows(table_counts(runner, conn.database)) == 0:
                raise BackupError(  # noqa: TRY301
                    f"{conn.database} has no rows outside alembic_version: refusing to keep "
                    "an empty backup (wrong database? pass --allow-empty if it is new)"
                )
        except BaseException:
            with contextlib.suppress(OSError):
                partial.unlink()
            raise
        try:
            os.link(partial, final)  # fails if final appeared meanwhile; never overwrites
        except FileExistsError:
            partial.unlink()
            raise BackupError(f"{final.name} already exists; try again in a second") from None
        partial.unlink()
    finally:
        os.umask(old_umask)
    size = final.stat().st_size
    print(f"backup ok: {final} ({size} bytes, {tables} tables)")

    stale = backups_for(directory, conn.database)[:-keep]
    failures = []
    for path in stale:
        try:
            path.unlink()
            print(f"pruned: {path.name}")
        except OSError as exc:
            failures.append(f"{path.name}: {exc.strerror}")
    if failures:
        raise BackupError("backup ok, but old backups could not be removed: " + "; ".join(failures))
    return final


_COUNT_SQL = """
SELECT table_name,
       (xpath('/row/c/text()',
              query_to_xml(format('SELECT count(*) AS c FROM %I.%I', table_schema, table_name),
                           false, true, '')))[1]::text
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
ORDER BY table_name
"""


def table_counts(runner: Runner, database: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in runner.psql(_COUNT_SQL, database=database).splitlines():
        name, _, number = line.partition("\t")
        counts[name] = int(number)
    return counts


def alembic_revision(runner: Runner, database: str) -> str:
    out = runner.psql("SELECT version_num FROM alembic_version", database=database).strip()
    if not out:
        raise BackupError(f"{database}: alembic_version is empty")
    return out


def restore_check(
    conn: Connection, path: Path, *, allow_drift: bool, allow_empty: bool = False
) -> None:
    runner = Runner(conn)
    live = conn.database
    verify_dump(runner, path, live)
    scratch = f"restore_check_{secrets.token_hex(6)}"
    created = False
    try:
        runner.psql(f"CREATE DATABASE {_quote_ident(scratch)}", database=live)
        created = True
        runner.check(
            [
                "pg_restore",
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                "--no-password",
                f"--dbname={scratch}",  # a name, never a URL: pg_restore ignores PGDATABASE
                str(path),
            ],
            "pg_restore",
            database=scratch,
        )
        restored_rev = alembic_revision(runner, scratch)
        live_rev = alembic_revision(runner, live)
        restored = table_counts(runner, scratch)
        current = table_counts(runner, live)
    finally:
        if created:
            try:
                runner.psql(
                    f"DROP DATABASE IF EXISTS {_quote_ident(scratch)} WITH (FORCE)", database=live
                )
            except BackupError as exc:
                print(
                    f"warning: could not drop throwaway database {scratch}: {exc}", file=sys.stderr
                )

    problems = []
    if restored_rev != live_rev:
        problems.append(f"alembic revision: backup {restored_rev}, live {live_rev}")
    for name in sorted(set(restored) | set(current)):
        before, after = restored.get(name), current.get(name)
        if before != after:
            problems.append(f"table {name}: backup {before}, live {after}")
    if not restored:
        raise BackupError("the restored database has no tables")
    if not allow_empty and data_rows(restored) == 0:
        raise BackupError(
            "the dump has no rows outside alembic_version: an empty backup is not a backup "
            "(pass --allow-empty if the database was empty on purpose)"
        )
    rows = sum(restored.values())
    if problems:
        revision_problems = [p for p in problems if p.startswith("alembic")]
        if revision_problems or not allow_drift:
            raise MismatchError(
                "restored copy differs from the live database:\n  " + "\n  ".join(problems)
            )
        print("row counts differ from the live database (allowed by --allow-drift):")
        for problem in problems:
            print(f"  {problem}")
    print(f"restore check ok: revision {restored_rev}, {len(restored)} tables, {rows} rows")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="command", required=True)
    p_backup = sub.add_parser("backup", help="dump the database")
    p_backup.add_argument("--dir", default=os.environ.get("BACKUP_DIR", DEFAULT_DIR))
    p_backup.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="newest dumps to keep")
    p_check = sub.add_parser(
        "restore-check", help="restore a dump into a throwaway database and compare"
    )
    p_backup.add_argument("--allow-empty", action="store_true", help="keep a dump with no rows")
    p_check.add_argument("file")
    p_check.add_argument("--allow-empty", action="store_true", help="accept a dump with no rows")
    p_check.add_argument("--allow-drift", action="store_true", help="accept row-count differences")
    args = parser.parse_args(argv)

    try:
        if shutil.which("pg_dump") is None:
            raise BackupError("pg_dump is not installed or not on PATH")  # noqa: TRY301
        conn = connection_from_environment(os.environ)
        if args.command == "backup":
            backup(conn, Path(args.dir), args.keep, allow_empty=args.allow_empty)
        else:
            restore_check(
                conn, Path(args.file), allow_drift=args.allow_drift, allow_empty=args.allow_empty
            )
    except MismatchError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return EXIT_MISMATCH
    except BackupError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    return 0


if __name__ == "__main__":
    sys.exit(main())
