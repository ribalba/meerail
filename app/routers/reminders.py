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
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from core import undo
from core.database import get_db
from core.models import Account, Message, Reminder
from .. import events, journal, mailops, reminders as reminders_core, spanning
from ..deps import require_ui_auth
from .messages import _readable, also_ids

router = APIRouter(tags=["reminders"], dependencies=[Depends(require_ui_auth)])


class RemindIn(BaseModel):
    # The instant the mail should come back, worked out in the reader's own
    # timezone and sent absolute — see reminders.normalize_due. Accepts an
    # offset ("2026-08-10T09:00:00+02:00") or plain UTC; both are normalized.
    due_at: datetime
    # One message of the same conversation in each other account it is in
    # (the reader's ``members``). Each of those conversations is put off too,
    # with the same deadline: the lists show the conversation across accounts
    # as one row, and a reminder on half of it would leave the other half in
    # the inbox. Bounded like every ``also`` (app/spanning.py).
    also: list[int] = Field(default_factory=list, max_length=spanning.MAX_ALSO)


def _readable_copies(db: DBSession, ids: list[int]) -> list[Message]:
    """The messages behind ``also`` ids, passing over any the user has deleted.

    Through the same gate as the message itself (_readable), and a copy that
    has gone is skipped rather than refusing the keypress on the one that is
    still here.
    """
    out = []
    for message_id in ids:
        try:
            out.append(_readable(db, message_id))
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
    return out


def _require_archive(db: DBSession, msg: Message) -> None:
    """Refuse up front when another account's half has nowhere to be parked.

    set_reminder refuses the same thing for the message the request is named
    by, before it moves anything. This is the same check for the other copies,
    asked before the first of them moves, and it names the account, because
    the person looking at one merged row cannot tell which of its accounts is
    the one without an Archive.
    """
    if reminders_core.pending_for(db, msg) is not None:
        return                    # only its deadline will move; nothing is parked
    if mailops.archive_mailbox(db, msg.account_id) is not None:
        return
    account = db.get(Account, msg.account_id)
    name = (account.label or account.email) if account is not None else "Another account"
    raise HTTPException(
        status_code=400,
        detail=f"{name} has no Archive folder, so there is nowhere to keep a reminder's "
               f"mail until it is due. Create one on the server (or mark an existing "
               f"folder \\Archive) and sync again.")


@router.post("/api/messages/{message_id}/remind")
def set_reminder(message_id: int, body: RemindIn, db: DBSession = Depends(get_db)) -> dict:
    """File this conversation away and bring it back at ``due_at``.

    Through the same gate as reading it: a reminder moves mail, and mail the
    user has deleted is not theirs to move any more.

    With ``also``, the same conversation in the other accounts gets a reminder
    of its own, each in its own account (a reminder parks into that account's
    Archive and only the server holding the mail can move it), all with the
    same deadline and one shared op_id. One Undo then takes every one of them
    back. Every account is checked for an Archive before anything moves, and
    the whole keypress is one transaction, so a refusal leaves nothing parked.
    """
    msg = _readable(db, message_id)
    due_at = reminders_core.normalize_due(body.due_at)
    copies = _readable_copies(db, also_ids(body.also, message_id))
    for copy in copies:
        _require_archive(db, copy)
    shared = undo.new_op_id()
    reminder, op_id = reminders_core.set_reminder(db, msg, due_at, op_id=shared)
    placed = [(reminder, msg)]
    for copy in copies:
        other, moved = reminders_core.set_reminder(db, copy, due_at, op_id=shared)
        op_id = op_id or moved
        # A copy in a conversation already handled above (the client sent two
        # messages of one account's thread) finds that reminder again. It is
        # still one promise, and told once.
        if not any(other is seen for seen, _ in placed):
            placed.append((other, copy))
    # Published from here rather than from set_reminder, because set_reminder is
    # also what applies a record that arrived from another install — publishing
    # in there would send every record straight back to the server. Queued in
    # this transaction, so the promise and the telling of it commit together.
    # One record per reminder, because each is its own promise in its own
    # account to the other installs.
    db.flush()
    for each, anchor in placed:
        journal.publish_reminder_set(db, each, anchor)
    db.commit()
    for account_id in {anchor.account_id for _, anchor in placed}:
        mailops.wake_agent(db, account_id)
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
    also: list[int] = Query([]),
    db: DBSession = Depends(get_db),
) -> dict:
    """Take a reminder back — either way round.

    ``restore`` is the whole difference between "I will deal with it now after
    all", which puts the conversation back in the inbox unread as the deadline
    would have, and "never mind", which leaves it in Archive where it has been
    sitting. The mail is not where it was when the reminder was set, so a single
    verb that guessed between those would be wrong half the time.

    ``also`` names the same conversation in other accounts, and every pending
    reminder among them is taken back the same way. A 404 only when none of
    them has one: half of a merged conversation already back is not a reason to
    leave the other half waiting.
    """
    copies = also_ids(also, message_id)
    msg = _readable(db, message_id)
    found: list = []
    for m in [msg, *_readable_copies(db, copies)]:
        reminder = reminders_core.pending_for(db, m)
        if reminder is not None and not any(reminder is seen for seen in found):
            found.append(reminder)
    if not found:
        raise HTTPException(status_code=404, detail="No reminder is set on this conversation")
    for reminder in found:
        if restore:
            reminders_core.fire(db, reminder)
        else:
            reminders_core.cancel(db, reminder)
        # Both ways round retire the promise everywhere, and they are not the same
        # record: "fired" says the mail is on its way back (the move is already
        # queued here, and reaches the other installs through the mail server),
        # while "cancel" says it stays filed. An install that received the wrong one
        # of those would either move mail nobody asked it to, or go on holding a
        # deadline for a conversation that has already come back.
        journal.publish_reminder_op(db, reminder, "fired" if restore else "cancel")
    db.commit()
    if restore:
        for account_id in {reminder.account_id for reminder in found}:
            mailops.wake_agent(db, account_id)
    events.publish({"type": "present", "reminder": 1})
    # The message's own reminder when it had one, which is the one the reader
    # was showing, and otherwise the first of the others.
    return reminders_core.describe(found[0])


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
