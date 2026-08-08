"""The "remind me" verb, and the list of what is waiting on one.

Two routes hang off a message, because that is how it is reached — the mail is
on screen and the answer is "not today" — and one lists what has been put off,
which is the only way to find a conversation again before its time comes. What
any of it actually does is in app/reminders.py; this is the HTTP surface.

The reminder is keyed by message rather than by an id of its own on purpose: the
reader has a message open, not a reminder, and a client that had to look up an id
before it could cancel one would need a round trip to answer a keypress.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from core.database import get_db
from core.models import Message, Reminder
from .. import events, mailops, reminders as reminders_core
from ..deps import require_ui_auth
from .messages import _readable

router = APIRouter(tags=["reminders"], dependencies=[Depends(require_ui_auth)])


class RemindIn(BaseModel):
    # The instant the mail should come back, worked out in the reader's own
    # timezone and sent absolute — see reminders.normalize_due. Accepts an
    # offset ("2026-08-10T09:00:00+02:00") or plain UTC; both are normalized.
    due_at: datetime


@router.post("/api/messages/{message_id}/remind")
def set_reminder(message_id: int, body: RemindIn, db: DBSession = Depends(get_db)) -> dict:
    """File this conversation away and bring it back at ``due_at``.

    Through the same gate as reading it: a reminder moves mail, and mail the
    user has deleted is not theirs to move any more.
    """
    msg = _readable(db, message_id)
    due_at = reminders_core.normalize_due(body.due_at)
    reminder, op_id = reminders_core.set_reminder(db, msg, due_at)
    db.commit()
    mailops.wake_agent(db, msg.account_id)
    events.publish({"type": "present", "reminder": 1})
    # op_id lets the Recent actions panel show the parking straight away —
    # null when the reminder already existed and only its deadline moved,
    # because then no mail was filed. See app/static/js/app.undo.js.
    return {**reminders_core.describe(reminder), "op_id": op_id}


@router.delete("/api/messages/{message_id}/remind")
def clear_reminder(
    message_id: int,
    restore: bool = Query(True, description="Bring the mail back now (default), or "
                                            "just forget the reminder and leave it filed"),
    db: DBSession = Depends(get_db),
) -> dict:
    """Take a reminder back — either way round.

    ``restore`` is the whole difference between "I will deal with it now after
    all", which puts the conversation back in the inbox unread as the deadline
    would have, and "never mind", which leaves it in Archive where it has been
    sitting. The mail is not where it was when the reminder was set, so a single
    verb that guessed between those would be wrong half the time.
    """
    msg = _readable(db, message_id)
    reminder = reminders_core.pending_for(db, msg)
    if reminder is None:
        raise HTTPException(status_code=404, detail="No reminder is set on this conversation")
    if restore:
        reminders_core.fire(db, reminder)
    else:
        reminders_core.cancel(db, reminder)
    db.commit()
    if restore:
        mailops.wake_agent(db, msg.account_id)
    events.publish({"type": "present", "reminder": 1})
    return reminders_core.describe(reminder)


@router.get("/api/reminders")
def list_reminders(db: DBSession = Depends(get_db)) -> dict:
    """Everything waiting on a reminder, soonest first.

    The conversations are also listable as a folder (``/api/messages?scope=
    reminders``), which is what the sidebar row opens — that renders them as the
    mail they are, sorted by date like every other list. This one is sorted by
    when they come back, which is the other question anyone asks of the same set,
    and it is what a compact panel or another client would read.
    """
    rows = db.execute(
        select(Reminder, Message.subject, Message.from_name, Message.from_addr)
        .join(Message, Message.id == Reminder.message_pk)
        .where(Reminder.state == "pending")
        .order_by(Reminder.due_at, Reminder.id)
    ).all()
    out = []
    for reminder, subject, from_name, from_addr in rows:
        item = reminders_core.describe(reminder)
        item.update({"subject": subject or "(no subject)",
                     "from_name": from_name, "from_addr": from_addr})
        out.append(item)
    return {"rows": out, "total": len(out),
            # Reminders whose time has come and which have not landed yet — a
            # server that was off, or a move the agent has not managed. The
            # sidebar shows nothing for it; a client that wants to say "these
            # are late" has the number without re-deriving it from the rows.
            "overdue": sum(1 for r in out if r["overdue"])}
