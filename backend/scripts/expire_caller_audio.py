"""Delete stored call audio that is past its workspace's retention period.

    uv run python scripts/expire_caller_audio.py --dry-run   # count, delete nothing
    uv run python scripts/expire_caller_audio.py             # delete

The backend already does this by itself (at start-up and hourly, see
``CALLER_AUDIO_EXPIRY_INTERVAL_MINUTES``). This script is an extra, for a manual pass or
a cron job:

    docker compose exec -T backend python scripts/expire_caller_audio.py

Each workspace has its own period (``audio_retention_days``, 30 unless
changed). A file that cannot be deleted is reported and kept in the database,
so the next run tries it again; the exit status is 1 when any such file is left.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def _run(dry_run: bool) -> int:
    from app.db.session import AsyncSessionLocal, engine
    from app.services.audio_store import get_audio_store
    from app.services.caller_audio import expire_audio

    try:
        async with AsyncSessionLocal() as db:
            counts = await expire_audio(db, get_audio_store(), dry_run=dry_run)
            if not dry_run:
                await db.commit()
    finally:
        await engine.dispose()

    verb = "would be deleted" if dry_run else "deleted"
    print(f"{counts['expired']} audio file(s) {verb}.")
    if counts["failed"]:
        print(f"{counts['failed']} file(s) could NOT be deleted and are still stored.")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="count only; delete nothing")
    return asyncio.run(_run(parser.parse_args().dry_run))


if __name__ == "__main__":
    sys.exit(main())
