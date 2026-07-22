"""Listens for commands from the web app over Postgres LISTEN/NOTIFY.

The agent normally syncs on its own schedule: IDLE on INBOX, re-sync on change,
fall back to ``poll_interval``. The refresh button in the UI needs to cut that
wait short, so the web app publishes on ``COMMAND_CHANNEL`` (see
``core.events.publish_command``) and this thread turns the notification into a
set ``threading.Event`` that each account's sync loop watches.

Nothing here talks to the network beyond the database the agent already uses —
the two processes share Postgres, so it carries the messages too.
"""

from __future__ import annotations

import json
import threading
import time

import psycopg

from core.events import COMMAND_CHANNEL, dsn

import log

# One event per account email; each account's sync loop owns and clears its own.
_wakes: dict[str, threading.Event] = {}
_lock = threading.Lock()


def wake_event(email: str) -> threading.Event:
    """Get (creating on first call) the refresh signal for an account."""
    with _lock:
        return _wakes.setdefault(email, threading.Event())


def _wake_all() -> None:
    with _lock:
        events = list(_wakes.values())
    for ev in events:
        ev.set()


def _handle(command: dict) -> None:
    if command.get("type") != "refresh":
        return
    email = command.get("email")
    if email:
        # Unknown address: nothing to wake, and creating an event for it would
        # leak a signal no loop ever clears.
        with _lock:
            ev = _wakes.get(email)
        if ev:
            ev.set()
    else:
        _wake_all()


def _listen_forever() -> None:
    # The retry is every 2s, so while Postgres is down this loop can emit
    # hundreds of identical lines — enough to bury the sync errors you opened
    # the log to read. Say it once per outage and once again on recovery.
    complained = False
    while True:
        try:
            with psycopg.connect(dsn(), autocommit=True) as conn:
                conn.execute(f"LISTEN {COMMAND_CHANNEL}")
                if complained:
                    log.ok("listening for UI refresh commands again", "commands")
                    complained = False
                for notify in conn.notifies():
                    try:
                        _handle(json.loads(notify.payload))
                    except (ValueError, TypeError):
                        continue
        except Exception as e:  # noqa: BLE001
            if not complained:
                log.error(f"listener error: {e!r}; reconnecting every 2s until it "
                          f"comes back", "commands")
                advice = log.hint(e)
                if advice:
                    log.warn(advice, "commands")
                complained = True
        # Connection dropped (DB restart, network blip) — back off and reconnect.
        time.sleep(2.0)


def start() -> threading.Thread:
    """Run the listener in a daemon thread and return it."""
    t = threading.Thread(target=_listen_forever, name="commands", daemon=True)
    t.start()
    return t
