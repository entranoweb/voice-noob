"""Fail when the models and the migrated database disagree in a new way.

Run after `alembic upgrade head`, against the database named by DATABASE_URL:

    uv run python scripts/check_migration_drift.py            # the gate
    uv run python scripts/check_migration_drift.py --print    # list every difference

The reasoning, and what counts as a difference, is in `app/db/migration_drift.py`.
"""

from __future__ import annotations

import asyncio
import importlib
import pkgutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.ext.asyncio import create_async_engine

import app.models
from app.core.config import settings
from app.db.base import Base
from app.db.migration_drift import compare_with_baseline, drift_keys, parse_baseline

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

BASELINE = Path(__file__).resolve().parent.parent / "migrations" / "drift-baseline.txt"


def _differences(connection: Connection) -> list[Any]:
    context = MigrationContext.configure(connection, opts={"compare_type": True})
    return list(compare_metadata(context, Base.metadata))


async def _found() -> list[str]:
    # Every model module, or a table that is not imported looks like one to drop.
    for module in pkgutil.iter_modules(app.models.__path__):
        importlib.import_module(f"{app.models.__name__}.{module.name}")

    engine = create_async_engine(str(settings.DATABASE_URL))
    try:
        async with engine.connect() as connection:
            return drift_keys(await connection.run_sync(_differences))
    finally:
        await engine.dispose()


def main() -> int:
    found = asyncio.run(_found())

    if "--print" in sys.argv[1:]:
        print("\n".join(found))
        return 0

    report = compare_with_baseline(found, parse_baseline(BASELINE.read_text(encoding="utf-8")))
    print(f"{len(report.known)} known difference(s) listed in migrations/drift-baseline.txt")

    if report.new:
        print(f"\n{len(report.new)} NEW difference(s) between the models and the migrations:")
        for key in report.new:
            print(f"  {key}")
        print(
            "\nA model changed without a migration, or a migration without a model. "
            "Write the migration (`uv run alembic revision --autogenerate`) or fix the model. "
            "Do not add the line to the baseline to make this pass."
        )
    if report.fixed:
        print(f"\n{len(report.fixed)} baseline line(s) no longer match anything:")
        for key in report.fixed:
            print(f"  {key}")
        print(
            "\nThat difference has been fixed. Delete the line from "
            "migrations/drift-baseline.txt so it cannot come back unnoticed."
        )
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
