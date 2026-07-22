"""UI-facing message actions. Each updates local state optimistically and
enqueues a PendingAction for the agent to apply to IMAP (two-way sync).

Flag changes are per-folder (a message can live in several folders, so we touch
every location). Move/trash/archive remove the source location now; the target
folder's copy is re-ingested on the next sync (dedup keeps content single)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session as DBSession, selectinload

from .. import events
from core.database import get_db
from core.events import publish_command
from ..deps import require_ui_auth
from .messages import _resolve_mailbox_ids
from core.mail.store import recompute_counts
from core.models import Account, Mailbox, Message, MessageLocation, PendingAction

router = APIRouter(prefix="/api/messages", tags=["actions"], dependencies=[Depends(require_ui_auth)])


def _enqueue(db: DBSession, account_id: int, message_pk: int, type_: str, payload: dict) -> None:
    db.add(PendingAction(account_id=account_id, message_pk=message_pk, type=type_, payload=payload))


def _get_message(db: DBSession, message_id: int) -> Message:
    msg = db.get(Message, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return msg


def _wake_agent(db: DBSession, msg: Message) -> None:
    """Nudge the agent to drain the queue now.

    A move only lands in the target folder once the agent has run it against
    IMAP and re-ingested the copy. Left to its own schedule that is a poll
    interval away, so the message would vanish from the source folder and not
    show up in the target for up to half a minute — long enough to look like
    the archive was lost. Flag changes skip this: they are applied locally the
    moment you press the key, so the IMAP round trip can wait for the next pass.
    """
    account = db.get(Account, msg.account_id)
    if account:
        publish_command({"type": "refresh", "email": account.email})


def _announce(db: DBSession, account_ids: set[int], moved: int) -> None:
    """Tell the agent and the browsers that a batch landed — once, not per message.

    Every publish() opens its own pooled connection and commits a pg_notify, so
    announcing each message individually cost one round trip per message: a
    couple of hundred for a full-page selection, which was the bulk of how long
    a bulk delete took against a non-local database. Nothing reads the
    message_id off these events — the UI treats them purely as "something
    changed, reload" and debounces them anyway — so one event per batch carries
    exactly as much information as N did.
    """
    for account_id in account_ids:
        account = db.get(Account, account_id)
        if account:
            publish_command({"type": "refresh", "email": account.email})
    events.publish({"type": "present", "moved": moved})


def _recompute(db: DBSession, mailbox_ids: set[int]) -> None:
    for mid in mailbox_ids:
        mb = db.get(Mailbox, mid)
        if mb:
            recompute_counts(db, mb)


class BulkItem(BaseModel):
    """One selected list row. Rows are conversations, so `thread_id` is the
    usual case; messages that never got threaded carry only `message_id`."""
    account_id: int
    thread_id: str | None = None
    message_id: int | None = None


class BulkTrashRequest(BaseModel):
    items: list[BulkItem]


class BulkTrashAllRequest(BaseModel):
    """The same selector the list view was built from — see list_messages()."""
    mailbox_id: int | None = None
    scope: str | None = None
    unread_only: bool = False


# Both bulk routes are registered ahead of the /{message_id}/... ones below:
# FastAPI matches in declaration order, and "bulk" is a perfectly good
# message_id as far as /{message_id}/trash is concerned.
@router.post("/bulk/trash")
def bulk_trash(req: BulkTrashRequest, db: DBSession = Depends(get_db)):
    """Trash a set of selected rows.

    Rows that have gone (trashed by another window, or moved by a sync between
    the select and the click) are skipped rather than failing the batch: the
    user asked for these to be gone, and they are.
    """
    touched: set[int] = set()
    accounts: set[int] = set()
    moved = 0

    # Gathered per account rather than per row. Doing a thread lookup and a
    # trash-mailbox lookup for each selected row is two queries per row, which
    # is most of the wall clock once a selection runs to a whole page.
    by_account: dict[int, tuple[set[str], set[int]]] = {}
    for item in req.items:
        threads, loose = by_account.setdefault(item.account_id, (set(), set()))
        if item.thread_id:
            threads.add(item.thread_id)
        elif item.message_id:
            loose.add(item.message_id)

    for account_id, (threads, loose) in by_account.items():
        target = _role_mailbox(db, account_id, "trash")  # None -> \Deleted + expunge
        match = []
        if threads:
            match.append(Message.thread_id.in_(threads))
        if loose:
            match.append(Message.id.in_(loose))
        if not match:
            continue
        msgs = db.execute(
            select(Message)
            .where(Message.account_id == account_id, or_(*match))
            # Without this every message lazy-loads its own locations, one
            # round trip each, immediately below in _move_messages.
            .options(selectinload(Message.locations))
        ).scalars().all()
        if not msgs:
            continue
        moved += _move_messages(db, msgs, target, touched)
        accounts.add(account_id)

    _recompute(db, touched)
    db.commit()
    _announce(db, accounts, moved)
    return {"ok": True, "moved": moved}


# A folder-wide delete is chunked rather than done in one transaction: every
# placement becomes a PendingAction row, so a 40k-message folder is a very long
# request and a very large commit. The client loops on `done`, which also gives
# it something honest to show a progress count from.
BULK_ALL_CHUNK = 2000


@router.post("/bulk/trash-all")
def bulk_trash_all(req: BulkTrashAllRequest, db: DBSession = Depends(get_db)):
    """Trash everything matching a list selector, up to one chunk at a time."""
    q = select(MessageLocation).where(MessageLocation.deleted.is_(False))
    if req.scope == "flagged":
        q = q.where(MessageLocation.flagged.is_(True))
    else:
        ids = _resolve_mailbox_ids(db, req.mailbox_id, req.scope)
        if not ids:
            raise HTTPException(status_code=400, detail="No mailbox selected")
        q = q.where(MessageLocation.mailbox_id.in_(ids))
    if req.unread_only:
        q = q.where(MessageLocation.seen.is_(False))

    remaining = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    locs = db.execute(q.limit(BULK_ALL_CHUNK)).scalars().all()

    # One query for the chunk's messages instead of a primary-key lookup per
    # placement, locations eager-loaded for the same reason as in bulk_trash.
    by_id = {
        m.id: m for m in db.execute(
            select(Message).where(Message.id.in_({loc.message_pk for loc in locs}))
            .options(selectinload(Message.locations))
        ).scalars().all()
    } if locs else {}

    touched: set[int] = set()
    accounts: set[int] = set()
    trash_of: dict[int, Mailbox | None] = {}   # account_id -> its Trash, looked up once
    moved = 0
    for loc in locs:
        msg = by_id.get(loc.message_pk)
        if msg is None:
            # Orphaned placement. Drop it here rather than skipping: it would
            # match the selector again on the next chunk and stall the loop.
            db.delete(loc)
            touched.add(loc.mailbox_id)
            continue
        if msg.account_id not in trash_of:
            trash_of[msg.account_id] = _role_mailbox(db, msg.account_id, "trash")
        target = trash_of[msg.account_id]
        # Selecting everything in Trash and pressing delete means empty it, not
        # move it to itself — which _move_to would treat as a no-op, leaving the
        # client looping over rows that never go away.
        if target is not None and loc.mailbox_id == target.id:
            target = None         # IMAP \Deleted + expunge
        _move_to(db, msg, loc.mailbox_id, target, touched)
        accounts.add(msg.account_id)
        moved += 1

    _recompute(db, touched)
    db.commit()
    _announce(db, accounts, moved)
    return {"ok": True, "moved": moved, "done": remaining <= len(locs)}


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


def _move_to(db: DBSession, msg: Message, source_mailbox_id: int, target: Mailbox | None,
             touched: set[int] | None = None) -> None:
    """Move exactly one folder placement, preserving the message's other labels.

    Pass `touched` to collect the affected mailboxes instead of recounting them
    here: bulk callers move thousands of placements out of the same few folders,
    and recomputing per placement would redo that scan once per message.
    """
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
    if touched is None:
        _recompute(db, {source.id})
    else:
        touched.add(source.id)


def _role_mailbox(db: DBSession, account_id: int, role: str) -> Mailbox | None:
    return db.execute(
        select(Mailbox).where(Mailbox.account_id == account_id, Mailbox.role == role)
    ).scalars().first()


def _archive_mailbox(db: DBSession, account_id: int) -> Mailbox | None:
    """Where "archive" files mail, which is not always an \\Archive folder.

    Gmail-style servers publish no \\Archive at all: archiving there means
    dropping the INBOX label while the message stays in \\All ("All Mail"),
    which as an IMAP MOVE is exactly INBOX -> All Mail. Without this fallback
    every Gmail account fails the archive action outright.
    """
    return (_role_mailbox(db, account_id, "archive")
            or _role_mailbox(db, account_id, "all"))


@router.post("/{message_id}/trash")
def trash(message_id: int, source_mailbox_id: int, db: DBSession = Depends(get_db)):
    msg = _get_message(db, message_id)
    target = _role_mailbox(db, msg.account_id, "trash")  # None -> IMAP \Deleted + expunge
    _move_to(db, msg, source_mailbox_id, target)
    db.commit()
    _wake_agent(db, msg)
    events.publish({"type": "present", "message_id": message_id})
    return {"ok": True}


@router.post("/{message_id}/archive")
def archive(message_id: int, source_mailbox_id: int, db: DBSession = Depends(get_db)):
    msg = _get_message(db, message_id)
    target = _archive_mailbox(db, msg.account_id)
    if target is None:
        raise HTTPException(status_code=400, detail="This account has no Archive folder")
    _move_to(db, msg, source_mailbox_id, target)
    db.commit()
    _wake_agent(db, msg)
    events.publish({"type": "present", "message_id": message_id})
    return {"ok": True}


def _thread_move(db: DBSession, thread_id: str, account_id: int, target: Mailbox | None) -> int:
    """File an entire conversation, every message and every placement.

    Doing this server-side rather than message-by-message from the reader is
    deliberate. The reader holds the thread as it looked when it was opened, so
    a reply ingested since — the one you are archiving *because* you just
    answered it — would be left behind and keep the conversation in the list.
    And a message can sit in several folders at once (a Proton/Gmail label as
    well as the inbox); clearing only the placement the reader happened to pick
    leaves the other one, which is enough for the row to stay exactly where it
    was. Both are why "archive" could look like it did nothing.
    """
    msgs = db.execute(
        select(Message).where(Message.account_id == account_id, Message.thread_id == thread_id)
    ).scalars().all()
    if not msgs:
        raise HTTPException(status_code=404, detail="Thread not found")
    touched: set[int] = set()
    moved = _move_messages(db, msgs, target, touched)
    _recompute(db, touched)
    db.commit()
    _wake_agent(db, msgs[0])
    for msg in msgs:
        events.publish({"type": "present", "message_id": msg.id})
    return moved


def _move_messages(db: DBSession, msgs: list[Message], target: Mailbox | None,
                   touched: set[int]) -> int:
    """Move every placement of every message, without committing or recounting."""
    moved = 0
    for msg in msgs:
        # Snapshotted: _move_to deletes out of msg.locations as it goes.
        for mailbox_id in [loc.mailbox_id for loc in msg.locations]:
            if target is not None and mailbox_id == target.id:
                continue          # already filed where it is going
            _move_to(db, msg, mailbox_id, target, touched)
            moved += 1
    return moved


@router.post("/threads/{thread_id:path}/archive")
def archive_thread(thread_id: str, account_id: int, db: DBSession = Depends(get_db)):
    target = _archive_mailbox(db, account_id)
    if target is None:
        raise HTTPException(status_code=400, detail="This account has no Archive folder")
    return {"ok": True, "moved": _thread_move(db, thread_id, account_id, target)}


@router.post("/threads/{thread_id:path}/trash")
def trash_thread(thread_id: str, account_id: int, db: DBSession = Depends(get_db)):
    target = _role_mailbox(db, account_id, "trash")  # None -> IMAP \Deleted + expunge
    return {"ok": True, "moved": _thread_move(db, thread_id, account_id, target)}


@router.post("/{message_id}/move")
def move(message_id: int, mailbox_id: int, source_mailbox_id: int, db: DBSession = Depends(get_db)):
    msg = _get_message(db, message_id)
    target = db.get(Mailbox, mailbox_id)
    if target is None or target.account_id != msg.account_id:
        raise HTTPException(status_code=400, detail="Invalid target mailbox")
    _move_to(db, msg, source_mailbox_id, target)
    db.commit()
    _wake_agent(db, msg)
    events.publish({"type": "present", "message_id": message_id})
    return {"ok": True}
