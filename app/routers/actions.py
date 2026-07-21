"""UI-facing message actions. Each updates local state optimistically and
enqueues a PendingAction for the agent to apply to IMAP (two-way sync).

Flag changes are per-folder (a message can live in several folders, so we touch
every location). Move/trash/archive remove the source location now; the target
folder's copy is re-ingested on the next sync (dedup keeps content single)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from .. import events
from core.database import get_db
from ..deps import require_ui_auth
from core.mail.store import recompute_counts
from core.models import Mailbox, Message, MessageLocation, PendingAction

router = APIRouter(prefix="/api/messages", tags=["actions"], dependencies=[Depends(require_ui_auth)])


def _enqueue(db: DBSession, account_id: int, message_pk: int, type_: str, payload: dict) -> None:
    db.add(PendingAction(account_id=account_id, message_pk=message_pk, type=type_, payload=payload))


def _get_message(db: DBSession, message_id: int) -> Message:
    msg = db.get(Message, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return msg


def _recompute(db: DBSession, mailbox_ids: set[int]) -> None:
    for mid in mailbox_ids:
        mb = db.get(Mailbox, mid)
        if mb:
            recompute_counts(db, mb)


@router.post("/{message_id}/mark")
def mark(message_id: int, seen: bool = True, db: DBSession = Depends(get_db)):
    msg = _get_message(db, message_id)
    touched: set[int] = set()
    for loc in msg.locations:
        if loc.seen == seen:
            continue
        loc.seen = seen
        mb = db.get(Mailbox, loc.mailbox_id)
        _enqueue(db, msg.account_id, msg.id, "setflags", {
            "folder": mb.imap_name, "uid": loc.imap_uid,
            "add": ["\\Seen"] if seen else [], "remove": [] if seen else ["\\Seen"]})
        touched.add(loc.mailbox_id)
    _recompute(db, touched)
    db.commit()
    events.publish({"type": "flags", "message_id": message_id})
    return {"ok": True, "seen": seen}


@router.post("/{message_id}/flag")
def flag(message_id: int, flagged: bool = True, db: DBSession = Depends(get_db)):
    msg = _get_message(db, message_id)
    touched: set[int] = set()
    for loc in msg.locations:
        loc.flagged = flagged
        mb = db.get(Mailbox, loc.mailbox_id)
        _enqueue(db, msg.account_id, msg.id, "setflags", {
            "folder": mb.imap_name, "uid": loc.imap_uid,
            "add": ["\\Flagged"] if flagged else [], "remove": [] if flagged else ["\\Flagged"]})
        touched.add(loc.mailbox_id)
    _recompute(db, touched)
    db.commit()
    events.publish({"type": "flags", "message_id": message_id})
    return {"ok": True, "flagged": flagged}


def _move_to(db: DBSession, msg: Message, source_mailbox_id: int, target: Mailbox | None) -> None:
    """Move exactly one folder placement, preserving the message's other labels."""
    loc = next((item for item in msg.locations if item.mailbox_id == source_mailbox_id), None)
    if loc is None:
        raise HTTPException(status_code=400, detail="Message is not in the source mailbox")
    source = db.get(Mailbox, loc.mailbox_id)
    if target is not None and source.id == target.id:
        return
    if target is not None:
        _enqueue(db, msg.account_id, msg.id, "move",
                 {"from_folder": source.imap_name, "uid": loc.imap_uid, "to_folder": target.imap_name})
    else:
        _enqueue(db, msg.account_id, msg.id, "delete",
                 {"folder": source.imap_name, "uid": loc.imap_uid})
    db.delete(loc)
    _recompute(db, {source.id})


def _role_mailbox(db: DBSession, account_id: int, role: str) -> Mailbox | None:
    return db.execute(
        select(Mailbox).where(Mailbox.account_id == account_id, Mailbox.role == role)
    ).scalars().first()


@router.post("/{message_id}/trash")
def trash(message_id: int, source_mailbox_id: int, db: DBSession = Depends(get_db)):
    msg = _get_message(db, message_id)
    target = _role_mailbox(db, msg.account_id, "trash")  # None -> IMAP \Deleted + expunge
    _move_to(db, msg, source_mailbox_id, target)
    db.commit()
    events.publish({"type": "present", "message_id": message_id})
    return {"ok": True}


@router.post("/{message_id}/archive")
def archive(message_id: int, source_mailbox_id: int, db: DBSession = Depends(get_db)):
    msg = _get_message(db, message_id)
    target = _role_mailbox(db, msg.account_id, "archive")
    if target is None:
        raise HTTPException(status_code=400, detail="This account has no Archive folder")
    _move_to(db, msg, source_mailbox_id, target)
    db.commit()
    events.publish({"type": "present", "message_id": message_id})
    return {"ok": True}


@router.post("/{message_id}/move")
def move(message_id: int, mailbox_id: int, source_mailbox_id: int, db: DBSession = Depends(get_db)):
    msg = _get_message(db, message_id)
    target = db.get(Mailbox, mailbox_id)
    if target is None or target.account_id != msg.account_id:
        raise HTTPException(status_code=400, detail="Invalid target mailbox")
    _move_to(db, msg, source_mailbox_id, target)
    db.commit()
    events.publish({"type": "present", "message_id": message_id})
    return {"ok": True}
