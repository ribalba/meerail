import asyncio
import json

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from .. import events
from ..deps import require_ui_auth

router = APIRouter(tags=["stream"])


@router.get("/api/stream", dependencies=[Depends(require_ui_auth)])
async def stream(request: Request):
    """Server-sent events: sync progress, new mail, flag changes."""
    q = events.subscribe()

    async def gen():
        try:
            yield {"event": "hello", "data": "{}"}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield {"event": ev.get("type", "message"), "data": json.dumps(ev)}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}  # keepalive
        finally:
            events.unsubscribe(q)

    return EventSourceResponse(gen())
