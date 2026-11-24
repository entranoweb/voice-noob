"""Move every stored credential to the active encryption key.

Rotation, start to finish:

1. Generate a key and put it FIRST in CREDENTIAL_ENCRYPTION_KEYS, keeping the
   old one behind it:  ``k2:<new key>,k1:<old key>``
2. Restart the application. New writes use ``k2``; old rows still read.
3. Run this script:  ``uv run python scripts/rotate_credential_keys.py``
4. When it reports nothing left under ``k1`` (or ``plain``), drop ``k1`` from
   the setting and restart.

Dropping a key while rows still name it makes those credentials unreadable.
``--check`` reports what sits under which key and changes nothing.

Also the way to encrypt rows that are still plain text, for an installation
that ran the migration before it had a key.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def _run(check_only: bool) -> int:
    from app.core.credential_store import count_by_key, encrypt_to_active_key
    from app.core.secret_box import get_box
    from app.db.session import engine

    box = get_box()
    if box is None:
        print("CREDENTIAL_ENCRYPTION_KEYS is not set; nothing can be done.")
        return 2

    async with engine.begin() as connection:
        before = await connection.run_sync(count_by_key)
        print(f"Active key: {box.active_key_id}. Stored values by key: {before or 'none'}")
        if check_only:
            return 0
        changed = await connection.run_sync(encrypt_to_active_key)
        after = await connection.run_sync(count_by_key)
    await engine.dispose()

    print(f"Rows rewritten: {changed}")
    print(f"Stored values by key now: {after or 'none'}")
    stale = {key: count for key, count in after.items() if key != box.active_key_id}
    if stale:
        print(f"Still not under the active key: {stale}")
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="report only; change nothing")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.check)))


if __name__ == "__main__":
    main()
