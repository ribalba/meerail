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
    until the move lands, and the next empty takes it — and comes back in
    `queued`, because a Trash where every message is one of those deletes
    nothing, and "nothing happened" is not an answer a button can give.

    On an imported account there is no server to expunge from and the row is the
    mail, so the same button deletes the rows — see mailops.purge, which is also
    what the `bulk/purge` routes below run. Emptying it through the queue path
    dropped the placement and left the message in the database in no folder at
    all: a Trash that reported itself emptied while every byte of it stayed.
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
    # The ones this route leaves alone, counted so the caller can say why it
    # did. A Trash holding nothing but queued moves deletes nothing and is
    # entitled to a reason: without this the answer is `deleted: 0, done: true`,
    # which the client can only render as the button having done nothing at all.
    queued = db.scalar(select(func.count()).select_from(
        select(MessageLocation.id)
        .where(MessageLocation.mailbox_id.in_(ids), MessageLocation.imap_uid <= 0)
        .subquery())) or 0

    by_id = {
        m.id: m for m in db.execute(
            select(Message).where(Message.id.in_({loc.message_pk for loc in locs}))
            .options(selectinload(Message.locations))
        ).scalars().all()
    } if locs else {}

    touched: set[int] = set()
    accounts: set[int] = set()
    deleted = 0
    # Collected and run in one statement after the loop rather than per message:
    # mailops.purge expires the session to keep the identity map honest, which
    # would pull `msg` out from under the rows still to be walked.
    purge_ids: list[int] = []
    op_id = undo.new_op_id()      # for the panel only; see move_to's delete branch
    for loc in locs:
        msg = by_id.get(loc.message_pk)
        if msg is None:
            db.delete(loc)
            touched.add(loc.mailbox_id)
            continue
        if mailops.is_local(db, msg.account_id):
            purge_ids.append(loc.id)
        else:
            # IMAP \Deleted + UID EXPUNGE
            mailops.move_to(db, msg, loc.mailbox_id, None, touched,
                            op_id=op_id, op_kind="delete")
        accounts.add(msg.account_id)
        deleted += 1
    mailops.purge(db, purge_ids, touched)

    mailops.recompute(db, touched)
    db.commit()
    mailops.announce(db, accounts, deleted)
    return {"ok": True, "deleted": deleted, "done": remaining <= len(locs),
            "queued": queued}


class BulkPurgeRequest(BulkTrashRequest):
    """The ticked rows, and the caller saying out loud that it means it."""
    confirm: bool = False


class BulkPurgeAllRequest(BulkTrashAllRequest):
    """Everything matching the list selector, and the same confirmation."""
    confirm: bool = False


# What a whole-folder permanent delete is refused with off an imported account,
# and why: this operation deletes rows, and on an account with a server behind
# it the rows are a copy. Deleting the copy achieves nothing except making the
# message vanish until the next pass fetches it again — which looks exactly like
# a delete that silently failed, hours later, with no button to blame. The
# per-conversation route has a second path for those accounts instead, because
# there the server can be told (see _expunge_from_trash); a folder-wide one has
# a name already, and it is Empty Trash.
NOT_LOCAL = ("Permanently deleting mail is for imported accounts, where this app holds "
             "the only copy. This account has a mail server behind it, which still has "
             "the message — delete it, then empty the Trash.")


def _require_local(db: DBSession, account_id: int) -> None:
    if not mailops.is_local(db, account_id):
        raise HTTPException(status_code=400, detail=NOT_LOCAL)


def _require_confirmed(confirm: bool) -> None:
    """`confirm` defaults to false, so a client that forgets it gets an error
    rather than a deletion — the same contract as Empty Trash, for the same
    reason."""
    if not confirm:
        raise HTTPException(status_code=400,
                            detail="Permanently deleting mail cannot be undone and has to "
                                   "be confirmed explicitly")


# What a permanent delete aimed at mail that is not in Trash is refused with on
# an account with a server behind it. Deleting the rows there would only hide a
# message the server still has, until the next pass fetched it again; what *can*
# be destroyed is what the server has already been told is rubbish, and this is
# the sentence that says which half of that a caller has hit.
NOT_IN_TRASH = ("This account has a mail server behind it, so mail can only be deleted "
                "for good once it is in Trash. Delete it first — that files it there — "
                "and then delete it again.")

# The gap between the app asking for a move to Trash and the agent having made
# it: there is no UID to expunge yet, and refusing for a moment is the honest
# answer. Same wording as the one move_to gives a re-aimed move mid-flight.
STILL_MOVING = "This message is still being moved to Trash — try again in a moment"


def _expunge_from_trash(db: DBSession, message_pks: set[int], touched: set[int],
                        op_id: str) -> int:
    """Tell the server to destroy these, for the accounts that have one.

    The single-conversation counterpart of Empty Trash, and it does exactly what
    that does to one message: \\Deleted plus a UID EXPUNGE against the placement
    sitting in Trash. Only that placement — a label server files the same message
    under \\All and whatever else it wears, and "delete this out of the Trash" is
    not permission to strip labels off mail elsewhere, any more than emptying the
    folder is.

    Mail that is not in Trash is refused rather than trashed-then-expunged. Two
    keypresses is what makes the destructive one deliberate, and inferring the
    first from the second is how a Delete on an inbox row would quietly become a
    delete nothing comes back from.
    """
    trash = mailops.trash_ids(db)
    msgs = db.execute(
        select(Message).where(Message.id.in_(message_pks))
        .options(selectinload(Message.locations))
    ).scalars().all()
    done = 0
    for msg in msgs:
        here = [loc for loc in msg.locations if loc.mailbox_id in trash]
        locs = [loc for loc in here if loc.imap_uid > 0]
        if not locs:
            raise HTTPException(status_code=409 if here else 400,
                                detail=STILL_MOVING if here else NOT_IN_TRASH)
        for loc in locs:
            mailops.move_to(db, msg, loc.mailbox_id, None, touched,
                            op_id=op_id, op_kind="delete")
        done += 1
    return done


@router.post("/bulk/purge")
def bulk_purge(req: BulkPurgeRequest, db: DBSession = Depends(get_db)):
    """Destroy the selected conversations.

    The answer to the question an imported mailbox actually asks — "I do not
    want a Trash, I want this gone" — and it is a separate route with a separate
    name for the same reason Empty Trash is: mail is only ever destroyed by
    something that says destroy in its name. Delete still means Trash here, and
    the Trash it means is made on the spot (mailops.trash_mailbox), so nothing
    that was recoverable before stops being.

    On an imported account every placement of every message goes, not just the
    one the folder on screen shows. Someone ticking a conversation and asking
    for it to be gone has not said anything about which folder they were
    standing in, and a copy left behind under another folder's name is the
    delete not having worked.

    An account with a server behind it takes the other path, and only from Trash
    (_expunge_from_trash). Deleting its rows here would achieve nothing — the
    server still has the message and the next pass fetches it back — which is
    why this route used to refuse those accounts outright. But refusing was the
    whole answer, and the folder that answer left broken was Trash itself:
    Delete on a message already in it moved it to where it already was, said
    "This is already in Trash", and offered nothing else. Emptying the whole
    folder was the only way to remove one message from it.

    There is no Undo either way. On an imported account the rows are the mail;
    on a server one the expunge is on its way out. The op_id is for the panel,
    so that "6 messages deleted" is something a person can see they did — see
    move_to's delete branch and mailops.purge.
    """
    _require_confirmed(req.confirm)

    # Grouped per account exactly like bulk_trash: one query per account rather
    # than a thread lookup per ticked row.
    by_account: dict[int, tuple[set[str], set[int]]] = {}
    for item in req.items:
        threads, loose = by_account.setdefault(item.account_id, (set(), set()))
        if item.thread_id:
            threads.add(item.thread_id)
        elif item.message_id:
            loose.add(item.message_id)

    accounts: set[int] = set()
    imported: set[int] = set()          # message pks whose rows are the mail
    served: dict[int, set[int]] = {}    # account -> message pks a server holds
    for account_id, (threads, loose) in by_account.items():
        match = []
        if threads:
            match.append(Message.thread_id.in_(threads))
        if loose:
            match.append(Message.id.in_(loose))
        if not match:
            continue
        pks = set(db.execute(
            select(Message.id).where(Message.account_id == account_id, or_(*match))
        ).scalars().all())
        if not pks:
            continue
        accounts.add(account_id)
        if mailops.is_local(db, account_id):
            imported.update(pks)
        else:
            served[account_id] = pks

    touched: set[int] = set()
    op_id = undo.new_op_id()      # for the panel only; see move_to's delete branch
    deleted = 0
    for pks in served.values():
        deleted += _expunge_from_trash(db, pks, touched, op_id)
    # Last, and after every expunge: purge expires the session, which would pull
    # the Message rows out from under a walk still to come.
    deleted += mailops.purge_messages(db, imported, touched)
    mailops.recompute(db, touched)
    db.commit()
    mailops.announce(db, accounts, deleted)
    return {"ok": True, "deleted": deleted}


@router.post("/bulk/purge-all")
def bulk_purge_all(req: BulkPurgeAllRequest, db: DBSession = Depends(get_db)):
    """Destroy everything a folder selector matches, a chunk at a time.

    The whole-folder version of bulk_purge — "this import went in the wrong
    place, take all of it back out" — chunked and looped by the client like
    bulk_trash_all, because forty thousand rows is not one request. `done` is
    reported against the count *before* the chunk, and every placement in the
    chunk is deleted, so the loop cannot fail to terminate the way a selector
    that keeps re-matching its own output can.

    `flagged` is refused rather than supported. That selector spans every
    account, which is how a flagged message sitting in someone's Trash was once
    destroyed by a button that never said so; a permanent delete aimed at a
    filter rather than a folder is that mistake with the safety catch removed.
    A folder is a place a person is standing in and can see the size of.

    `deleted` counts messages that stopped existing and `removed` counts
    placements taken out, and they differ: an mbox imported twice into two
    folders is one message wearing two placements, and emptying one of those
    folders removes a placement without destroying anything. The client loops on
    `removed`, because that is the number that says whether the chunk moved.
    """
    _require_confirmed(req.confirm)
    if req.scope == "flagged":
        raise HTTPException(
            status_code=400,
            detail="Permanently deleting everything flagged is not offered — flagged mail "
                   "spans every folder and account. Open a folder and delete that.")

    ids = _resolve_mailbox_ids(db, req.mailbox_id, req.scope)
    if not ids:
        raise HTTPException(status_code=400, detail="No mailbox selected")
    accounts = set(db.execute(
        select(Mailbox.account_id).where(Mailbox.id.in_(ids))
    ).scalars().all())
    for account_id in accounts:
        _require_local(db, account_id)

    conds = [MessageLocation.mailbox_id.in_(ids)]
    if req.unread_only:
        conds.append(MessageLocation.seen.is_(False))
    remaining = db.scalar(
        select(func.count()).select_from(MessageLocation).where(*conds)) or 0
    loc_ids = list(db.execute(
        select(MessageLocation.id).where(*conds).limit(BULK_ALL_CHUNK)).scalars().all())

    touched: set[int] = set()
    deleted = mailops.purge(db, loc_ids, touched)
    mailops.recompute(db, touched)
    db.commit()
    mailops.announce(db, accounts, deleted)
    return {"ok": True, "deleted": deleted, "removed": len(loc_ids),
            "done": remaining <= len(loc_ids)}


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
