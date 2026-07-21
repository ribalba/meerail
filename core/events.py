"""Cross-process event publishing, over Postgres LISTEN/NOTIFY.

Ingest runs in the agent process while the SSE stream is served by the web app,
so an in-memory pub/sub cannot bridge them. Both sides already share the
database, so it carries the notifications too: publishers ``NOTIFY`` and the web
app holds one ``LISTEN`` connection that fans events out to browser subscribers
(see ``app/events.py``).

Payloads must stay small — Postgres caps a notification at 8000 bytes — which is
fine because events are just cache-invalidation hints ("something changed in this
folder"), never message content.
"""

from __future__ import annotations

import json

from sqlalchemy import text

from .database import engine

CHANNEL = "meerail_events"

# Well below the 8000-byte NOTIFY limit, leaving room for the JSON envelope.
_MAX_PAYLOAD = 4000


def publish(event: dict) -> None:
    """Broadcast an event to every listening process. Best-effort: never raises."""
    try:
        payload = json.dumps(event, default=str)
        if len(payload) > _MAX_PAYLOAD:
            # Drop the detail but keep the type so listeners still refresh.
            payload = json.dumps({"type": event.get("type", "change")})
        with engine.connect() as conn:
            conn.execute(text("SELECT pg_notify(:chan, :payload)"),
                         {"chan": CHANNEL, "payload": payload})
            conn.commit()
    except Exception:
        # Events are advisory; a failure here must never break ingest or an API call.
        pass
