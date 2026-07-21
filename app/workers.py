"""Background workers started on server startup.

Mail ingest and attachment text extraction belong to the agent now, so the only
thing left here is the contacts rollup — pure derived data, rebuilt from rows the
agent has already written.
"""

from __future__ import annotations

import asyncio

from core.config import get_settings
from core.database import SessionLocal

from .contacts import rebuild_contacts

settings = get_settings()


def _rebuild_contacts_once() -> int:
    db = SessionLocal()
    try:
        return rebuild_contacts(db, settings.contacts_scan_years)
    finally:
        db.close()


async def contacts_loop() -> None:
    # Build once at startup (so autocomplete works immediately), then refresh
    # periodically to pick up newly-synced mail.
    while True:
        try:
            await asyncio.to_thread(_rebuild_contacts_once)
        except Exception:
            pass
        await asyncio.sleep(6 * 3600)
