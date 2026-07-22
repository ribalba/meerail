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

# Commands travel the other way — web app to agent — and get their own channel
# on purpose. The agent publishes ingest events on CHANNEL, so if it also
# listened there for work it would wake itself on its own notifications.
COMMAND_CHANNEL = "meerail_commands"

# Well below the 8000-byte NOTIFY limit, leaving room for the JSON envelope.
_MAX_PAYLOAD = 4000


def dsn() -> str:
    """Plain libpq DSN for the database, without the SQLAlchemy driver tag.

    LISTEN needs a raw psycopg connection held open in autocommit, which is not
    something the SQLAlchemy pool should hand out.
    """
    return engine.url.set(drivername="postgresql").render_as_string(hide_password=False)


def _notify(channel: str, payload: dict) -> None:
    """Best-effort NOTIFY: never raises."""
    try:
        body = json.dumps(payload, default=str)
        if len(body) > _MAX_PAYLOAD:
            # Drop the detail but keep the type so listeners still act.
            body = json.dumps({"type": payload.get("type", "change")})
        with engine.connect() as conn:
            conn.execute(text("SELECT pg_notify(:chan, :payload)"),
                         {"chan": channel, "payload": body})
            conn.commit()
    except Exception:
        # Events are advisory; a failure here must never break ingest or an API call.
        pass


def publish(event: dict) -> None:
    """Broadcast an event to every listening process. Best-effort: never raises."""
    _notify(CHANNEL, event)


def publish_command(command: dict) -> None:
    """Ask the running agent(s) to do something. Best-effort: never raises.

    Fire-and-forget by design: with no agent running the notification is simply
    dropped, which is the same outcome as the agent being mid-sync already.
    """
    _notify(COMMAND_CHANNEL, command)
