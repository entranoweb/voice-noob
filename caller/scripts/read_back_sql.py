"""A read-back for a system whose records are in PostgreSQL. Read-only, one scope.

The caller's batch runs this after each call (``--read-back command``, or the
``read_back`` part of a target profile). It prints one flat JSON object: what
the system holds for the caller of that call. The grader uses it as "stored".

    python scripts/read_back_sql.py --queries my-queries.json
    python scripts/read_back_sql.py --queries my-queries.json --dry-run

It knows no table and no system. The queries file says what to select; see
``scripts/read_back_queries.example.json``. Nothing about the connection is in
that file or on the command line:

    SYNTHIQ_READBACK_DSN        the connection string (a read-only role)
    SYNTHIQ_READBACK_SCOPE_ID   the one tenant, clinic or account every query is held to

What the batch supplies, as query parameters:

    %(scope_id)s             SYNTHIQ_READBACK_SCOPE_ID
    %(call_ref)s             the platform's reference for the call
    %(fact_name)s            the caller's name as the scenario states it
    %(fact_first_name)s      the name up to its last space
    %(fact_last_name)s       the name after its last space
    %(fact_date_of_birth)s, %(fact_postcode)s, %(fact_phone)s

What keeps it from changing or leaking anything:

* every query must be a single ``select`` and must use ``%(scope_id)s``;
* the session is read-only (the database refuses a write, whatever the text says);
* values reach the database as parameters, never as part of the query text;
* a ``sanity`` query must find a row first. A role that cannot see the scope gets
  zero rows from every table, and zero rows would otherwise read as "the agent
  stored nothing".

Exit codes: 0 with JSON printed; 2 the queries file or the environment is wrong;
3 the database could not be reached or the sanity query found nothing. On
anything but 0 the batch records a failed read-back and "stored" falls back to
what the agent's tool was sent, labelled as such.

A query that finds no row answers every one of its ``columns`` with ``null``:
the system was asked and holds nothing. The grader counts that as not stored.
A query whose ``needs`` are not all known is skipped and answers nothing.

Needs the ``psycopg`` package (``pip install "psycopg[binary]"``); it is not a
dependency of the caller, which never talks to a database itself.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from typing import Any

FACTS = ("name", "date_of_birth", "postcode", "phone")
_PARAMETER = re.compile(r"%\((\w+)\)s")
_SETTING = re.compile(r"[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?")
_COLUMN = re.compile(r"[a-z_][a-z0-9_]*")
# A second line of defence only; the read-only session is the first.
_WRITES = re.compile(
    r"\b(insert|update|delete|merge|truncate|drop|alter|create|grant|revoke|copy|call|do|"
    r"into|lock|vacuum|refresh|reindex|set|reset|listen|notify|execute|prepare)\b",
    re.IGNORECASE,
)


class Refused(ValueError):
    """The queries file or the environment is not fit to run."""


def parameters(env: Mapping[str, str]) -> dict[str, str]:
    """Everything a query may name, from the environment the batch gives a read-back."""
    name = env.get("SYNTHIQ_FACT_NAME", "").strip()
    first, _, last = name.rpartition(" ")
    values = {
        "scope_id": env.get("SYNTHIQ_READBACK_SCOPE_ID", "").strip(),
        "call_ref": env.get("SYNTHIQ_CALL_REF", "").strip(),
        "fact_first_name": first,
        "fact_last_name": last,
    }
    values.update(
        {f"fact_{fact}": env.get(f"SYNTHIQ_FACT_{fact.upper()}", "").strip() for fact in FACTS}
    )
    return values


def _check_sql(label: str, sql: Any, known: set[str]) -> str:
    if not isinstance(sql, str) or not sql.strip():
        raise Refused(f"{label}: needs a query")
    text = sql.strip()
    if not text.lower().startswith("select") or ";" in text or "--" in text or "/*" in text:
        raise Refused(f"{label}: one plain select, no ';' and no comments")
    written = _WRITES.search(text)
    if written:
        raise Refused(f"{label}: {written[0]!r} has no place in a read-back")
    used = set(_PARAMETER.findall(text))
    if "scope_id" not in used:
        raise Refused(f"{label}: must be held to the scope with %(scope_id)s")
    if used - known:
        raise Refused(f"{label}: unknown parameters {sorted(used - known)}")
    if "%" in _PARAMETER.sub("", text):
        raise Refused(f"{label}: a bare % is not allowed; values go in as %(name)s")
    return text


def load_queries(data: Any) -> dict[str, Any]:
    """Check a queries file. Raises ``Refused`` with the reason it will not be run."""
    if not isinstance(data, dict):
        raise Refused("the queries file is one JSON object")
    unknown = sorted(set(data) - {"notes", "settings", "sanity", "queries", "timeout_ms"})
    if unknown:
        raise Refused(f"the queries file has unknown parts: {unknown}")
    known = set(parameters({}))
    settings = data.get("settings") or {}
    if not isinstance(settings, dict):
        raise Refused("'settings' maps a setting's name to the parameter it is given")
    for setting, source in settings.items():
        if not _SETTING.fullmatch(str(setting)) or source not in known:
            raise Refused(
                f"settings: {setting!r} must be a plain name given one of {sorted(known)}"
            )
    queries = data.get("queries")
    if not isinstance(queries, list) or not queries:
        raise Refused("'queries' is a list with at least one query")
    checked = []
    seen: set[str] = set()
    for index, raw in enumerate(queries):
        label = f"queries[{index}]"
        if not isinstance(raw, dict) or set(raw) - {"name", "sql", "columns", "needs"}:
            raise Refused(f"{label}: an object with 'sql', 'columns' and, if wanted, 'needs'")
        columns = raw.get("columns")
        if (
            not isinstance(columns, list)
            or not columns
            or not all(isinstance(c, str) and _COLUMN.fullmatch(c) for c in columns)
        ):
            raise Refused(f"{label}: 'columns' lists the keys it answers with, in plain names")
        if seen & set(columns):
            raise Refused(
                f"{label}: {sorted(seen & set(columns))} are answered by an earlier query"
            )
        seen |= set(columns)
        needs = raw.get("needs") or []
        if not isinstance(needs, list) or set(needs) - known:
            raise Refused(f"{label}: 'needs' may name only {sorted(known)}")
        checked.append(
            {
                "name": str(raw.get("name") or label),
                "sql": _check_sql(label, raw.get("sql"), known),
                "columns": list(columns),
                "needs": [str(n) for n in needs],
            }
        )
    timeout = data.get("timeout_ms", 10000)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 0 < timeout <= 60000:
        raise Refused("'timeout_ms' is a whole number of milliseconds, at most 60000")
    return {
        "settings": {str(k): str(v) for k, v in settings.items()},
        "sanity": _check_sql("sanity", data.get("sanity"), known),
        "queries": checked,
        "timeout_ms": timeout,
    }


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, dt.datetime | dt.date | dt.time):
        # As the database holds it. A date kept in a timestamp column is cast in
        # the query (``::date``), not guessed at here.
        return value.isoformat()
    return str(value)


def read(connection: Any, spec: dict[str, Any], values: dict[str, str]) -> dict[str, Any]:
    """Run the checked queries on an open, read-only connection."""
    held: dict[str, Any] = {}
    with connection.cursor() as cursor:
        cursor.execute(
            "select set_config('statement_timeout', %s, true)", (str(spec["timeout_ms"]),)
        )
        for setting, source in spec["settings"].items():
            cursor.execute("select set_config(%s, %s, true)", (setting, values[source]))
        cursor.execute(spec["sanity"], values)
        if cursor.fetchone() is None:
            raise LookupError(
                "the sanity query found nothing: the scope id is wrong, or this role cannot "
                "see it. Nothing was read, because every answer would have been 'not stored'."
            )
        for query in spec["queries"]:
            if any(not values.get(need) for need in query["needs"]):
                continue  # not asked: the grader falls back for these keys, and says so
            cursor.execute(query["sql"], values)
            row = cursor.fetchone()
            names = [column.name for column in cursor.description or ()]
            found = dict(zip(names, row, strict=False)) if row is not None else {}
            if row is not None and set(query["columns"]) - set(found):
                lacking = sorted(set(query["columns"]) - set(found))
                raise LookupError(f"{query['name']}: the select does not return {lacking}")
            for column in query["columns"]:
                held[column] = _plain(found.get(column))
    return held


def _connect(dsn: str) -> Any:
    try:
        import psycopg  # type: ignore[import-not-found,unused-ignore]
    except ImportError as error:
        raise LookupError('the psycopg package is needed: pip install "psycopg[binary]"') from error
    connection = psycopg.connect(dsn, connect_timeout=10)
    connection.read_only = True
    return connection


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    connect: Callable[[str], Any] = _connect,
) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--queries", required=True, help="the JSON file of queries to run")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="check the queries file and print what would be run; connects to nothing",
    )
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    try:
        with open(args.queries, encoding="utf-8") as handle:
            spec = load_queries(json.load(handle))
        values = parameters(env)
        if args.dry_run:
            print("DRY RUN: connects to nothing.")
            given = {key: bool(value) for key, value in values.items()}
            print(f"parameters given: {json.dumps(given, sort_keys=True)}")
            print(f"connection string set: {bool(env.get('SYNTHIQ_READBACK_DSN'))}")
            for setting, source in spec["settings"].items():
                print(f"setting {setting} = %({source})s")
            print(f"sanity: {spec['sanity']}")
            for query in spec["queries"]:
                print(f"{query['name']} -> {query['columns']} needs {query['needs']}:")
                print(f"  {query['sql']}")
            return 0
        if not values["scope_id"]:
            raise Refused("SYNTHIQ_READBACK_SCOPE_ID is not set: no scope, no read-back")
        dsn = env.get("SYNTHIQ_READBACK_DSN", "")
        if not dsn:
            raise Refused("SYNTHIQ_READBACK_DSN is not set")
    except (OSError, ValueError) as error:
        print(f"read-back refused: {error}", file=sys.stderr)
        return 2
    try:
        connection = connect(dsn)
        try:
            held = read(connection, spec, values)
        finally:
            connection.close()
    except Exception as error:  # noqa: BLE001 - whatever the driver raises, say it and stop
        # The driver's message can quote the connection string; only its kind is printed.
        detail = str(error) if isinstance(error, LookupError) else type(error).__name__
        print(f"read-back failed: {detail}", file=sys.stderr)
        return 3
    print(json.dumps(held, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
