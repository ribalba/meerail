"""The agent <-> server protocol.

Flow per folder (agent drives, server is stateless-friendly):
  1. POST /folders  — register discovered folders, get back {id, uidvalidity, last_uid}
  2. POST /scan     — send (uid, message_id, flags) for uids > last_uid; server
                      records locations for content it already has, returns need_raw
  3. POST /messages — upload raw bytes only for need_raw uids
  4. POST /cursor   — advance last_uid once the batch is fully ingested
  5. POST /flags    — flag deltas for already-synced uids (IDLE / periodic reconcile)
  6. POST /present  — full uid set in the folder; server prunes vanished locations

All endpoints are gated by the (optional) agent token.
"""

from __future__ import annotations

import base64

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from .. import events
from ..database import get_db
from ..deps import require_agent_auth
from ..models import Account, Attachment, Mailbox, Message, MessageLocation, PendingAction, utcnow
from ..mail.store import ingest_location_only, ingest_raw, recompute_counts
from ..schemas import (
    ActionAck,
    ActionOut,
    CursorRequest,
    FlagsRequest,
    FolderCursor,
    FolderRegister,
    HeartbeatRequest,
    MessagesRequest,
    MessagesResponse,
    PresentRequest,
    ScanRequest,
    ScanResponse,
)

router = APIRouter(prefix="/api/agent", tags=["agent"], dependencies=[Depends(require_agent_auth)])
MAX_ACTION_ATTEMPTS = 5


# Map an IMAP SPECIAL-USE flag / folder name to a meerail mailbox role.
_ROLE_BY_FLAG = {
    "\\sent": "sent",
    "\\drafts": "drafts",
    "\\junk": "junk",
    "\\trash": "trash",
    "\\archive": "archive",
    "\\all": "all",
    "\\flagged": "flagged",
}


def _derive_role(imap_name: str, role_hint: str) -> str:
    hint = (role_hint or "").strip().lower()
    if hint in _ROLE_BY_FLAG:
        return _ROLE_BY_FLAG[hint]
    if imap_name.upper() == "INBOX":
        return "inbox"
    leaf = imap_name.rsplit("/", 1)[-1].lower()
    return {"sent": "sent", "drafts": "drafts", "draft": "drafts", "trash": "trash",
            "junk": "junk", "spam": "junk", "archive": "archive"}.get(leaf, "custom")


def _leaf(imap_name: str) -> str:
    return imap_name.rsplit("/", 1)[-1]


def _account(db: DBSession, email: str) -> Account:
    acc = db.execute(select(Account).where(Account.email == email.lower())).scalar_one_or_none()
    if acc is None:
        raise HTTPException(status_code=404, detail=f"Account '{email}' is not configured on the server")
    acc.last_agent_seen = utcnow()
    return acc


def _mailbox(db: DBSession, account: Account, imap_name: str) -> Mailbox:
    mb = db.execute(
        select(Mailbox).where(Mailbox.account_id == account.id, Mailbox.imap_name == imap_name)
    ).scalar_one_or_none()
    if mb is None:
        raise HTTPException(status_code=409, detail=f"Folder '{imap_name}' not registered; POST /folders first")
    return mb


@router.post("/folders", response_model=list[FolderCursor])
def register_folders(payload: FolderRegister, db: DBSession = Depends(get_db)):
    account = _account(db, payload.account)
    out: list[FolderCursor] = []
    for i, f in enumerate(payload.folders):
        mb = db.execute(
            select(Mailbox).where(Mailbox.account_id == account.id, Mailbox.imap_name == f.imap_name)
        ).scalar_one_or_none()
        if mb is None:
            mb = Mailbox(
                account_id=account.id,
                imap_name=f.imap_name,
                display_name=_leaf(f.imap_name),
                role=_derive_role(f.imap_name, f.role_hint),
                sort_order=i,
            )
            db.add(mb)
        else:
            # UIDVALIDITY change invalidates our UID cursor for this folder.
            if mb.uidvalidity is not None and f.uidvalidity is not None and mb.uidvalidity != f.uidvalidity:
                mb.last_uid = 0
            if mb.role == "custom":
                mb.role = _derive_role(f.imap_name, f.role_hint)
        mb.uidvalidity = f.uidvalidity
        mb.uidnext = f.uidnext
        db.flush()
        out.append(FolderCursor(id=mb.id, imap_name=mb.imap_name, role=mb.role,
                                uidvalidity=mb.uidvalidity, last_uid=mb.last_uid))
    db.commit()
    events.publish({"type": "folders", "account": account.email, "count": len(out)})
    return out


@router.post("/scan", response_model=ScanResponse)
def scan(payload: ScanRequest, db: DBSession = Depends(get_db)):
    account = _account(db, payload.account)
    mb = _mailbox(db, account, payload.folder)
    need_raw: list[int] = []
    matched = 0
    for item in payload.items:
        if item.message_id and ingest_location_only(
            db, account, mb, item.uid, item.flags.model_dump(), item.message_id
        ):
            matched += 1
        else:
            need_raw.append(item.uid)
    db.commit()
    return ScanResponse(matched=matched, need_raw=need_raw)


@router.post("/messages", response_model=MessagesResponse)
def upload_messages(payload: MessagesRequest, db: DBSession = Depends(get_db)):
    account = _account(db, payload.account)
    mb = _mailbox(db, account, payload.folder)
    stored = created = 0
    for item in payload.items:
        try:
            raw = base64.b64decode(item.raw_b64)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail=f"Bad base64 for uid {item.uid}")
        _msg, was_new = ingest_raw(db, account, mb, item.uid, item.flags.model_dump(), raw)
        stored += 1
        created += 1 if was_new else 0
    db.commit()
    if stored:
        events.publish({"type": "messages", "account": account.email,
                        "folder": mb.imap_name, "stored": stored})
    return MessagesResponse(stored=stored, created=created)


@router.post("/cursor")
def advance_cursor(payload: CursorRequest, db: DBSession = Depends(get_db)):
    account = _account(db, payload.account)
    mb = _mailbox(db, account, payload.folder)
    if payload.last_uid > mb.last_uid:
        mb.last_uid = payload.last_uid
    recompute_counts(db, mb)
    db.commit()
    events.publish({"type": "cursor", "account": account.email, "folder": mb.imap_name,
                    "last_uid": mb.last_uid, "total": mb.total_count, "unread": mb.unread_count})
    return {"ok": True, "last_uid": mb.last_uid}


@router.post("/flags")
def update_flags(payload: FlagsRequest, db: DBSession = Depends(get_db)):
    account = _account(db, payload.account)
    mb = _mailbox(db, account, payload.folder)
    updated = 0
    for item in payload.items:
        loc = db.execute(
            select(MessageLocation).where(
                MessageLocation.mailbox_id == mb.id, MessageLocation.imap_uid == item.uid
            )
        ).scalar_one_or_none()
        if loc is None:
            continue
        f = item.flags
        loc.seen, loc.flagged, loc.answered, loc.draft, loc.deleted = (
            f.seen, f.flagged, f.answered, f.draft, f.deleted
        )
        loc.keywords = f.keywords
        updated += 1
    recompute_counts(db, mb)
    db.commit()
    events.publish({"type": "flags", "account": account.email, "folder": mb.imap_name,
                    "updated": updated, "unread": mb.unread_count})
    return {"ok": True, "updated": updated}


@router.post("/present")
def prune_vanished(payload: PresentRequest, db: DBSession = Depends(get_db)):
    account = _account(db, payload.account)
    mb = _mailbox(db, account, payload.folder)
    present = set(payload.uids)
    locs = db.execute(
        select(MessageLocation).where(MessageLocation.mailbox_id == mb.id)
    ).scalars().all()
    affected: set[int] = set()
    removed = 0
    for loc in locs:
        if loc.imap_uid not in present:
            affected.add(loc.message_pk)
            db.delete(loc)
            removed += 1
    db.flush()
    # Delete messages (and their blobs) that no longer live in any folder.
    for pk in affected:
        remaining = db.scalar(
            select(MessageLocation.id).where(MessageLocation.message_pk == pk).limit(1)
        )
        if remaining:
            continue
        _delete_message_blobs(db, pk)
        db.query(Message).filter(Message.id == pk).delete()
    recompute_counts(db, mb)
    db.commit()
    events.publish({"type": "present", "account": account.email, "folder": mb.imap_name,
                    "removed": removed, "total": mb.total_count})
    return {"ok": True, "removed": removed}


def _delete_message_blobs(db: DBSession, message_pk: int) -> None:
    import os

    msg = db.get(Message, message_pk)
    if msg and msg.raw_path:
        try:
            os.remove(msg.raw_path)
        except OSError:
            pass
    for path in db.execute(
        select(Attachment.disk_path).where(Attachment.message_pk == message_pk)
    ).all():
        if path[0]:
            try:
                os.remove(path[0])
            except OSError:
                pass


@router.post("/heartbeat")
def heartbeat(payload: HeartbeatRequest, db: DBSession = Depends(get_db)):
    account = _account(db, payload.account)
    if payload.backfill_complete is not None:
        account.backfill_complete = payload.backfill_complete
    account.last_sync_at = utcnow()
    db.commit()
    return {"ok": True}


# --- Outbound actions (populated in M5; forward-compatible now) ------------


@router.get("/actions", response_model=list[ActionOut])
def get_actions(account: str, db: DBSession = Depends(get_db)):
    acc = _account(db, account)
    db.commit()
    rows = db.execute(
        select(PendingAction)
        .where(PendingAction.account_id == acc.id, PendingAction.status == "pending")
        .order_by(PendingAction.created_at)
        .limit(50)
    ).scalars().all()
    return rows


@router.get("/outbound/{outbound_id}")
def get_outbound(outbound_id: int, db: DBSession = Depends(get_db)):
    """The raw MIME for a queued outgoing message (fetched by the agent to send)."""
    import base64

    from ..models import Outbound

    ob = db.get(Outbound, outbound_id)
    if ob is None or not ob.raw_mime:
        raise HTTPException(status_code=404, detail="Outbound message not found")
    return {"raw_b64": base64.b64encode(ob.raw_mime.encode("utf-8")).decode("ascii")}


@router.post("/actions/{action_id}/ack")
def ack_action(action_id: int, ack: ActionAck, db: DBSession = Depends(get_db)):
    action = db.get(PendingAction, action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="Action not found")
    next_attempt = action.attempts + 1
    terminal_error = not ack.ok and next_attempt >= MAX_ACTION_ATTEMPTS
    action.status = "done" if ack.ok else ("error" if terminal_error else "pending")
    action.error = ack.error
    action.attempts = next_attempt
    # A successful send flips its Outbound record to "sent" (Proton then auto-saves
    # it to Sent, which the next folder sync ingests normally).
    if action.type == "send":
        from ..models import Outbound
        ob = db.get(Outbound, action.payload.get("outbound_id"))
        if ob:
            ob.state = "sent" if ack.ok else ("error" if terminal_error else "queued")
            ob.error = ack.error
            if ack.ok:
                ob.sent_at = utcnow()
    db.commit()
    return {"ok": True}
