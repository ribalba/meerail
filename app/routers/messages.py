"""Read APIs for the mail UI: message list, detail, thread, attachments."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session as DBSession

from core.database import get_db
from ..deps import require_ui_auth
from ..mail.render import sanitize_html
from core.models import Account, Attachment, Mailbox, Message, MessageLocation, Recipient

router = APIRouter(prefix="/api", tags=["messages"], dependencies=[Depends(require_ui_auth)])


def _resolve_mailbox_ids(db: DBSession, mailbox_id: int | None, scope: str | None) -> list[int]:
    if mailbox_id is not None:
        return [mailbox_id]
    if scope == "unified_inbox":
        return list(db.execute(select(Mailbox.id).where(Mailbox.role == "inbox")).scalars().all())
    return []  # flagged/other scopes filter differently (see below)


@router.get("/messages")
def list_messages(
    db: DBSession = Depends(get_db),
    mailbox_id: int | None = None,
    scope: str | None = Query(None, description="unified_inbox | flagged"),
    unread_only: bool = False,
    limit: int = Query(60, le=200),
    offset: int = 0,
):
    """A flat, date-descending list of messages in a folder/scope.

    Each row carries its thread size so the UI can badge conversations; opening a
    row loads the whole thread in the reader.
    """
    j = select(
        Message.id, Message.thread_id, Message.subject, Message.from_name, Message.from_addr,
        Message.date_sent, Message.snippet, Message.has_attachments,
        MessageLocation.seen, MessageLocation.flagged, MessageLocation.answered,
        Message.account_id, Account.color, MessageLocation.mailbox_id, Mailbox.role,
    ).select_from(MessageLocation).join(
        Message, Message.id == MessageLocation.message_pk
    ).join(Mailbox, Mailbox.id == MessageLocation.mailbox_id).join(
        Account, Account.id == Message.account_id
    ).where(MessageLocation.deleted.is_(False))

    if scope == "flagged":
        j = j.where(MessageLocation.flagged.is_(True))
    else:
        ids = _resolve_mailbox_ids(db, mailbox_id, scope)
        if not ids:
            return {"rows": [], "total": 0}
        j = j.where(MessageLocation.mailbox_id.in_(ids))

    if unread_only:
        j = j.where(MessageLocation.seen.is_(False))

    total = db.scalar(select(func.count()).select_from(j.subquery()))
    rows = db.execute(
        j.order_by(Message.date_sent.desc().nulls_last()).limit(limit).offset(offset)
    ).all()

    # Thread sizes for the page (global per account).
    thread_ids = {r.thread_id for r in rows if r.thread_id}
    sizes: dict[tuple[int, str], int] = {}
    if thread_ids:
        account_threads = {(r.account_id, r.thread_id) for r in rows if r.thread_id}
        for account_id, tid, n in db.execute(
            select(Message.account_id, Message.thread_id, func.count())
            .where(tuple_(Message.account_id, Message.thread_id).in_(account_threads))
            .group_by(Message.account_id, Message.thread_id)
        ).all():
            sizes[(account_id, tid)] = n

    return {
        "total": int(total or 0),
        "rows": [
            {
                "id": r.id, "thread_id": r.thread_id, "subject": r.subject or "(no subject)",
                "from_name": r.from_name, "from_addr": r.from_addr,
                "date": r.date_sent.isoformat() if r.date_sent else None,
                "snippet": r.snippet, "has_attachments": r.has_attachments,
                "seen": r.seen, "flagged": r.flagged, "answered": r.answered,
                "account_id": r.account_id, "account_color": r.color,
                "mailbox_id": r.mailbox_id, "mailbox_role": r.role,
                "thread_count": sizes.get((r.account_id, r.thread_id), 1),
            }
            for r in rows
        ],
    }


def _recipients(db: DBSession, message_pk: int) -> dict[str, list[dict]]:
    rows = db.execute(
        select(Recipient.kind, Recipient.name, Recipient.address)
        .where(Recipient.message_pk == message_pk)
    ).all()
    out: dict[str, list[dict]] = {"to": [], "cc": [], "bcc": [], "reply_to": [], "from": []}
    for kind, name, addr in rows:
        out.setdefault(kind, []).append({"name": name, "address": addr})
    return out


def _message_detail(db: DBSession, msg: Message, load_remote: bool) -> dict:
    safe_html, blocked = sanitize_html(msg.body_html, msg.id, load_remote) if msg.body_html else ("", 0)
    atts = db.execute(
        select(Attachment).where(Attachment.message_pk == msg.id).order_by(Attachment.id)
    ).scalars().all()
    # Any location's flags (a message may be in several folders; report the union).
    locs = db.execute(
        select(MessageLocation).where(MessageLocation.message_pk == msg.id)
    ).scalars().all()
    return {
        "id": msg.id, "account_id": msg.account_id, "thread_id": msg.thread_id,
        "message_id": msg.message_id, "subject": msg.subject or "(no subject)",
        "from_name": msg.from_name, "from_addr": msg.from_addr,
        "date": msg.date_sent.isoformat() if msg.date_sent else None,
        "recipients": _recipients(db, msg.id),
        "body_html": safe_html, "body_text": msg.body_text,
        "remote_blocked": blocked, "images_loaded": load_remote,
        "has_attachments": msg.has_attachments,
        "seen": any(l.seen for l in locs), "flagged": any(l.flagged for l in locs),
        "answered": any(l.answered for l in locs),
        "locations": [
            {"mailbox_id": l.mailbox_id, "role": db.get(Mailbox, l.mailbox_id).role}
            for l in locs if not l.deleted
        ],
        "attachments": [
            {"id": a.id, "filename": a.filename, "content_type": a.content_type,
             "size": a.size_bytes, "is_inline": a.is_inline}
            for a in atts if not a.is_inline
        ],
    }


@router.get("/messages/{message_id}")
def get_message(message_id: int, images: bool = False, db: DBSession = Depends(get_db)):
    msg = db.get(Message, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return _message_detail(db, msg, load_remote=images)


@router.get("/threads/{thread_id}")
def get_thread(thread_id: str, account_id: int, images: bool = False, db: DBSession = Depends(get_db)):
    msgs = db.execute(
        select(Message).where(Message.account_id == account_id, Message.thread_id == thread_id)
        .order_by(Message.date_sent.asc().nulls_first())
    ).scalars().all()
    if not msgs:
        raise HTTPException(status_code=404, detail="Thread not found")
    return {
        "thread_id": thread_id,
        "subject": msgs[-1].subject or "(no subject)",
        "messages": [_message_detail(db, m, load_remote=images) for m in msgs],
    }


@router.get("/attachments/{attachment_id}")
def download_attachment(attachment_id: int, db: DBSession = Depends(get_db)):
    att = db.get(Attachment, attachment_id)
    if att is None or att.content is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    filename = (att.filename or "attachment").replace('"', "")
    return Response(
        content=att.content,
        media_type=att.content_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/messages/{message_id}/cid/{content_id}")
def inline_cid(message_id: int, content_id: str, db: DBSession = Depends(get_db)):
    att = db.execute(
        select(Attachment).where(
            Attachment.message_pk == message_id, Attachment.content_id == content_id
        )
    ).scalars().first()
    if att is None or att.content is None:
        raise HTTPException(status_code=404, detail="Inline image not found")
    return Response(content=att.content,
                    media_type=att.content_type or "application/octet-stream")
