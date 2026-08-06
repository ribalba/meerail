"""Remind me: a conversation that leaves the inbox now and comes back later.

The keypress means "not today". What it does is file the whole conversation
away, exactly as Archive does, and write down when to undo that — so the inbox
is the list of mail that is actually yours to deal with now, and Monday morning
brings back the things that were Monday's.

Three decisions are worth reading before the code:

**It parks in Archive.** Not in a Snoozed folder of its own: creating one is a
per-account round trip through the agent that can fail (or be refused by the
server) long after the button was pressed, and the mail has to go *somewhere*
the moment the key is pressed. Archive is a folder every account already has —
or, on Gmail-style servers, is \\All, which archiving already means there
(actions._archive_mailbox). The cost is that mail waiting on a reminder looks
like ordinary archived mail in other clients; the reminders view in this one is
what tells them apart, and firing a reminder is what puts them back.

**The server watches the clock, not the agent.** A reminder is this app's own
promise, like the Outbox — the agent knows only about the moves it is asked to
apply, and it goes to sleep whenever nothing has changed. The worker in
app/workers.py ticks over the due ones; a server that was down at nine o'clock
fires them when it comes back, late rather than never.

**Firing goes through the ordinary move queue.** Bringing a conversation back
writes the same PendingActions the Move button writes (actions._move_to), so it
inherits everything that already works there: the placement appears in the inbox
straight away, an unreachable mail server delays the write-back rather than
losing it, and a move that is still in flight is refused and retried rather than
addressed by a UID nobody has heard of.

**Parking is recorded in the undo panel; firing is not.** Setting a reminder is
one keypress that takes a conversation out of the inbox, and a list of what
recently moved your mail that omitted it was simply wrong — it made "remind me"
the one action that happened invisibly. So the park carries an ``op_id`` like
any other filing.

Undoing it has to take back the *promise* as well as the placement, and that is
the whole reason this was left out to begin with: mail put back in the inbox
while a reminder still points at it would be parked all over again when the
reminder came due, by a thing the user believed they had cancelled. So the undo
route cancels the reminder in the same transaction, found by conversation
(``pending_for``) rather than by anything stored on the action — see
app/routers/undo.py::_cancel_reminder.

Firing carries no ``op_id``. Nobody pressed anything: a reminder falling due at
nine on Monday is not an action to offer an Undo on, and the conversation
arriving in the inbox is its own notification. The strip over it is where "park
it again" lives.

What a reminder does *not* do is remember which messages it did not park —
see set_reminder, where a conversation already sitting in Archive is recorded as
coming back to the inbox, because "remind me about this" said over filed mail
means put it in front of me and there is nowhere else it could sensibly land.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession, selectinload

from core import undo
from core.events import publish_command
from core.models import Account, Mailbox, Message, Reminder, utcnow

from . import events
from .routers.actions import (
    _archive_mailbox, _move_messages, _move_to, _recompute, _role_mailbox, set_seen,
)

# How far ahead a reminder may be set. Not a judgement about what is a useful
# distance — it is what stops a mistyped year parking a conversation until 2226,
# somewhere the person who filed it will never think to look.
MAX_HORIZON = timedelta(days=730)

# ...and the floor. Anything sooner is a conversation that would leave the inbox
# and be put back before the list had finished redrawing.
MIN_LEAD = timedelta(seconds=30)

# How many due reminders one tick fires. A ceiling rather than a limit anybody
# will meet: reminders come due a handful at a time, and this only matters for
# the server that has been off for a fortnight and finds a month's worth waiting.
PER_TICK = 200


def normalize_due(raw: datetime) -> datetime:
    """The instant a reminder is for, in the naive UTC everything else is stored in.

    The browser is what works it out. "Next Monday, nine o'clock" is a question
    about the reader's own calendar and timezone, and the server has neither —
    it would have to be told the offset to answer it, and would then get the
    answer wrong twice a year at the DST boundary. So the presets are computed
    where the calendar is, and what arrives here is an absolute instant, which is
    the one form that still means the same thing when the same account is opened
    from a laptop in another country.
    """
    if raw.tzinfo is not None:
        raw = raw.astimezone(timezone.utc).replace(tzinfo=None)
    now = utcnow()
    if raw < now + MIN_LEAD:
        raise HTTPException(status_code=400, detail="That time has already passed")
    if raw > now + MAX_HORIZON:
        raise HTTPException(
            status_code=400, detail="A reminder can be set at most two years ahead")
    return raw


# --- Reading ---------------------------------------------------------------


def pending_for(db: DBSession, msg: Message) -> Reminder | None:
    """The reminder already set on this mail's conversation, if any.

    Keyed on the conversation rather than the message, because that is what a
    reminder acts on: setting one on a reply is setting it on the thread, and
    finding it again from a different message of the same thread has to give the
    same row or the second keypress would park an already-parked conversation.
    """
    q = select(Reminder).where(Reminder.account_id == msg.account_id,
                               Reminder.state == "pending")
    if msg.thread_id:
        q = q.where(Reminder.thread_id == msg.thread_id)
    else:
        q = q.where(Reminder.message_pk == msg.id)
    return db.execute(q.order_by(Reminder.id)).scalars().first()


def pending_by_thread(db: DBSession, keys: set[tuple[int, str]]) -> dict[tuple[int, str], datetime]:
    """When each of these (account, thread) conversations is due back.

    One query for a whole page of list rows, so the little clock beside a parked
    conversation costs the list nothing per row. Threads only: a message that was
    never threaded is keyed by its own id in the list too, and those come back
    through ``pending_by_message`` below.
    """
    if not keys:
        return {}
    thread_ids = {key for _, key in keys}
    rows = db.execute(
        select(Reminder.account_id, Reminder.thread_id, Reminder.due_at)
        .where(Reminder.state == "pending", Reminder.thread_id.in_(thread_ids))
    ).all()
    out = {(a, t): d for a, t, d in rows if (a, t) in keys}
    return out


def pending_by_message(db: DBSession, message_pks: set[int]) -> dict[int, datetime]:
    """The same, for mail that never got threaded."""
    if not message_pks:
        return {}
    rows = db.execute(
        select(Reminder.message_pk, Reminder.due_at)
        .where(Reminder.state == "pending",
               Reminder.thread_id.is_(None),
               Reminder.message_pk.in_(message_pks))
    ).all()
    return {pk: due for pk, due in rows}


def due(db: DBSession, now: datetime | None = None, limit: int = PER_TICK) -> list[Reminder]:
    """The reminders whose time has come, oldest deadline first."""
    return db.execute(
        select(Reminder)
        .where(Reminder.state == "pending", Reminder.due_at <= (now or utcnow()))
        .order_by(Reminder.due_at, Reminder.id)
        .limit(limit)
    ).scalars().all()


def describe(reminder: Reminder) -> dict:
    """One reminder as the UI reads it."""
    return {
        "id": reminder.id,
        "account_id": reminder.account_id,
        "message_id": reminder.message_pk,
        "thread_id": reminder.thread_id,
        "due_at": reminder.due_at,
        "created_at": reminder.created_at,
        "state": reminder.state,
        # The last thing that stopped this firing, kept while it goes on being
        # retried — not a verdict, and not a reason it will not happen. Same
        # convention as the Outbox (core/outbox.py).
        "error": reminder.error,
        "overdue": reminder.state == "pending" and reminder.due_at <= utcnow(),
    }


# --- Setting one -----------------------------------------------------------


def _conversation(db: DBSession, msg: Message) -> list[Message]:
    """Every message of this one's conversation, locations loaded.

    The whole thread, for the reason archive and trash take the whole thread
    (actions._thread_move): a conversation half-parked is a conversation still
    in the inbox. Resolved here rather than in the browser so a reply that
    arrived since the reader was drawn goes with it.
    """
    if not msg.thread_id:
        return [msg]
    return list(db.execute(
        select(Message)
        .where(Message.account_id == msg.account_id, Message.thread_id == msg.thread_id)
        .options(selectinload(Message.locations))
    ).scalars().all())


def set_reminder(db: DBSession, msg: Message, due_at: datetime) -> tuple[Reminder, str | None]:
    """Park this conversation and write down when to bring it back.

    A second reminder on a conversation that already has one only moves the
    deadline. Re-parking it would be wrong twice over: the mail is already in
    Archive, so there is nothing to move, and the folders it originally came out
    of — the only record of where it goes back to — would be overwritten with
    "Archive", which is where it is precisely because of the first reminder.
    """
    existing = pending_for(db, msg)
    if existing is not None:
        existing.due_at = due_at
        existing.error = None
        # No op id: nothing moved, so there is nothing new for the panel to
        # offer an Undo on. Changing a deadline is done from the reminders view.
        return existing, None

    park = _archive_mailbox(db, msg.account_id)
    if park is None:
        raise HTTPException(
            status_code=400,
            detail="This account has no Archive folder, so there is nowhere to keep a "
                   "reminder's mail until it is due. Create one on the server (or mark "
                   "an existing folder \\Archive) and sync again.")
    inbox = _role_mailbox(db, msg.account_id, "inbox")
    msgs = _conversation(db, msg)

    # Snapshotted before the move, because afterwards there is nothing left to
    # read it off: every placement this is about is deleted by _move_messages.
    parked = []
    for m in msgs:
        origins = [loc.mailbox_id for loc in m.locations if loc.mailbox_id != park.id]
        if not origins:
            # Already filed where this would park it — a reminder set from the
            # Archive folder, or on the sent half of a conversation whose
            # replies were archived earlier. Nothing moves for it now, and when
            # the reminder fires it goes to the inbox, which is the only place
            # "show me this again" can mean.
            if inbox is None:
                continue
            origins = [inbox.id]
        parked.append({"message": m.id, "from": origins})

    touched: set[int] = set()
    # Recorded in the panel like any other filing. It is one keypress that moved
    # a conversation out of the inbox, which is exactly what that list is for —
    # leaving it out made "remind me" the one action that happened invisibly.
    # Undoing it takes the promise back too; see app/routers/undo.py.
    op_id = undo.new_op_id()
    _move_messages(db, msgs, park, touched, op_id, "remind")
    _recompute(db, touched)

    reminder = Reminder(
        account_id=msg.account_id, message_pk=msg.id, thread_id=msg.thread_id,
        due_at=due_at, park_mailbox_id=park.id, parked=parked,
    )
    db.add(reminder)
    return reminder, op_id


# --- Bringing it back ------------------------------------------------------


def _return_folder(db: DBSession, account_id: int, mailbox_ids: list[int]) -> Mailbox | None:
    """Which of the folders a message was parked out of it goes back to.

    A message can be filed in several at once — the inbox and a label, on a
    server where those are the same thing — and one move puts it in exactly one
    of them, so this picks. The inbox wins: a reminder is a request to be shown
    something, and the inbox is where being shown things happens.

    \\All never wins, even when it is all that was recorded. It is not a folder
    anything is *in* — on Proton and Gmail it is the union of the whole account —
    so a conversation put back there would be filed exactly where it already was
    and would come back to nobody. That falls through to the inbox, as does a
    folder that has been deleted while the mail waited.
    """
    boxes = [db.get(Mailbox, mid) for mid in mailbox_ids or []]
    boxes = [mb for mb in boxes if mb is not None and mb.account_id == account_id]
    boxes.sort(key=lambda mb: (mb.role != "inbox", mb.role == "all", mb.id))
    best = boxes[0] if boxes else None
    if best is None or best.role == "all":
        return _role_mailbox(db, account_id, "inbox") or best
    return best


def fire(db: DBSession, reminder: Reminder) -> int:
    """Put the conversation back where it came from, unread. Returns how many moved.

    Unread is the whole point of the return trip: a message that comes back read
    comes back invisible, sorted in among mail from a week ago with nothing to
    say it has just arrived. Marked before the move rather than after, because
    the placement being marked is the one in Archive — its UID is the only one
    that exists to name the message by, and the queue is drained in the order it
    was written (agent/actions.py::drain_actions), so the flag is cleared on the
    server and the move then carries it.

    A message that is no longer where it was parked is left alone: it was filed
    somewhere by hand while it waited, and that is a more recent instruction
    than this one.
    """
    park = db.get(Mailbox, reminder.park_mailbox_id) if reminder.park_mailbox_id else None
    touched: set[int] = set()
    restored = 0
    for entry in reminder.parked or []:
        msg = db.get(Message, entry.get("message"))
        if msg is None:
            continue                       # deleted while it waited
        loc = None if park is None else next(
            (item for item in msg.locations if item.mailbox_id == park.id), None)
        if loc is None:
            continue                       # moved by hand since it was parked
        target = _return_folder(db, reminder.account_id, entry.get("from") or [])
        if target is None or target.id == park.id:
            continue
        set_seen(db, msg, False, touched)
        _move_to(db, msg, park.id, target, touched)
        restored += 1

    _recompute(db, touched)
    reminder.state = "done"
    reminder.fired_at = utcnow()
    reminder.error = None
    return restored


def cancel(db: DBSession, reminder: Reminder) -> None:
    """Forget the reminder and leave the mail filed where it is.

    The other half of the pair: this is "never mind, it can stay in Archive",
    while ``fire`` reached by hand is "bring it back now". Keeping them apart
    matters because the mail is not where it was when the reminder was set, and
    a single Cancel that guessed which of the two was meant would be wrong half
    the time.
    """
    reminder.state = "cancelled"
    reminder.error = None


# --- The tick --------------------------------------------------------------


def _reason(exc: BaseException) -> str:
    return str(getattr(exc, "detail", None) or exc) or exc.__class__.__name__


def _note_failure(db: DBSession, reminder_id: int, exc: BaseException) -> None:
    """Record why a reminder did not fire, and leave it pending.

    Nothing retires a reminder except firing it or the user taking it back.
    Everything that can stop one — a move still in flight, a folder the server
    has not listed yet, a database that went away mid-tick — is a thing that can
    stop being true, and the next tick is a minute away. So the row keeps its
    place in the queue and carries the reason, which is what the UI shows beside
    it instead of silence.
    """
    db.rollback()
    reminder = db.get(Reminder, reminder_id)
    if reminder is None:
        return
    reminder.error = _reason(exc)[:500]
    db.commit()


def run_due(db: DBSession, now: datetime | None = None) -> int:
    """Fire every reminder that has come due. Returns how many mails came back.

    One transaction per reminder: a conversation whose folder has gone is not a
    reason for the nine behind it to stay parked, and a failure leaves its reason
    on its own row rather than rolling the whole tick back.
    """
    woken = 0
    accounts: set[int] = set()
    for row in due(db, now):
        reminder_id, account_id = row.id, row.account_id
        try:
            # Claimed inside its own transaction, and re-checked while claimed.
            # The list above was read without a lock and one commit ago, so by
            # now another server process on the same database (or the user
            # pressing "bring it back" as the tick ran) may have fired this very
            # row — and firing a reminder twice moves the conversation out of the
            # inbox it has just been put into. SKIP LOCKED rather than waiting:
            # whoever holds it is already doing this work.
            reminder = db.execute(
                select(Reminder)
                .where(Reminder.id == reminder_id, Reminder.state == "pending")
                .with_for_update(skip_locked=True)
            ).scalars().first()
            if reminder is None:
                db.rollback()
                continue
            restored = fire(db, reminder)
            db.commit()
        except Exception as exc:  # noqa: BLE001
            _note_failure(db, reminder_id, exc)
            continue
        woken += restored
        accounts.add(account_id)

    # Told once at the end rather than per reminder: each publish is a pg_notify
    # round trip of its own, and the browsers treat all of these as "something
    # changed, reload" anyway (see actions._announce).
    for account_id in accounts:
        account = db.get(Account, account_id)
        if account:
            publish_command({"type": "refresh", "email": account.email})
    if accounts:
        events.publish({"type": "present", "moved": woken})
    return woken
