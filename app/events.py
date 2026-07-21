"""Tiny in-process pub/sub for server-sent events.

Publishers may run in FastAPI's threadpool (sync endpoints), so ``publish`` is
thread-safe: it hops onto the captured event loop before touching subscriber
queues (which are asyncio.Queue and single-loop affine).
"""

from __future__ import annotations

import asyncio

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


def publish(event: dict) -> None:
    """Publish an event from any thread."""
    loop = _loop
    if loop is None:
        return
    try:
        loop.call_soon_threadsafe(_dispatch, event)
    except RuntimeError:
        pass  # loop shutting down
