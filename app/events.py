"""Server-sent events fan-out for the web UI.

Events originate in whichever process made the change — mostly the agent, which
does all mail ingest — so they arrive over Postgres LISTEN/NOTIFY rather than an
in-process call (see ``core/events.py`` for the publish side). One listener
connection feeds every browser subscriber.

``publish`` is re-exported so server-side writers (mark read, flag, move) use the
same path; their events round-trip through Postgres too, which keeps ordering
consistent with the agent's.
"""

from __future__ import annotations

import asyncio
import json
import time

import psycopg

from core.events import CHANNEL, dsn, publish  # noqa: F401  (publish re-exported for routers)

_loop: asyncio.AbstractEventLoop | None = None
_subscribers: set[asyncio.Queue] = set()


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def _dispatch(event: dict) -> None:
    for q in list(_subscribers):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass  # slow consumer: drop rather than block the publisher


def _deliver_threadsafe(event: dict) -> None:
    """Hand an event from the listener thread to the event loop."""
    loop = _loop
    if loop is None:
        return
    try:
        loop.call_soon_threadsafe(_dispatch, event)
    except RuntimeError:
        pass  # loop shutting down


def _listen_forever() -> None:
    """Blocking LISTEN loop; run in a worker thread.

    Uses its own connection rather than one from the engine pool: this one is
    held open indefinitely in autocommit (LISTEN inside a transaction only
    delivers on commit), which is not how pooled connections should be used.
    """
    while True:
        try:
            with psycopg.connect(dsn(), autocommit=True) as conn:
                conn.execute(f"LISTEN {CHANNEL}")
                for notify in conn.notifies():
                    try:
                        _deliver_threadsafe(json.loads(notify.payload))
                    except (ValueError, TypeError):
                        continue
        except Exception:
            pass
        # Connection dropped (DB restart, network blip) — back off and reconnect.
        time.sleep(2.0)


async def listener_loop() -> None:
    await asyncio.to_thread(_listen_forever)
