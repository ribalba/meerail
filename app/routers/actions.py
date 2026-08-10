"""UI-facing message actions: the HTTP surface over app/mailops.py.

Each route updates local state optimistically and enqueues a PendingAction for
the agent to apply to IMAP (two-way sync). Flag changes are per-folder (a message
can live in several folders, so we touch every location). Move/trash/archive
remove the source location now; the target folder's copy is re-ingested on the
next sync (dedup keeps content single).

What each of those *means* lives in app/mailops.py, not here. This module is
request in, response out: parse the selector, call the engine, commit, say what
happened. The split is what lets app/reminders.py and app/routers/undo.py file
mail by the same rules without importing a router — see mailops' docstring.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session as DBSession, selectinload

from .. import events, mailops
from core import undo
from core.database import get_db
from ..deps import require_ui_auth
from .messages import _resolve_mailbox_ids
from core.models import Mailbox, Message, MessageLocation

router = APIRouter(prefix="/api/messages", tags=["actions"], dependencies=[Depends(require_ui_auth)])


def _get_message(db: DBSession, message_id: int) -> Message:
    msg = db.get(Message, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return msg


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


class BulkMoveRequest(BulkTrashRequest):
    """The ticked rows, and the one folder they are all going into."""
    target_mailbox_id: int


class BulkMoveAllRequest(BulkTrashAllRequest):
    """Everything matching the list selector, into one folder.

    ``mailbox_id`` is the source — the folder on screen — and
    ``target_mailbox_id`` is where it all goes. Two ids for what reads as one
    operation, because the selector is the same object the list was built from
    and the destination is not part of it.
    """
    target_mailbox_id: int


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
    # One id for the whole selection, across accounts: the user pressed Delete
    # once, and Undo puts back what that keypress took, not one account's share
    # of it.
    op_id = undo.new_op_id()

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
        target = mailops.trash_mailbox(db, account_id)
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
            # round trip each, immediately below in move_messages.
            .options(selectinload(Message.locations))
        ).scalars().all()
        if not msgs:
            continue
        moved += mailops.move_messages(db, msgs, target, touched, op_id, "trash")
        accounts.add(account_id)

    mailops.recompute(db, touched)
    db.commit()
    mailops.announce(db, accounts, moved)
    return {"ok": True, "moved": moved, "op_id": op_id}


def _move_target(db: DBSession, mailbox_id: int) -> Mailbox:
    target = db.get(Mailbox, mailbox_id)
    if target is None:
        raise HTTPException(status_code=400, detail="Invalid target mailbox")
    return target


@router.post("/bulk/move")
def bulk_move(req: BulkMoveRequest, db: DBSession = Depends(get_db)):
    """File a set of selected rows into one folder.

    The single-message move done to a selection, and shaped like bulk_trash
    rather than like a loop over it: the rows are gathered into one query and
    one operation, so two hundred conversations is one entry in the panel and
    one Undo, not two hundred requests each racing the list refresh behind them.

    One account, because a move is: IMAP has no way to move a message between
    two mailboxes on different servers, and the destination is one folder that
    belongs to one of them. A selection spanning accounts — which the unified
    inbox makes easy to build — is refused rather than half-applied to the rows
    that happen to match, since the other half would silently stay put.
    """
    target = _move_target(db, req.target_mailbox_id)
    if any(item.account_id != target.account_id for item in req.items):
        raise HTTPException(
            status_code=400,
            detail="These messages are not all in the same account as that folder. "
                   "Mail can only be moved within one account.")

    threads = {item.thread_id for item in req.items if item.thread_id}
    loose = {item.message_id for item in req.items if not item.thread_id and item.message_id}
    match = []
    if threads:
        match.append(Message.thread_id.in_(threads))
    if loose:
        match.append(Message.id.in_(loose))
    if not match:
        return {"ok": True, "moved": 0, "op_id": None}

    msgs = db.execute(
        select(Message)
        .where(Message.account_id == target.account_id, or_(*match))
        # See bulk_trash: without this every message lazy-loads its own
        # placements, one round trip each, inside move_messages.
        .options(selectinload(Message.locations))
    ).scalars().all()

    touched: set[int] = set()
    op_id = undo.new_op_id()
    moved = mailops.move_messages(db, msgs, target, touched, op_id, "move")
    mailops.recompute(db, touched)
    db.commit()
    mailops.announce(db, {target.account_id}, moved)
    return {"ok": True, "moved": moved, "op_id": op_id}


# A folder-wide delete is chunked rather than done in one transaction: every
# placement becomes a PendingAction row, so a 40k-message folder is a very long
# request and a very large commit. The client loops on `done`, which also gives
# it something honest to show a progress count from.
BULK_ALL_CHUNK = 2000


@router.post("/bulk/trash-all")
def bulk_trash_all(req: BulkTrashAllRequest, db: DBSession = Depends(get_db)):
    """Trash everything matching a list selector, up to one chunk at a time.

    Trash is where this puts mail, and so it is also the one place this will not
    touch. Placements already in Trash are excluded by the query, and a selector
    that names the Trash folder itself is refused with a pointer to Empty Trash
    (below), which is the only operation in meerail that destroys mail.

    Both of those used to be the same code path: a placement that was already in
    Trash was quietly re-read as "the user means delete this for good". That is
    right for someone standing in Trash pressing Delete, and catastrophic for
    every other way of reaching it — `Delete all Flagged` selects flagged
    messages from *every* folder, so a flagged message sitting in Trash was
    permanently deleted by a button whose name never said so. Worse, the
    optimistic Trash placement this endpoint writes keeps the Flagged flag, so on
    a selection larger than one chunk the second chunk found the messages the
    first had just trashed and destroyed those too.

    Excluding Trash from the selector is also what keeps the client's loop
    finite: a placement that cannot move is not counted as remaining, so `done`
    can actually become true.
    """
    trash_ids = mailops.trash_ids(db)
    q = select(MessageLocation).where(MessageLocation.deleted.is_(False))
    if req.scope == "flagged":
        q = q.where(MessageLocation.flagged.is_(True))
    else:
        ids = _resolve_mailbox_ids(db, req.mailbox_id, req.scope)
        if not ids:
            raise HTTPException(status_code=400, detail="No mailbox selected")
        if trash_ids.issuperset(ids):
            raise HTTPException(
                status_code=400,
                detail="These messages are already in Trash. Emptying the Trash deletes "
                       "them from the server for good, which is a separate action.")
        q = q.where(MessageLocation.mailbox_id.in_(ids))
    if trash_ids:
        q = q.where(MessageLocation.mailbox_id.not_in(trash_ids))
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
    trash_of: dict[int, Mailbox] = {}   # account_id -> its Trash, looked up once
    collapse_for: dict[int, bool] = {}  # ...and whether it files by label
    moved = 0
    # One id per chunk, not per folder-wide delete: the client calls this in a
    # loop until `done`, and each call is a transaction of its own that has
    # already been committed by the time the next one runs. Grouping them under
    # a single id would offer an Undo that could only ever put back the part
    # that had not been committed yet. A 40k-message delete is therefore twenty
    # undoable operations, which is honest about what each one can take back.
    op_id = undo.new_op_id()

    # Grouped by message, not walked placement by placement. On a server where
    # folders are labels, the placements in INBOX, in a user label and in \All
    # are one message seen three times, and the first move takes the other two
    # with it — so queueing a move for each meant two commands addressing a UID
    # the first had just retired ("Message does not exist"), retried and
    # eventually settled having achieved nothing. move_messages has done it this
    # way since that was found; this endpoint was walking the raw rows and had
    # the bug back.
    by_message: dict[int, list[MessageLocation]] = {}
    for loc in locs:
        msg = by_id.get(loc.message_pk)
        if msg is None:
            # Orphaned placement. Drop it here rather than skipping: it would
            # match the selector again on the next chunk and stall the loop.
            db.delete(loc)
            touched.add(loc.mailbox_id)
            continue
        by_message.setdefault(loc.message_pk, []).append(loc)

    for message_pk, group in by_message.items():
        msg = by_id[message_pk]
        if msg.account_id not in trash_of:
            trash_of[msg.account_id] = mailops.trash_mailbox(db, msg.account_id)
            collapse_for[msg.account_id] = mailops.labels_one_message(db, msg.account_id)
        collapse = collapse_for[msg.account_id]
        # Snapshotted before the first move: move_to deletes out of
        # msg.locations as it goes.
        mailbox_ids = [loc.mailbox_id for loc in group]
        carrier = mailops._carrier(db, msg, mailbox_ids) if collapse else None
        snaps = mailops.snapshots(db, msg, mailbox_ids)
        for mailbox_id in mailbox_ids:
            if mailops.move_to(db, msg, mailbox_id, trash_of[msg.account_id], touched,
                               enqueue_action=not collapse or mailbox_id == carrier,
                               op_id=op_id, op_kind="trash", undo_from=snaps):
                moved += 1
        accounts.add(msg.account_id)

    mailops.recompute(db, touched)
    db.commit()
    mailops.announce(db, accounts, moved)
    return {"ok": True, "moved": moved, "done": remaining <= len(locs), "op_id": op_id}


@router.post("/bulk/move-all")
def bulk_move_all(req: BulkMoveAllRequest, db: DBSession = Depends(get_db)):
    """File everything matching a list selector into one folder, a chunk at a time.

    The whole-folder move: "everything in here belongs over there", which is
    what an import that landed in the wrong place needs and what ticking rows a
    page at a time cannot honestly do. Chunked and looped by the client exactly
    like bulk_trash_all, and for the same reason — a folder-wide operation is
    tens of thousands of placements, which is not one request.

    Two things keep that loop finite. Placements already in the destination are
    excluded, so a message that has arrived cannot match again; and the whole
    selector is narrowed to the destination's own account, which is what makes
    "move all flagged" — a selector that spans every account — mean the flagged
    mail this folder could actually take, rather than repeatedly matching mail
    no move will ever touch.
    """
    target = _move_target(db, req.target_mailbox_id)

    q = (select(MessageLocation)
         .join(Message, Message.id == MessageLocation.message_pk)
         .where(MessageLocation.deleted.is_(False),
                Message.account_id == target.account_id,
                MessageLocation.mailbox_id != target.id))
    if req.scope == "flagged":
        q = q.where(MessageLocation.flagged.is_(True))
    else:
        ids = _resolve_mailbox_ids(db, req.mailbox_id, req.scope)
        if not ids:
            raise HTTPException(status_code=400, detail="No mailbox selected")
        if ids == [target.id]:
            raise HTTPException(status_code=400,
                                detail=mailops.already_there(target))
        q = q.where(MessageLocation.mailbox_id.in_(ids))
    if req.unread_only:
        q = q.where(MessageLocation.seen.is_(False))

    remaining = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    locs = db.execute(q.limit(BULK_ALL_CHUNK)).scalars().all()

    by_id = {
        m.id: m for m in db.execute(
            select(Message).where(Message.id.in_({loc.message_pk for loc in locs}))
            .options(selectinload(Message.locations))
        ).scalars().all()
    } if locs else {}

    touched: set[int] = set()
    moved = 0
    # One id per chunk, like bulk_trash_all: each call is its own committed
    # transaction, so an id spanning them would offer an Undo that could only
    # take back the last one.
    op_id = undo.new_op_id()
    collapse = mailops.labels_one_message(db, target.account_id)

    # Grouped by message rather than walked placement by placement — see
    # bulk_trash_all, where doing the latter queued a second command against a
    # UID the first had just retired.
    by_message: dict[int, list[MessageLocation]] = {}
    for loc in locs:
        msg = by_id.get(loc.message_pk)
        if msg is None:
            # Orphaned placement: drop it here, or it matches the selector again
            # on the next chunk and the loop never finishes.
            db.delete(loc)
            touched.add(loc.mailbox_id)
            continue
        by_message.setdefault(loc.message_pk, []).append(loc)

    for message_pk, group in by_message.items():
        msg = by_id[message_pk]
        mailbox_ids = [loc.mailbox_id for loc in group]
        carrier = mailops._carrier(db, msg, mailbox_ids) if collapse else None
        snaps = mailops.snapshots(db, msg, mailbox_ids)
        for mailbox_id in mailbox_ids:
            if mailops.move_to(db, msg, mailbox_id, target, touched,
                               enqueue_action=not collapse or mailbox_id == carrier,
                               op_id=op_id, op_kind="move", undo_from=snaps):
                moved += 1

    mailops.recompute(db, touched)
    db.commit()
    mailops.announce(db, {target.account_id}, moved)
    return {"ok": True, "moved": moved, "done": remaining <= len(locs), "op_id": op_id}


class EmptyTrashRequest(BaseModel):
    """Which Trash to empty, and the caller saying out loud that it means it.

    `mailbox_id` is the Trash folder on screen; without one, every account's
    Trash is emptied (or one account's, with `account_id`). `confirm` defaults
    to false, so a client that forgets it gets an error rather than a deletion.
    """
    mailbox_id: int | None = None
    account_id: int | None = None
    confirm: bool = False


@router.post("/bulk/empty-trash")
def bulk_empty_trash(req: EmptyTrashRequest, db: DBSession = Depends(get_db)):
    """Delete the contents of Trash from the server, for good.

    The only operation in meerail that destroys mail, and the only one that says
    so in its name. Everything else — trash, archive, move, the bulk versions of
    all three — files a message somewhere it can be got back from; when one of
    those *inferred* a permanent delete from where the message happened to be
    sitting, the destruction arrived under a button labelled something else. So
    this is its own endpoint, it takes an explicit `confirm`, and nothing else
    can reach it by accident.

    Chunked like bulk_trash_all, for the same reason: every placement becomes a
    queue row for the agent, and a Trash holding 40k messages is not one request.

    Placements the server has not seen yet are left alone. Those are moves to
    Trash that are still queued (core.mail.store.place_pending): the message is,
    as far as any mail server knows, still sitting in the folder it came from,
    and "empty the Trash" is not permission to delete it there. It stays in Trash
    until the move lands, and the next empty takes it.
    """
    if not req.confirm:
        raise HTTPException(status_code=400,
                            detail="Emptying the Trash deletes mail for good and has to be "
                                   "confirmed explicitly")
    ids = mailops.trash_ids(db)
    if req.mailbox_id is not None:
        if req.mailbox_id not in ids:
            raise HTTPException(status_code=400, detail="That folder is not a Trash folder")
        ids = {req.mailbox_id}
    elif req.account_id is not None:
        ids = set(db.execute(
            select(Mailbox.id).where(Mailbox.account_id == req.account_id,
                                     Mailbox.role == "trash")
        ).scalars().all())
    if not ids:
        raise HTTPException(status_code=400, detail="This account has no Trash folder")

    q = (select(MessageLocation)
         .where(MessageLocation.mailbox_id.in_(ids), MessageLocation.imap_uid > 0))
    remaining = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    locs = db.execute(q.limit(BULK_ALL_CHUNK)).scalars().all()

    by_id = {
        m.id: m for m in db.execute(
            select(Message).where(Message.id.in_({loc.message_pk for loc in locs}))
            .options(selectinload(Message.locations))
        ).scalars().all()
    } if locs else {}

    touched: set[int] = set()
    accounts: set[int] = set()
    deleted = 0
    op_id = undo.new_op_id()      # for the panel only; see move_to's delete branch
    for loc in locs:
        msg = by_id.get(loc.message_pk)
        if msg is None:
            db.delete(loc)
            touched.add(loc.mailbox_id)
            continue
        # IMAP \Deleted + UID EXPUNGE
        mailops.move_to(db, msg, loc.mailbox_id, None, touched, op_id=op_id, op_kind="delete")
        accounts.add(msg.account_id)
        deleted += 1

    mailops.recompute(db, touched)
    db.commit()
    mailops.announce(db, accounts, deleted)
    return {"ok": True, "deleted": deleted, "done": remaining <= len(locs)}


@router.post("/{message_id}/mark")
def mark(message_id: int, seen: bool = True, db: DBSession = Depends(get_db)):
    msg = _get_message(db, message_id)
    touched = mailops.set_seen(db, msg, seen)
    mailops.recompute(db, touched)
    db.commit()
    events.publish({"type": "flags", "message_id": message_id})
    return {"ok": True, "seen": seen}


@router.post("/{message_id}/flag")
def flag(message_id: int, flagged: bool = True, db: DBSession = Depends(get_db)):
    msg = _get_message(db, message_id)
    touched = mailops.set_flagged(db, msg, flagged)
    mailops.recompute(db, touched)
    db.commit()
    events.publish({"type": "flags", "message_id": message_id})
    return {"ok": True, "flagged": flagged}


@router.post("/{message_id}/trash")
def trash(message_id: int, source_mailbox_id: int, db: DBSession = Depends(get_db)):
    msg = _get_message(db, message_id)
    target = mailops.trash_mailbox(db, msg.account_id)
    op_id = undo.new_op_id()
    if not mailops.move_to(db, msg, source_mailbox_id, target, op_id=op_id, op_kind="trash"):
        raise HTTPException(status_code=409, detail=mailops.already_there(target))
    db.commit()
    mailops.wake_agent(db, msg.account_id)
    events.publish({"type": "present", "message_id": message_id})
    # The operation id goes back to the browser so the Recent actions panel can
    # show the entry the moment the keypress lands, rather than waiting for the
    # event that says the list changed. See app/static/js/app.undo.js.
    return {"ok": True, "op_id": op_id}


@router.post("/{message_id}/archive")
def archive(message_id: int, source_mailbox_id: int, db: DBSession = Depends(get_db)):
    msg = _get_message(db, message_id)
    target = mailops.require_archive_mailbox(db, msg.account_id)
    op_id = undo.new_op_id()
    if not mailops.move_to(db, msg, source_mailbox_id, target, op_id=op_id, op_kind="archive"):
        raise HTTPException(status_code=409, detail=mailops.already_there(target))
    db.commit()
    mailops.wake_agent(db, msg.account_id)
    events.publish({"type": "present", "message_id": message_id})
    # See trash(), above.
    return {"ok": True, "op_id": op_id}


def _thread_move(db: DBSession, thread_id: str, account_id: int, target: Mailbox | None,
                 op_kind: str = "move") -> tuple[int, str]:
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
    # One id for the conversation: archiving a thread is one keypress, and Undo
    # puts the whole conversation back rather than the message the reader
    # happened to have open.
    op_id = undo.new_op_id()
    moved = mailops.move_messages(db, msgs, target, touched, op_id, op_kind)
    if not moved and target is not None:
        # Every placement was already in the target folder, so the conversation
        # is where it was being asked to go. Said rather than reported as a
        # success that moves nothing — see move_to, which answers the
        # single-message version of the same keypress.
        raise HTTPException(status_code=409, detail=mailops.already_there(target))
    mailops.recompute(db, touched)
    db.commit()
    mailops.wake_agent(db, msgs[0].account_id)
    # One event for the conversation, not one per message: each publish() is a
    # pg_notify round trip of its own (see mailops.announce), and nothing reads
    # the message_id off these — to the UI they are just "something changed".
    events.publish({"type": "present", "moved": moved})
    return moved, op_id


@router.post("/threads/{thread_id:path}/archive")
def archive_thread(thread_id: str, account_id: int, db: DBSession = Depends(get_db)):
    target = mailops.require_archive_mailbox(db, account_id)
    moved, op_id = _thread_move(db, thread_id, account_id, target, "archive")
    return {"ok": True, "moved": moved, "op_id": op_id}


@router.post("/threads/{thread_id:path}/trash")
def trash_thread(thread_id: str, account_id: int, db: DBSession = Depends(get_db)):
    target = mailops.trash_mailbox(db, account_id)
    moved, op_id = _thread_move(db, thread_id, account_id, target, "trash")
    return {"ok": True, "moved": moved, "op_id": op_id}


@router.post("/{message_id}/move")
def move(message_id: int, mailbox_id: int, source_mailbox_id: int, db: DBSession = Depends(get_db)):
    msg = _get_message(db, message_id)
    target = db.get(Mailbox, mailbox_id)
    if target is None or target.account_id != msg.account_id:
        raise HTTPException(status_code=400, detail="Invalid target mailbox")
    op_id = undo.new_op_id()
    if not mailops.move_to(db, msg, source_mailbox_id, target, op_id=op_id, op_kind="move"):
        raise HTTPException(status_code=409, detail=mailops.already_there(target))
    db.commit()
    mailops.wake_agent(db, msg.account_id)
    events.publish({"type": "present", "message_id": message_id})
    # See trash(), above.
    return {"ok": True, "op_id": op_id}
