"""Background workers started on server startup.

Mail ingest and attachment text extraction belong to the agent now, so what is
left here is what the app owes on its own account: the contacts rollup — pure
derived data, rebuilt from rows the agent has already written — and the reminder
tick, which is the only thing in meerail that has to happen at a particular
moment rather than in response to a request.
"""

from __future__ import annotations

import asyncio
import os

from core.config import get_settings
from core.database import SessionLocal

from . import reminders
from .contacts import rebuild_contact_pairs, rebuild_contacts

settings = get_settings()


def _rebuild_contacts_once() -> int:
    db = SessionLocal()
    try:
        count = rebuild_contacts(db, settings.contacts_scan_years)
        # Same window, same pass: the co-recipient ranking divides by the totals
        # in `contacts`, so the two tables have to describe the same mail.
        rebuild_contact_pairs(db, settings.contacts_scan_years)
        # One commit for both. Each table is emptied and refilled, so a commit
        # in between publishes a `contacts` full of new totals beside a
        # `contact_pairs` that is empty or still describes the last window —
        # which is what the composer would rank against for however long the
        # second half takes over a large mailbox. Committing once means readers
        # see the old pair of tables or the new pair, never one of each; a
        # failure halfway rolls the whole rebuild back and the next tick redoes
        # it.
        db.commit()
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


# How often the due reminders are looked for. A minute is as late as a reminder
# can be, which is well inside the resolution anyone sets one at — the presets
# are whole hours — and the query behind it is one index scan over rows that are
# still pending, so running it every minute forever costs nothing worth saving.
#
# Overridable only so the test stack can watch a reminder come back without
# sitting out a minute of wall clock (docker-compose.test.yml). Nothing in the
# UI or the config file exposes it: a shorter tick buys an install nothing, and
# a longer one is just a later reminder.
REMINDER_TICK = max(1, int(os.environ.get("MEERAIL_REMINDER_TICK") or 60))


def _fire_due_reminders() -> int:
    db = SessionLocal()
    try:
        return reminders.run_due(db)
    finally:
        db.close()


async def reminders_loop() -> None:
    """Bring back the mail whose time has come.

    Runs on the server rather than in the agent because a reminder is this app's
    own promise: the agent applies moves it is handed and sleeps when nothing has
    changed, and a promise about a moment cannot be kept by a process that is not
    watching the clock.

    The first tick happens at startup, before the sleep, and that is not an
    optimisation — it is what a server that was off overnight does with the nine
    o'clock reminders it missed. They fire late, which is the whole reason each
    one carries an absolute deadline rather than a countdown.
    """
    while True:
        try:
            woken = await asyncio.to_thread(_fire_due_reminders)
            if woken:
                print(f"reminders: brought back {woken} message(s)", flush=True)
        except Exception as e:  # noqa: BLE001
            # Never let one bad tick end the loop — a reminder that cannot fire
            # keeps its place in the queue (see app/reminders.py::_note_failure)
            # and the next tick tries again. But say so: silence here looks
            # exactly like "reminders don't work".
            print(f"reminder tick failed: {e!r}", flush=True)
        await asyncio.sleep(REMINDER_TICK)
