"""List registered phone numbers that are not on their workspace's carrier account.

    uv run python scripts/report_unverified_numbers.py --confirm-carrier-lookups

Read-only. For every live (not released) registration it asks the carrier, with
the credentials of the workspace the number is registered in, whether the
account holds the number, and prints those that registration would refuse
today. It changes nothing: no row is written, deleted or reassigned, and no
carrier call purchases, releases or configures anything. A registration that
cannot be checked (carrier down, no credentials saved) is listed with that
reason. Registrations with no workspace are listed (they cannot be checked),
and so are agents whose `phone_number_id` has no live registration (no carrier
call is needed for those).

Agents that would stop getting calls under the read-path routing rule
(registered, active, with a workspace, agent in that workspace) are listed with
the reason, so the impact is visible before the rule ships.

It makes one carrier API request per registered number using customers' saved
credentials, hence the required flag. Exit status: 0 when every number passes,
1 when any is listed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def _run() -> int:
    from app.db.session import AsyncSessionLocal, engine
    from app.services.telephony.number_ownership import (
        default_lookup_factory,
        find_unregistered_agent_numbers,
        find_unverified_numbers,
    )
    from app.services.telephony.number_routing import find_agents_that_stop_routing

    try:
        async with AsyncSessionLocal() as db:
            failures = await find_unverified_numbers(db, factory=default_lookup_factory)
            agents = await find_unregistered_agent_numbers(db)
            stopped = await find_agents_that_stop_routing(db)
            await db.rollback()  # nothing was written; make sure nothing is kept
    finally:
        await engine.dispose()

    for item in failures:
        print(
            f"workspace={item.workspace_id or '(none)'} provider={item.provider} "
            f"number={item.phone_number} record={item.phone_number_id}: {item.reason}"
        )
    print(f"{len(failures)} registered number(s) would be refused today.")
    for agent in agents:
        print(f"agent={agent.agent_id} phone_number_id={agent.reference}: no live registration")
    print(f"{len(agents)} agent(s) name a phone number with no live registration.")
    for item in stopped:
        print(f"agent={item.agent_id} stops routing: {item.reason}")
    print(f"{len(stopped)} agent(s) name a number they would no longer receive calls on.")
    return 1 if failures or agents or stopped else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--confirm-carrier-lookups",
        action="store_true",
        required=True,
        help="acknowledge that this sends one request per number to the carriers",
    )
    parser.parse_args()
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
