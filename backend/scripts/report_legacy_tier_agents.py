#!/usr/bin/env python
"""How many agents are on a retired pricing tier (budget, balanced), read-only.

Run before the rule that refuses those tiers at configuration time ships, to know
how many existing agents it leaves behind and how many of them can take a call:

    uv run python scripts/report_legacy_tier_agents.py

It prints a count, how many of those agents have a phone number, and their ids.
Ids only: no names, prompts or numbers. It runs one SELECT per question and never
commits, flushes or writes, and it reads whichever database this process's own
settings point at, so run it where production is reachable.

An agent "has a phone number" by either route the inbound webhook honours:
`agent.phone_number_id`, or an active phone number assigned to it. A released or
suspended number routes nowhere and does not count.
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class LegacyTierReport:
    agent_ids: list[uuid.UUID] = field(default_factory=list)
    with_phone: int = 0
    by_tier: dict[str, int] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.agent_ids)


async def collect(session: AsyncSession) -> LegacyTierReport:
    """Read the legacy-tier agents. Issues SELECTs only."""
    from sqlalchemy import select

    from app.models.agent import Agent
    from app.models.phone_number import PhoneNumber, PhoneNumberStatus
    from app.services.realtime_models import LEGACY_TIERS

    rows = (
        await session.execute(
            select(Agent.id, Agent.pricing_tier, Agent.phone_number_id)
            .where(Agent.pricing_tier.in_(LEGACY_TIERS))
            .order_by(Agent.created_at, Agent.id)
        )
    ).all()
    assigned = set(
        (
            await session.execute(
                select(PhoneNumber.assigned_agent_id).where(
                    PhoneNumber.assigned_agent_id.in_([r.id for r in rows]),
                    PhoneNumber.status == PhoneNumberStatus.ACTIVE.value,
                )
            )
        )
        .scalars()
        .all()
    )

    report = LegacyTierReport()
    report.agent_ids = [r.id for r in rows]
    report.by_tier = dict(Counter(r.pricing_tier for r in rows))
    report.with_phone = sum(1 for r in rows if r.phone_number_id or r.id in assigned)
    return report


def render(report: LegacyTierReport) -> str:
    tiers = ", ".join(f"{tier}={n}" for tier, n in sorted(report.by_tier.items())) or "none"
    lines = [
        f"{report.count} agent(s) on a legacy pricing tier ({tiers})",
        f"{report.with_phone} of them have a phone number",
    ]
    lines += [f"  {agent_id}" for agent_id in report.agent_ids]
    return "\n".join(lines)


async def main() -> int:
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        report = await collect(session)
    print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
