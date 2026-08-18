"""Background workers started on server startup.

Mail ingest and attachment text extraction belong to the agent now, so what is
left here is what the app owes on its own account: the contacts rollup — pure
derived data, rebuilt from rows the agent has already written — the reminder
tick, which is the only thing in meerail that has to happen at a particular
moment rather than in response to a request, the search-index backfill, which is
a one-off debt the upgrade that added the index left behind, and the journal
loop, which is what keeps several installs of this app agreeing about the things
IMAP has nowhere to put. The journal loop only exists on an install that has one
configured; see app/journal.py.
"""

from __future__ import annotations

import asyncio
import os

from core.config import get_settings
from core.database import SessionLocal

from core import searchindex

from . import journal, reminders
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


# How long to wait after a batch that had work in it. Not a rate limit so much
# as room to breathe: the agent is usually mid-import when this runs, and the
# backfill has nobody waiting on it — finishing a large mailbox a minute later
# costs nothing, while holding the write path up does.
SEARCH_INDEX_PAUSE = 0.2

# How much progress goes by between log lines. The backfill is the one thing
# that happens on an upgrade which the user can neither see nor hurry, and it
# takes minutes on a large mailbox — during which keyword search is still on the
# old slow path. Saying so beats leaving them to wonder whether `docker compose
# up --build` did anything.
SEARCH_INDEX_REPORT_EVERY = 20_000


def _search_index_debt() -> int:
    db = SessionLocal()
    try:
        return searchindex.pending(db)
    finally:
        db.close()


def _build_search_index_batch() -> int:
    db = SessionLocal()
    try:
        return searchindex.build_batch(db)
    finally:
        db.close()


def _analyze_after_search_index() -> None:
    db = SessionLocal()
    try:
        searchindex.analyze(db)
    finally:
        db.close()


async def search_index_loop() -> None:
    """Give mail that predates ``messages.search_tsv`` its search index.

    Nothing to do on a volume created after the column existed — the trigger
    fills it in on the way in — so the ordinary case is one empty batch at
    startup and then an hourly probe that costs an index lookup. The probe stays
    rather than the loop ending on its first zero: a batch that fails leaves
    rows behind, and the difference between "finished" and "gave up" is not one
    this can tell from the inside.

    The whole thing is driven from here rather than from ``init_db`` because the
    work is proportional to the mailbox. A server that would not answer until it
    had indexed 113k messages is a server that looks broken for five minutes;
    this way it serves from the first second and merely searches the old way
    until the last batch lands.
    """
    owed = 0
    try:
        owed = await asyncio.to_thread(_search_index_debt)
    except Exception as e:  # noqa: BLE001
        print(f"search index: could not size the backfill: {e!r}", flush=True)
    if owed:
        print(f"search index: {owed} message(s) predate it — building; "
              f"keyword search stays on the slower path until this finishes",
              flush=True)

    built = 0
    reported = 0
    while True:
        done = 0
        try:
            done = await asyncio.to_thread(_build_search_index_batch)
        except Exception as e:  # noqa: BLE001
            # Same reasoning as the contacts loop: never let one bad batch end
            # the loop, but never let it be silent either — search staying on
            # the slow path forever is exactly what this failing looks like.
            print(f"search index backfill failed: {e!r}", flush=True)
        if done:
            built += done
            if built - reported >= SEARCH_INDEX_REPORT_EVERY:
                reported = built
                print(f"search index: {built}/{owed or built} message(s)", flush=True)
        elif built:
            # Said once, on the edge from work to none: this is the moment
            # search gets fast, and it is worth being able to point at it.
            try:
                await asyncio.to_thread(_analyze_after_search_index)
            except Exception as e:  # noqa: BLE001
                # Not worth failing over — autoanalyze will get there — but
                # worth saying, because until it does the planner is costing
                # this query from statistics that predate the column.
                print(f"search index: ANALYZE after backfill failed: {e!r}", flush=True)
            print(f"search index: built for {built} message(s) — "
                  f"keyword search is on the index now", flush=True)
            built = reported = owed = 0
        await asyncio.sleep(SEARCH_INDEX_PAUSE if done else 3600)


# How often a snapshot is posted: the record that restates this install's whole
# shareable state and so lets the server delete what came before it
# (journal/server.py::_prune). Daily is far more often than pruning needs and
# far less often than it costs anything — a snapshot is one small record per
# pending reminder, and a mailbox with a hundred of those is unusual.
SNAPSHOT_EVERY = 24 * 3600


def _journal_once() -> tuple[int, int]:
    db = SessionLocal()
    try:
        return journal.sync_once(db)
    finally:
        db.close()


def _journal_snapshot() -> int:
    db = SessionLocal()
    try:
        count = journal.snapshot(db)
        db.commit()
        return count
    finally:
        db.close()


async def journal_loop() -> None:
    """Publish what this install decided, and apply what the others did.

    Started only where a journal is configured — an install without one runs
    exactly the code it ran before this existed, with no loop, no outbound
    request, and nothing on the network.

    The failure mode worth stating: a journal server that is unreachable stops
    nothing. Reminders still fire here, footers still save here, and the records
    describing all of it queue in ``journal_outbox`` until the server comes back.
    What is lost while it is down is agreement, not function — which is why this
    logs and sleeps rather than retrying tightly or escalating.
    """
    period = max(5, settings.journal_poll_interval)
    since_snapshot = 0.0
    # A snapshot on startup, so a machine that has just joined an existing
    # journal contributes its state immediately rather than a day later.
    first = True
    while True:
        try:
            if first or since_snapshot >= SNAPSHOT_EVERY:
                await asyncio.to_thread(_journal_snapshot)
                since_snapshot, first = 0.0, False
            sent, got = await asyncio.to_thread(_journal_once)
            if sent or got:
                print(f"journal: sent {sent}, applied {got}", flush=True)
        except Exception as e:  # noqa: BLE001
            # Same rule as the other two loops: one bad pass must not end the
            # loop. Everything this does is retried from durable rows on the
            # next one — the outbox keeps what has not been sent, and the cursor
            # keeps what has not been read.
            print(f"journal pass failed: {e!r}", flush=True)
        await asyncio.sleep(period)
        since_snapshot += period
