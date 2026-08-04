"""Background workers started on server startup.

Mail ingest and attachment text extraction belong to the agent now, so the only
thing left here is the contacts rollup — pure derived data, rebuilt from rows the
agent has already written.
"""

from __future__ import annotations

import asyncio

from core.config import get_settings
from core.database import SessionLocal

from .contacts import rebuild_contact_pairs, rebuild_contacts

settings = get_settings()


def _rebuild_contacts_once() -> int:
    db = SessionLocal()
    try:
        count = rebuild_contacts(db, settings.contacts_scan_years)
        # Same window, same pass: the co-recipient ranking divides by the totals
        # in `contacts`, so the two tables have to describe the same mail.
        rebuild_contact_pairs(db, settings.contacts_scan_years)
        return count
    finally:
        db.close()


async def contacts_loop() -> None:
    # Build once at startup (so autocomplete works immediately), then refresh
    # periodically to pick up newly-synced mail.
    while True:
        count = 0
        try:
            count = await asyncio.to_thread(_rebuild_contacts_once)
        except Exception as e:  # noqa: BLE001
            # Never let a bad rebuild kill the loop, but don't swallow it either:
            # a silent failure here looks exactly like "autocomplete is broken".
            print(f"contacts rebuild failed: {e!r}", flush=True)
        # A fresh install starts the app before the agent has written its first
        # message, so the startup rebuild finds nothing. Sleeping the full period
        # on that result would leave autocomplete dead for the rest of the day;
        # poll quickly until there is actually something to build from.
        await asyncio.sleep(6 * 3600 if count else 60)
