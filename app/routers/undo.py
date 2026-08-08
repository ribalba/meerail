"""Recent actions, and taking one back.

What can be undone is deliberately narrow: the actions that file mail somewhere
else — trash, archive, move, and the bulk versions of all three. See core/undo.py
for why, and for the two payload keys everything here reads.

There are exactly three things that can happen when Undo is pressed, and which
one it is depends only on how far the queued move has got:

*The agent has not touched it yet.* Nothing has been said to any mail server, so
the move is deleted out of the queue and the placements it removed are put back
verbatim — same folder, same UID, same flags. This is a true undo: afterwards
there is no record on either side that anything happened.

*The agent has it right now, or has just applied it.* There is no answer yet.
Mid-apply, the row is leased and rewriting it is the race that once left a
message in Trash on the server and in Archive here, for good
(mailops._retarget_pending). Just-applied, the move has run but the sync has not
brought the server's copy back, so the message is sitting on an optimistic
placement whose UID no server has heard of and there is nothing to address a
reverse move to. Both are refused with the same sentence, because to the person
pressing the button they are the same situation: wait a moment and press again.

*The move has landed and synced.* Undo is then an ordinary move in the opposite
direction, queued exactly like the one it reverses — which is why this is short:
``mailops.move_to`` already knows how to name a message to the agent, write the
optimistic placement, and refuse the two cases above.

One of those reverse moves may have nowhere to go: a message filed *out of*
\\All was never taken out of anything, and \\All is not a folder mail can be put
into. Those rows are skipped, one by one, and the rest of the operation runs —
see _reverse. Only an operation where every row is like that is refused, and
then nothing is marked and nothing changes. Undoing half a keypress is not a
thing anyone asked for, but neither is refusing a whole conversation over the
one message in it that was already filed.

One caveat, on servers where folders are labels. Trashing a message on Proton or
Gmail clears every label it had, and the reverse move puts back the one the move
was queued from. A message that was in the inbox *and* under two labels comes
back to the inbox. The alternative would be to re-apply the other labels here,
which means claiming placements the server has not got and watching the next sync
prune them — a worse lie than the honest partial restore.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, distinct, func, or_, select, tuple_
from sqlalchemy.orm import Session as DBSession

from .. import events, journal, mailops, reminders
from core import undo
from core.database import get_db
from core.mail.store import drop_pending_placement, recompute_counts
from core.models import Mailbox, Message, PendingAction, utcnow
from ..deps import require_ui_auth

router = APIRouter(prefix="/api/actions", tags=["undo"], dependencies=[Depends(require_ui_auth)])

# How many operations the panel lists.
DEFAULT_LIMIT = 5
MAX_LIMIT = 100

# How far back down the queue table to look for them.
#
# The panel is read on every sidebar refresh, and grouping by a JSONB key is not
# something an index can help with — so the work is bounded by primary key
# instead, which is a range scan. Actions are only ever appended, so the newest
# ids are the newest operations; anything older than this window is history
# nobody is going to press Undo on. A number large enough to cover several
# folder-wide deletes (2,000 rows each) and still cheap.
SCAN_ROWS = 20_000

# Statuses that mean the agent has finished with a row one way or another.
DONE = "done"

# The agent could not tell which message the row named — the folder was rebuilt
# under it, so its UID now points at whatever inherited that number, or at
# nothing. This is the one end state that genuinely blocks an undo: there is no
# message to put back and no safe way to guess which one was meant. ("error" is
# legacy; nothing writes it any more — see agent/actions.py::drain_actions.)
#
# Deliberately not "refused", which is the other way a row ends without running
# and reads as its opposite. The server said no to the *destination*, so the
# move never happened and the agent has already taken back the optimistic
# placement it wrote (agent/actions.py::_settle_refused) — taking the operation
# back is then the same local matter as a row still sitting in the queue, and it
# falls through to _cancel like one. Holding the two in one tuple is what
# answered a refused reminder park with a sentence about rebuilt folders and
# stale UIDs: a wrong diagnosis, pointing at a fix for a problem the account did
# not have, on the one action the user most needed back.
UNMATCHABLE = ("stale", "error")


def _op_column():
    return PendingAction.payload["op_id"].astext


@router.get("/recent")
def recent(limit: int = DEFAULT_LIMIT, db: DBSession = Depends(get_db)):
    """The last few things that moved mail, newest first, one row per keypress.

    Grouped by ``op_id`` rather than listed per queue row: a bulk trash of two
    hundred conversations is one thing the user did, and a panel that showed it
    as two hundred lines would bury everything before it.

    An operation that has been undone is not listed at all. It used to stay,
    greyed, saying "Already undone" — which reads as a list of things you did,
    including the undoing, and that is not what the panel is: it is the set of
    filings you can still take back. Pressing Undo makes the entry go, which is
    the plainest possible confirmation that it worked. The one case that leaves
    a trace is a move the mail server had already made, because reversing that
    queues a real move of its own, and that move is a new entry ("Put back")
    which is itself undoable.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    op = _op_column()

    floor = db.scalar(select(func.max(PendingAction.id))) or 0
    rows = db.execute(
        select(
            op.label("op_id"),
            func.count().label("n"),
            func.min(PendingAction.created_at).label("at"),
            func.max(PendingAction.id).label("last_id"),
            func.count().filter(PendingAction.status.in_(("pending", "leased"))).label("queued"),
            func.count().filter(PendingAction.status.in_(UNMATCHABLE)).label("blocked"),
        )
        .where(PendingAction.id > floor - SCAN_ROWS,
               PendingAction.type.in_(undo.LOGGED_TYPES),
               op.is_not(None))
        .group_by(op)
        # Dropped by the group rather than by the row, so an operation whose undo
        # got part way — some messages restored, one refused because the agent
        # had it — stays on the list with its Undo, rather than half vanishing.
        .having(func.count().filter(PendingAction.payload.has_key("undone_at")) == 0)
        .order_by(func.max(PendingAction.id).desc())
        .limit(limit)
    ).all()
    if not rows:
        return {"items": []}

    # One representative row per operation, for the subject and the destination.
    # The newest of the group, so that an operation whose first message has since
    # been re-filed still describes itself from a row that is current.
    reps = {
        action.id: (action, subject)
        for action, subject in db.execute(
            select(PendingAction, Message.subject)
            .outerjoin(Message, Message.id == PendingAction.message_pk)
            .where(PendingAction.id.in_([r.last_id for r in rows]))
        ).all()
    }

    unreversible = _unreversible_counts(db, [r.op_id for r in rows])
    threads = _thread_titles(db, [r.op_id for r in rows], floor)

    items = []
    for row in rows:
        rep = reps.get(row.last_id)
        if rep is None:
            continue
        action, subject = rep
        payload = action.payload = action.payload or {}
        home = _home(db, action, payload)
        undoable, why = _undoability(row, payload, home,
                                     unreversible.get(row.op_id, 0))
        items.append({
            "op_id": row.op_id,
            "kind": payload.get("op_kind") or action.type,
            "at": row.at.isoformat() if row.at else None,
            "count": row.n,
            "subject": subject or "(no subject)",
            # Set only when the whole operation was one conversation, which is
            # then what the panel names instead of counting messages.
            "thread": threads.get(row.op_id),
            "to": _folder_name(db, action.account_id, payload.get("to_folder")),
            "from": _folder_name(db, action.account_id,
                                 payload.get("from_folder") or payload.get("folder")),
            "undoable": undoable,
            "reason": why,
        })
    return {"items": items}


def _unwritable(home: Mailbox | None) -> str | None:
    """Why this folder cannot be moved back into, if it cannot.

    Both cases are the same folder on a server where folders are labels. \\All is
    the union of everything the account holds rather than somewhere a message
    sits, and filing *into* it is a request Proton answers with "operation not
    allowed" — which is also what sets ``writes_refused_at``, the record of an
    agent having been told exactly that.

    Undo has to ask, because the ordinary move path never reaches this: the
    keypress that files mail picks its own destination through
    ``mailops.archive_mailbox``, which drops \\All the moment it has been refused once.
    An undo does not pick anything — it goes back to the folder named on the
    action, and on a label server that folder is very often \\All, because \\All
    is where all the mail is. So undoing an archive queued a move the server had
    already refused, once per message, and the failures arrived minutes later as
    a wall of red under a button that had reported success.

    Nothing is lost by skipping such a row. A move out of \\All was a *copy* on
    the way in (agent/actions.py::apply_action) — the message never left \\All,
    and the server still has it there — so there is no placement to restore and
    the undo would have achieved nothing even if the server had taken it.

    Far fewer moves reach here than used to. ``mailops.move_to`` no longer
    plans a move out of \\All into a folder the message already occupies, which
    was most of them; what is left is the message whose only placement anywhere
    is \\All, and for that one this is still the answer.
    """
    if home is None:
        return None
    where = home.display_name or home.imap_name
    if home.role == "all":
        return (f"{where} is this account's All Mail — the union of everything it holds, "
                f"not a folder mail can be filed into. Nothing was taken out of it when "
                f"this was filed, so there is nothing to put back.")
    if home.writes_refused_at is not None:
        return (f"The mail server has refused mail into {where}, so this cannot be put "
                f"back there. It is still wherever the server has it now.")
    return None


def _unreversible_counts(db: DBSession, op_ids: list[str]) -> dict[str, int]:
    """How many rows of each operation have nowhere to be put back to.

    Counted per operation, because the panel used to ask this of the *one*
    representative row it had already loaded — and an operation's rows do not
    agree. Archiving a conversation queues one per message, each naming the
    folder that message came out of, and on a label server the ones archived
    long ago come out of \\All while today's comes out of the inbox. Which
    answer the panel got then depended on which row happened to be newest, so
    the same operation was undoable or not by coin flip.

    The join is what decides "unreversible": a ``from_folder`` naming a mailbox
    _unwritable would reject. Bounded by the twelve operations on screen.
    """
    if not op_ids:
        return {}
    op = _op_column()
    rows = db.execute(
        select(op, func.count())
        .select_from(PendingAction)
        .join(Mailbox,
              and_(Mailbox.account_id == PendingAction.account_id,
                   Mailbox.imap_name == PendingAction.payload["from_folder"].astext))
        .where(op.in_(op_ids),
               or_(Mailbox.role == "all", Mailbox.writes_refused_at.is_not(None)))
        .group_by(op)
    ).all()
    return {op_id: count for op_id, count in rows}


def _thread_titles(db: DBSession, op_ids: list[str], floor: int) -> dict[str, str]:
    """The conversation each operation filed, for the operations that were one.

    "Archived 4 messages" describes the shape of what happened rather than the
    thing that was done: archiving a conversation is one keypress about one
    subject, and the four is an implementation detail of how many mails that
    conversation happens to hold. So an operation whose rows all belong to a
    single thread is named by that thread, and only a genuinely mixed selection
    falls back to a count.

    Two conditions, both necessary. Every row must carry a thread, because a
    message that never got threaded is a conversation of its own and an
    operation holding one is not the single named thread it would otherwise
    look like. And the operation must not span accounts — a bulk action can —
    since thread ids are only unique within one.

    The title is the newest message's subject, ordered exactly as the list and
    the reader order it (messages.py::list_messages), so the panel says back
    the same words that were on screen when the key was pressed rather than the
    original subject of a thread the user has only ever seen as "Re:".

    Bounded by the same ``floor`` window as the query that chose the operations,
    for the same reason: matching on a JSONB key is a sequential scan of the
    queue table, and the panel is read on every sidebar refresh.
    """
    if not op_ids:
        return {}
    op = _op_column()
    rows = db.execute(
        select(op.label("op_id"),
               PendingAction.account_id,
               func.count(distinct(Message.thread_id)).label("threads"),
               func.count().filter(Message.thread_id.is_(None)).label("loose"),
               func.min(Message.thread_id).label("thread_id"))
        .select_from(PendingAction)
        .outerjoin(Message, Message.id == PendingAction.message_pk)
        .where(PendingAction.id > floor - SCAN_ROWS,
               op.in_(op_ids), PendingAction.type.in_(undo.LOGGED_TYPES))
        .group_by(op, PendingAction.account_id)
    ).all()

    single: dict[str, tuple[int, str] | None] = {}
    for row in rows:
        key = (row.account_id, row.thread_id) if row.threads == 1 and not row.loose else None
        # An operation that reached two accounts arrives here as two groups.
        # Seeing it twice is itself the answer: it is not one conversation.
        single[row.op_id] = None if row.op_id in single else key
    keys = {key for key in single.values() if key}
    if not keys:
        return {}

    titles = {
        (account_id, thread_id): subject
        for account_id, thread_id, subject in db.execute(
            select(Message.account_id, Message.thread_id, Message.subject)
            .where(tuple_(Message.account_id, Message.thread_id).in_(keys))
            .distinct(Message.account_id, Message.thread_id)
            .order_by(Message.account_id, Message.thread_id,
                      Message.date_sent.desc().nulls_last(), Message.id.desc())
        ).all()
    }
    return {op_id: titles[key] for op_id, key in single.items()
            if key and titles.get(key)}


def _undoability(row, payload: dict, home: Mailbox | None,
                 unreversible: int) -> tuple[bool, str | None]:
    """Whether the panel should offer Undo on this operation, and why not.

    Only reasons that will still be true in a minute belong here. The two states
    the agent might be in — holding the row, or having just applied it — are
    deliberately *not* among them: they last seconds, only the request itself can
    tell them apart, and a panel that refreshes on a debounce would go on
    refusing for a while after the moment had passed. Pressing Undo through one
    of those gets a sentence saying to try again, which the panel then shows
    against the row (see app.undo.js) until the next press succeeds.
    """
    if payload.get("op_kind") == "delete":
        return False, ("Emptying the Trash deletes mail from the mail server. "
                       "There is nothing left to put back.")
    # Asked only of an operation that has reached the server, and only when
    # *every* one of its rows is unreversible. While the actions are still
    # queued, undoing is a local matter — the row comes out of the queue and the
    # placements go back — and no folder has to accept anything. And a single
    # unreversible row among reversible ones is skipped rather than fatal (see
    # _reverse), so the button still has work to do.
    if row.queued < row.n and unreversible >= row.n:
        refused = _unwritable(home)
        if refused:
            return False, refused
    if row.blocked:
        return False, ("The folder these came from has been rebuilt since, so they can no "
                       "longer be found by the UIDs this recorded.")
    return True, None


def _folder_name(db: DBSession, account_id: int, imap_name: str | None) -> str | None:
    if not imap_name:
        return None
    mb = mailops.mailbox_by_name(db, account_id, imap_name)
    return (mb.display_name or mb.imap_name) if mb is not None else imap_name


# The sentence both mid-flight cases get. One sentence rather than two, because
# "the agent is holding this row" and "the move ran and the folder has not been
# re-read" are the same fact to the person who pressed the button.
SYNCING = ("This is still being synced to the mail server, so it cannot be undone yet — "
           "try again in a moment.")


@router.post("/{op_id}/undo")
def undo_operation(op_id: str, db: DBSession = Depends(get_db)):
    """Take back everything one keypress did.

    All of it or none of it. The rows are locked for the duration, which is what
    keeps an agent from leasing one of them halfway through — it uses
    ``SKIP LOCKED`` (agent/actions.py::_lease), so it passes over anything held
    here rather than blocking behind it.
    """
    actions = db.execute(
        select(PendingAction)
        .where(_op_column() == op_id)
        .order_by(PendingAction.id)
        .with_for_update()
    ).scalars().all()
    if not actions:
        raise HTTPException(status_code=404, detail="That action is no longer on record")

    first = actions[0].payload or {}
    if first.get("op_kind") == "delete" or any(a.type == "delete" for a in actions):
        raise HTTPException(
            status_code=400,
            detail="Emptying the Trash deletes mail from the mail server, so there is "
                   "nothing left here to put back.")
    if all((a.payload or {}).get("undone_at") for a in actions):
        raise HTTPException(status_code=409, detail="This has already been undone.")
    if any(a.status in UNMATCHABLE for a in actions):
        raise HTTPException(
            status_code=400,
            detail="The folder these messages came from has been rebuilt since they were "
                   "moved, so they can no longer be found by the UIDs this recorded. "
                   "They are wherever the mail server has them; the next sync will show "
                   "where that is.")
    if any(a.status == "leased" for a in actions):
        raise HTTPException(status_code=409, detail=SYNCING)

    touched: set[int] = set()
    accounts: set[int] = set()
    reverse_op = undo.new_op_id()
    restored = 0
    # Reasons collected from rows that had nowhere to go back to. Only consulted
    # if *nothing* was put back — one skipped message among restored ones is a
    # partial undo, which is the honest outcome on a label server and not worth
    # interrupting the user over.
    unreversible: list[str] = []
    # Before the placements move, because it is keyed on where the conversation
    # is *now* — and because the promise is the more important half. A placement
    # restored while a live reminder still points at the conversation would be
    # parked all over again the moment the reminder came due, by something the
    # user had every reason to think they had just cancelled.
    if first.get("op_kind") == "remind":
        _cancel_reminder(db, actions)
    for action in actions:
        if (action.payload or {}).get("undone_at"):
            continue                      # a retry finishing what a 409 interrupted
        if action.status == DONE:
            restored += _reverse(db, action, touched, reverse_op, unreversible)
        else:
            restored += _cancel(db, action, touched)
        accounts.add(action.account_id)

    # Nothing at all could be put back, and now it is worth saying — the reason
    # is the same one the panel shows, and raising rolls back the marks above so
    # the entry stays on the list rather than vanishing having done nothing.
    if not restored and unreversible:
        raise HTTPException(status_code=400, detail=unreversible[0])

    for mailbox_id in touched:
        mb = db.get(Mailbox, mailbox_id)
        if mb is not None:
            recompute_counts(db, mb)
    db.commit()

    # The reverse move is a move like any other, so it gets the same nudge: left
    # to the poll interval the message would be gone from the folder it was
    # undone out of and not yet back in the one it came from.
    for account_id in accounts:
        mailops.wake_agent(db, account_id)
    events.publish({"type": "present", "moved": restored})
    return {"ok": True, "restored": restored}


def _cancel_reminder(db: DBSession, actions: list[PendingAction]) -> None:
    """Retire the promise behind a parked conversation.

    Found by conversation rather than by an id on the action, which is what
    ``pending_for`` already does for every other caller — setting a reminder from
    a reply and finding it again from the root have to give the same row, and
    that logic should exist once. Nothing is stored on the action, so nothing can
    go stale.

    Silent when there is no pending reminder left: it may have fired in the
    seconds since, or been cancelled from the reminders view, and in both cases
    the placement half of this undo is still exactly what was asked for.
    """
    seen: set[int] = set()
    for action in actions:
        if action.message_pk is None or action.message_pk in seen:
            continue
        seen.add(action.message_pk)
        msg = db.get(Message, action.message_pk)
        if msg is None:
            continue
        reminder = reminders.pending_for(db, msg)
        if reminder is not None:
            reminders.cancel(db, reminder)
            # The other installs have to be told, and told *cancel* rather than
            # fired: undoing the parking is what puts this mail back, and that
            # move is already queued here. A "fired" record would have every
            # other machine retire the promise too — which is right — but the
            # distinction matters for what they do with a conversation that has
            # since been filed by hand, and the two are not interchangeable.
            journal.publish_reminder_op(db, reminder, "cancel")
            return          # one per conversation, and they are all one thread


def _cancel(db: DBSession, action: PendingAction, touched: set[int]) -> int:
    """Undo a move the agent has not applied yet: delete it and put things back.

    The real undo, and the only one that leaves no trace: no server was ever
    told, so there is nothing to tell it now. The optimistic placement the move
    wrote goes, and the placements it removed come back exactly as they were —
    which is what ``undo_from`` is for, since a move deletes those rows and on a
    label server queues only one action for however many it cleared.

    The row is kept, marked "undone", rather than deleted. The agent never
    selects it again (agent/actions.py::_claimable), and it is what the panel
    reads to show the operation as already taken back — a deleted row would
    simply vanish from the list, which reads as the Undo having failed.
    """
    msg = db.get(Message, action.message_pk) if action.message_pk else None
    if msg is None:
        # Nothing left to put back, and nothing left to apply either — so the
        # row is retired the same way a cancelled move is, which is what stops
        # the agent trying to move a message that is no longer there.
        _mark_undone(action, cancelled=True)
        return 0

    payload = action.payload or {}
    target = mailops.mailbox_by_name(db, action.account_id, payload.get("to_folder"))
    if target is not None:
        drop_pending_placement(db, msg.id, target.id)
        touched.add(target.id)

    try:
        restored = undo.restore(db, msg, payload.get("undo_from") or [], touched)
    except undo.Unrestorable as e:
        raise HTTPException(status_code=409, detail=f"This cannot be put back: {e}.") from e
    _mark_undone(action, cancelled=True)
    return restored


def _reverse(db: DBSession, action: PendingAction, touched: set[int], op_id: str,
             unreversible: list[str]) -> int:
    """Undo a move the mail server has already made: move it back.

    Not a restore — the server's copy is the truth now, and a row written here
    claiming the old folder would be pruned by the next sync. So this queues the
    opposite move and lets it travel the same path as the one it reverses.

    ``mailops.move_to`` is what raises for the two mid-flight cases: the message is
    sitting on an optimistic placement, from the move being undone or from
    another keypress since, and there is no UID on it that names anything to a
    mail server. Its wording is about moving rather than undoing, so it is caught
    and re-said here.
    """
    msg = db.get(Message, action.message_pk) if action.message_pk else None
    payload = action.payload or {}
    if msg is None or not payload.get("to_folder"):
        return 0

    home = _home(db, action, payload)
    current = mailops.mailbox_by_name(db, action.account_id, payload["to_folder"])
    if home is None or current is None:
        raise HTTPException(
            status_code=400,
            detail="One of the folders this moved between is no longer in the database, "
                   "so there is nowhere to put it back.")
    # Checked here as well as in the panel, and this is the one that counts:
    # queueing the move anyway is what produced a wall of "operation not
    # allowed" some minutes after an undo had reported success — one refused
    # action per message, none of which could ever have worked.
    #
    # Skipped, though, not fatal. Raising here cost the operation its *other*
    # messages: a conversation with one message already filed in \\All could not
    # be taken back at all, reminder included, because of a row whose reversal
    # would have achieved nothing anyway. Nothing is lost by passing over it —
    # the message never left \\All, so there is no placement to restore. Whether
    # the operation as a whole managed anything is decided by the caller.
    refused = _unwritable(home)
    if refused:
        unreversible.append(refused)
        _mark_undone(action, cancelled=False)
        return 0
    if not any(loc.mailbox_id == current.id for loc in msg.locations):
        # Already somewhere else — moved on by another window, or by a rule on
        # the server. Undoing would drag it back out of wherever it now is.
        return 0

    try:
        mailops.move_to(db, msg, current.id, home, touched, op_id=op_id, op_kind="undo")
    except HTTPException as e:
        if e.status_code == 409:
            raise HTTPException(status_code=409, detail=SYNCING) from e
        raise
    _mark_undone(action, cancelled=False)
    return 1


def _home(db: DBSession, action: PendingAction, payload: dict) -> Mailbox | None:
    """The folder a reverse move should aim at.

    ``from_folder`` and not the first entry in ``undo_from``: the snapshots
    describe every placement the move cleared, and on a label server that is
    three folders for one queued command. The one the agent was told to move
    *out of* is the one it can be told to move back into.
    """
    return mailops.mailbox_by_name(db, action.account_id, payload.get("from_folder"))


def _mark_undone(action: PendingAction, cancelled: bool) -> None:
    """Record that this row has been taken back, and how.

    ``undone_at`` goes on either way: it is what stops a second press undoing
    the same thing twice, and what the panel reads to show the operation as
    spent.

    The *status* only changes for a move that never ran. A move the agent
    applied stays "done", because it is — it happened, and what reverses it is
    the separate action queued alongside. Overwriting that with "undone" would
    make a move that reached the server indistinguishable from one that never
    left, which is the distinction everything from move_in_flight to the sync's
    repair sweep turns on.
    """
    # Payload replaced rather than mutated: it is plain JSONB, and an in-place
    # edit is committed as no change at all.
    action.payload = {**(action.payload or {}), "undone_at": utcnow().isoformat()}
    if cancelled:
        action.status = "undone"
    action.updated_at = utcnow()
