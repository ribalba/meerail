"""Filing mail: where a message goes, and what has to be told about it.

The engine behind trash, archive, move and the bulk versions of all three, plus
the folder-role resolution that decides where those land. Every one of them is
the same two-part operation — change the local view now, queue an instruction
for the agent to make the mail server agree later — and the hard parts are all
in the gap between those two halves: a placement the server has not seen yet, a
move that is queued and can still be re-aimed, a label server where one message
wears three folders and only one of them is worth a command.

Split out of ``app/routers/actions.py`` because it was never HTTP. That module
held nine route handlers and twenty-odd helpers, and three other modules reached
past the handlers for the helpers: ``app/reminders.py`` imported six of them by
private name to park a conversation, ``app/routers/undo.py`` imported ``move_to``
to reverse one, and ``app/routers/sync.py`` imported ``archive_mailbox`` to
offer the fix for an account that has nowhere to archive to. The cycles that
made — reminders importing a router that imports reminders — were worked around
with three function-level imports, each carrying a comment explaining that it
was there to break a ring.

So this is the same direction as the split ``app/sessions.py`` has from
``app/deps.py`` and ``app/transport.py`` from ``app/main.py``: the decision in
one place, the request-and-response adapter in another. Nothing here imports a
router and nothing here reads a Request, so the ring is gone and the three
function-level imports that used to break it are gone with it.

It is *not* the full version of that split, and the difference is worth being
plain about. Those two modules import no FastAPI at all, which is what lets the
test venv drive them with no server installed. This one raises
``HTTPException``, because these functions refuse for reasons a person has to be
told about — "this account has no Trash folder", "this message is still being
moved" — and the sentence is the useful half of the refusal. That one import is
what keeps this module out of the FastAPI-free suite, exactly as it keeps
``app/reminders.py`` out of it; both are covered over HTTP instead. Trading the
sentences for a local exception type the routers translate would buy that back,
and it is a bigger change than moving the code was.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from core import undo
from core.events import publish_command
from core.ingest import derive_role
from core.mail.store import is_pending, move_in_flight, place_pending, recompute_counts
from core.models import Account, Mailbox, Message, MessageLocation, PendingAction, utcnow

from . import events


# --- naming things to the agent ----------------------------------------------


def enqueue(db: DBSession, account_id: int, message_pk: int, type_: str, payload: dict) -> None:
    db.add(PendingAction(account_id=account_id, message_pk=message_pk, type=type_, payload=payload))


def uid_ref(mailbox: Mailbox, loc: MessageLocation, key: str = "folder") -> dict:
    """How an action names one message on the server: folder, UID — and the UID
    epoch the number was read in.

    A UID is only unique within a folder's UIDVALIDITY. Servers start a new one
    whenever their side of the mapping is rebuilt (a Bridge re-login, a restored
    mailbox), and every UID then goes back into the pool to be handed out again
    from the start. An action carrying only a folder and a number therefore says
    "message 4051 in INBOX" — which after a reset is a different message, quite
    possibly one that arrived this morning, and for a delete that is unrecoverable.

    So the epoch travels with the number, and the agent checks the two against
    the folder it has just opened before it touches anything
    (agent/actions.py::_select_verified). ``key`` is the payload's name for the
    folder, which a move spells ``from_folder``.
    """
    return {key: mailbox.imap_name, "uid": loc.imap_uid, "uidvalidity": mailbox.uidvalidity}


def snapshots(db: DBSession, msg: Message, mailbox_ids: list[int]) -> list[dict]:
    """Where this message is now, for the undo of the move about to remove it.

    Taken before the first ``move_to`` rather than inside it, because on a label
    server the three placements being cleared produce one queued action, and the
    undo record has to describe all three or undoing a Proton trash puts the
    message back under one of its labels and quietly loses the rest. See
    core/undo.py.
    """
    return [undo.snapshot(db, loc) for loc in msg.locations if loc.mailbox_id in mailbox_ids]


# --- telling the agent and the browsers --------------------------------------


def wake_agent(db: DBSession, account_id: int | None) -> None:
    """Nudge the agent to drain the queue for this account now.

    A move only lands in the target folder once the agent has run it against
    IMAP and re-ingested the copy. Left to its own schedule that is a poll
    interval away, so the message would vanish from the source folder and not
    show up in the target for up to half a minute — long enough to look like
    the archive was lost. Flag changes skip this: they are applied locally the
    moment you press the key, so the IMAP round trip can wait for the next pass.

    Takes an id rather than a row because the eleven places that used to write
    this out by hand had it in every shape — a Message, an Account, an id, and
    three different spellings of what to do when the lookup came back empty. An
    account that has gone is not an error here: there is no agent to wake for
    it, and the caller has already done the part that mattered.
    """
    if account_id is None:
        return
    account = db.get(Account, account_id)
    if account is not None:
        publish_command({"type": "refresh", "email": account.email})


def announce(db: DBSession, account_ids: set[int], moved: int) -> None:
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
        wake_agent(db, account_id)
    events.publish({"type": "present", "moved": moved})


def recompute(db: DBSession, mailbox_ids: set[int]) -> None:
    for mid in mailbox_ids:
        mb = db.get(Mailbox, mid)
        if mb:
            recompute_counts(db, mb)


# --- where mail is filed ------------------------------------------------------


def mailbox_by_name(db: DBSession, account_id: int, imap_name: str | None) -> Mailbox | None:
    """One account's folder, by the name the mail server calls it.

    The lookup the queue payloads are written in terms of: an action names a
    folder by its IMAP path, because that is the only name the agent can use,
    and everything that reads an action back has to turn that string into a row.
    Written out by hand in eight places across three modules before this, which
    is seven chances for one of them to forget that a folder name is only unique
    within an account.
    """
    if not imap_name:
        return None
    return db.execute(
        select(Mailbox).where(Mailbox.account_id == account_id,
                              Mailbox.imap_name == imap_name)
    ).scalars().first()


def role_mailbox(db: DBSession, account_id: int, role: str) -> Mailbox | None:
    return db.execute(
        select(Mailbox).where(Mailbox.account_id == account_id, Mailbox.role == role)
    ).scalars().first()


def trash_ids(db: DBSession) -> set[int]:
    """Every account's Trash folder. Bulk deletes are defined against this set
    rather than against one account's, because a selector like `flagged` spans
    accounts, and "already in Trash" has to mean the same thing in all of them."""
    return set(db.execute(select(Mailbox.id).where(Mailbox.role == "trash")).scalars().all())


def trash_mailbox(db: DBSession, account_id: int) -> Mailbox:
    """Where "trash" files mail. There is no second place to look.

    Unlike archive_mailbox, a missing \\Trash has no equivalent that still
    means "put this somewhere I can get it back from": the only other thing the
    keypress could be turned into is \\Deleted + EXPUNGE, and that is not
    trashing a message, it is destroying it. Doing that silently — which is what
    a ``None`` target here used to mean — turns one wrong SPECIAL-USE flag on
    the server into every trashed message being gone for good, with the UI
    showing exactly what it shows for a normal trash.

    So it fails instead, and says why. The account is one folder away from
    working; the mail it would otherwise have eaten is not recoverable at all.
    """
    target = role_mailbox(db, account_id, "trash")
    if target is None:
        raise HTTPException(
            status_code=400,
            detail="This account has no Trash folder, so there is nowhere to trash to. "
                   "Create one on the server (or mark an existing folder \\Trash) and "
                   "sync again.")
    return target


def _named_archive(db: DBSession, account_id: int) -> Mailbox | None:
    """A folder that is plainly an archive but is not filed as one.

    ``role`` is derived once, from the SPECIAL-USE flag the server offered when
    the folder was first listed, and it is a stored value: a row written by an
    older agent, or during a pass where Bridge answered the LIST without its
    flags, keeps whatever it got. The account then has a folder called Archive
    and no ``archive`` role, the fallback below fires, and every archive is
    queued against \\All — which on Proton is a move the server will not do.

    Cheap enough to re-derive here (a handful of rows, one name comparison
    each), and it uses the agent's own definition rather than a second one, so
    the two cannot disagree. Only ``custom`` rows are considered: a folder the
    server has told us is something else is not up for reinterpretation.
    """
    rows = db.execute(
        select(Mailbox).where(Mailbox.account_id == account_id, Mailbox.role == "custom")
        .order_by(Mailbox.sort_order, Mailbox.imap_name)
    ).scalars().all()
    return next((m for m in rows if derive_role(m.imap_name) == "archive"), None)


def archive_mailbox(db: DBSession, account_id: int) -> Mailbox | None:
    """Where "archive" files mail, which is not always an \\Archive folder.

    Gmail-style servers publish no \\Archive at all: archiving there means
    dropping the INBOX label while the message stays in \\All ("All Mail"),
    which as an IMAP MOVE is exactly INBOX -> All Mail. Without that fallback
    every Gmail account fails the archive action outright.

    It is a fallback and not a synonym, though, and the difference is the whole
    of this function. \\All is a real destination on Gmail and not one anywhere
    else: Proton publishes the same folder and rejects every attempt to file into
    it. So it is reached only when the account genuinely has nowhere else to
    archive to — after the role, and after the folder that is called Archive
    without being filed as one — and it is dropped entirely once an agent has
    been told no by the server (Mailbox.writes_refused_at). Offering it past
    that point queues a move that cannot run, and a move that cannot run is a
    message the app shows as archived and the server does not.
    """
    fallback = role_mailbox(db, account_id, "all")
    return (role_mailbox(db, account_id, "archive")
            or _named_archive(db, account_id)
            or (fallback if fallback is not None
                and fallback.writes_refused_at is None else None))


def require_archive_mailbox(db: DBSession, account_id: int) -> Mailbox:
    """Where "archive" files mail, or a 400 that says what the account is missing.

    Same shape as trash_mailbox and for the same reason: the alternative to a
    destination is not a quieter archive, it is a keypress that does nothing —
    and, before this, one that queued a move the agent then retried against a
    folder the server had already refused, every fifteen minutes, in silence.
    """
    target = archive_mailbox(db, account_id)
    if target is None:
        raise HTTPException(
            status_code=400,
            detail="This account has no Archive folder to file mail into. Create one "
                   "on the server (or mark an existing folder \\Archive) and sync "
                   "again — All Mail is not a folder this server accepts mail into.")
    return target


def already_there(target: Mailbox) -> str:
    """What to say when a message is asked to move to the folder it is in."""
    where = target.display_name or target.imap_name
    if target.role == "trash":
        return (f"This is already in {where}. Emptying the Trash is what deletes it "
                f"from the server for good.")
    return f"This is already in {where}."


def labels_one_message(db: DBSession, account_id: int) -> bool:
    """Does this account's server file one message under several labels?

    An \\All mailbox is what says so: Proton and Gmail both publish one, and on
    both of them the copies in INBOX, in \\All and under a user label are the
    same message seen three times, not three messages.
    """
    return db.scalar(
        select(Mailbox.id).where(Mailbox.account_id == account_id,
                                 Mailbox.role == "all").limit(1)
    ) is not None


# --- flags --------------------------------------------------------------------


def set_seen(db: DBSession, msg: Message, seen: bool,
             touched: set[int] | None = None) -> set[int]:
    """Mark every placement of a message read (or unread), telling the server too.

    Split out of the endpoint that calls it because a reminder coming back marks
    its mail unread as it lands (see app/reminders.py), and doing that by hand
    there would be a second copy of the rule about pending placements — the one
    thing in here that is easy to get wrong and invisible when you do.

    Returns the mailboxes whose counts this changed, so a caller doing several
    of these can recount once at the end.
    """
    touched = set() if touched is None else touched
    # Only a real unread -> read transition is a read at a knowable time. A
    # message that was already seen everywhere is one the user read at some
    # unrecorded point — often years ago, during backfill — and stamping "now"
    # for it would fill the read heatmap with the times the mailbox was opened
    # rather than the times mail was read. See models.Message.read_at.
    became_seen = seen and msg.read_at is None and any(not l.seen for l in msg.locations)
    for loc in msg.locations:
        if loc.seen == seen:
            continue
        loc.seen = seen
        touched.add(loc.mailbox_id)
        if is_pending(loc):
            # Nothing to tell the server: this placement is a move that has not
            # been applied yet, and there is no UID here that IMAP knows. The
            # flag rides along anyway — upsert_location inherits local state
            # onto the real placement when it lands, and queues the catch-up.
            continue
        mb = db.get(Mailbox, loc.mailbox_id)
        enqueue(db, msg.account_id, msg.id, "setflags", {
            **uid_ref(mb, loc),
            "add": ["\\Seen"] if seen else [], "remove": [] if seen else ["\\Seen"]})
    if became_seen:
        msg.read_at = utcnow()
    return touched


def set_flagged(db: DBSession, msg: Message, flagged: bool,
                touched: set[int] | None = None) -> set[int]:
    """The same for \\Flagged, and deliberately the same shape.

    The no-op check is the part that has to match. A placement that already
    carries the flag needs no IMAP round trip, and counting it as touched puts
    the folder through recompute_counts — two aggregates over every placement in
    it — to arrive back where it started. On a label server one keypress reaches
    three placements, so re-flagging an already-flagged message was three
    commands and three folder recounts for nothing.
    """
    touched = set() if touched is None else touched
    for loc in msg.locations:
        if loc.flagged == flagged:
            continue
        loc.flagged = flagged
        touched.add(loc.mailbox_id)
        if is_pending(loc):
            continue                      # see set_seen, above
        mb = db.get(Mailbox, loc.mailbox_id)
        enqueue(db, msg.account_id, msg.id, "setflags", {
            **uid_ref(mb, loc),
            "add": ["\\Flagged"] if flagged else [],
            "remove": [] if flagged else ["\\Flagged"]})
    return touched


# --- moving -------------------------------------------------------------------


def _retarget_pending(db: DBSession, msg: Message, target: Mailbox | None,
                      loc: MessageLocation, op_id: str | None = None,
                      op_kind: str = "move") -> str | None:
    """Re-aim the move that is already queued for this message.

    Returns "moved" when the queued action now points at ``target``, "undone"
    when the two moves cancelled each other out and there is nothing left to
    queue or to place (the caller is finished — see move_to), and None when
    there was no pending action to re-aim.

    Reached when the placement being moved is one we wrote ourselves — the
    message was archived a moment ago and the agent has not applied that move
    yet, so as far as the server is concerned the message is still in the folder
    it started in. Queueing a second move *from* the folder it is optimistically
    sitting in would address it by a UID no server has ever heard of.

    So the queued move is edited instead: same message, new destination. Filing
    something twice before the connection comes back is one move, to wherever it
    ended up.

    Taken under a lock, because the agent may be reaching for the same row. Read
    without one, this rewrote a destination onto a row an agent had already
    claimed and was at that moment applying: the old target went to the server,
    the new one was written over the top of the result, and neither side ever
    revisited it — the message sat in Trash on the server and in Archive here,
    for good. ``FOR UPDATE`` makes the two take turns. Whoever gets there first
    wins cleanly: an agent mid-apply leaves this with no pending row to re-aim,
    so the caller answers "still being moved, try again in a moment", and an edit
    that lands first makes the agent's own claim skip the row until the next pass
    (agent/actions.py::_lease), which then applies the destination the user
    actually asked for.
    """
    action = db.execute(
        select(PendingAction).where(
            PendingAction.message_pk == msg.id,
            PendingAction.type.in_(("move", "delete")),
            PendingAction.status == "pending",
        ).order_by(PendingAction.created_at.desc()).with_for_update()
    ).scalars().first()
    if action is None:
        return None
    payload = dict(action.payload or {})
    from_folder = payload.get("from_folder") or payload.get("folder")
    # The UID and the epoch it was read in are the pair that identifies the
    # message (see uid_ref); re-aiming the action changes where it goes, never
    # what it addresses, so both are carried over verbatim.
    ref = {"uid": payload.get("uid"), "uidvalidity": payload.get("uidvalidity")}
    if target is not None and target.imap_name == from_folder:
        return _undo_pending(db, msg, action, target, ref, loc)
    # The undo record travels with the action rather than being rewritten from
    # where the message is *now*: "now" is the optimistic placement the first
    # move wrote, and putting the message back there would restore a folder the
    # server has never had it in. Two keypresses before the agent has been round
    # are one move, and it is one move's worth of undo — back to where the mail
    # actually was. The operation id is the new one, so the panel shows the
    # keypress just made rather than the one it absorbed.
    keep = {"undo_from": payload.get("undo_from") or []}
    if target is not None:
        action.type = "move"
        action.payload = {"from_folder": from_folder, **ref, "to_folder": target.imap_name,
                          **undo.fields(op_id, op_kind, keep["undo_from"])}
    else:
        action.type = "delete"
        action.payload = {"folder": from_folder, **ref,
                          **undo.fields(op_id, "delete", None)}
    return "moved"


def _undo_pending(db: DBSession, msg: Message, action: PendingAction, target: Mailbox,
                  ref: dict, loc: MessageLocation) -> str:
    """The queued move is being asked to put the message back where it started.

    Re-aiming it the ordinary way would write "move INBOX 4051 to INBOX" — a
    command that is at best pointless (servers that take it re-file the message
    under a fresh UID) and at worst a permanent failure the agent then retries
    forever. The two moves cancel out, so the honest thing is to drop the action
    and put the placement back, which is a state the database can describe
    exactly: the move never happened, and nothing needs to be told about it.

    Reached by every route that files a message and then unfiles it before the
    agent has been round — archiving a mail on a laptop that is offline and
    dragging it back out of Archive, and a reminder falling due while the move
    that parked it is still queued (app/reminders.py).

    The UID is put back only when the folder is still in the epoch it was read
    in; UIDVALIDITY moving on means that number now names some other message, or
    nothing. The optimistic placement written in its stead is what every other
    move leaves behind, and the reconcile sweep replaces it with the server's own
    once the action is gone (agent/sync.py::_restore_unplaced) — which it can,
    precisely because this deleted the action that would otherwise have read as
    a move still in flight.
    """
    db.delete(action)
    uid = ref.get("uid")
    epoch = ref.get("uidvalidity")
    if uid is None or epoch is None or target.uidvalidity != epoch:
        return "moved"          # let the caller place it optimistically instead
    back = MessageLocation(mailbox_id=target.id, imap_uid=uid)
    back.seen, back.flagged = loc.seen, loc.flagged
    back.answered, back.draft, back.keywords = loc.answered, loc.draft, loc.keywords
    msg.locations.append(back)
    return "undone"


def _in_flight(db: DBSession, msg: Message) -> bool:
    """Is the move behind this optimistic placement still going to land?

    Only asked when the placement is one we wrote ourselves and there is no
    queued action left to re-aim (see _retarget_pending). The move finished
    seconds ago and the sync has not brought the real placement back yet: the
    honest answer to another keypress is "not yet". The move finished long ago
    and the server copy has still not arrived: never will be — the message is
    not where we think it is, or (as when a trash-as-COPY-plus-EXPUNGE deleted
    it outright) it is nowhere at all. Waiting on that forever is what wedged 22
    messages into a folder no keypress could get them out of.

    The agent asks the same question of the same actions for the opposite
    reason — see core.mail.store.move_in_flight, which is why this is one
    definition rather than two that can drift apart.
    """
    return move_in_flight(db, msg.id)


def move_to(db: DBSession, msg: Message, source_mailbox_id: int, target: Mailbox | None,
            touched: set[int] | None = None, enqueue_action: bool = True,
            op_id: str | None = None, op_kind: str = "move",
            undo_from: list[dict] | None = None) -> bool:
    """Move exactly one folder placement, preserving the message's other labels.

    Returns whether anything moved. False means the placement was already
    accounted for by where the message sits — see the \\All guard below — and
    the caller should not count it or queue anything on its behalf.

    The target placement is written straight away rather than waited for. The
    agent applies the move to IMAP and the next pass ingests the server's copy,
    which can be a poll interval away with a connection and days away without
    one; until then the message would be in no folder at all — gone from the
    list it was archived out of and not yet in the one it was archived into.
    See core.mail.store.place_pending.

    Pass `touched` to collect the affected mailboxes instead of recounting them
    here: bulk callers move thousands of placements out of the same few folders,
    and recomputing per placement would redo that scan once per message.

    `enqueue_action=False` files the placement locally and tells the server
    nothing. For a label server that is not a shortcut but the correct behaviour
    — one message wearing three labels is one message, and it only wants moving
    once. See move_messages.

    `op_id` and `undo_from` are what make the move undoable: the id groups every
    queue row one keypress produced, and the snapshots say where the message was
    before it. Both ride on the action's payload, so a caller that passes neither
    (the sync's own flag catch-up, say) simply does not appear in the panel. See
    core/undo.py.
    """
    loc = next((item for item in msg.locations if item.mailbox_id == source_mailbox_id), None)
    if loc is None:
        raise HTTPException(status_code=400, detail="Message is not in the source mailbox")
    source = db.get(Mailbox, loc.mailbox_id)
    if target is not None and source.id == target.id:
        # Nothing to do — and saying nothing about it is what made Delete look
        # broken in Trash. The reader offers Delete on every message, the folder
        # a message is in is not something the button knows, and a request that
        # answered "ok" while changing nothing left the row to reappear on the
        # next list refresh with no explanation at all.
        #
        # Reporting it is the whole fix; it deliberately does not turn into a
        # permanent delete. Destroying mail is Empty Trash's job (see
        # bulk_empty_trash), asked for by name and confirmed.
        raise HTTPException(
            status_code=409,
            detail=already_there(target))

    # \\All is the union of everything the account holds, not a folder a message
    # sits in — so a placement there says nothing about where the mail is filed,
    # and moving *out of* it only means "also file into the target". When the
    # message is already in that target, the command achieves nothing.
    #
    # This is not a tidy-up. On a label server every message is in \\All, so
    # archiving a thread whose older messages were archived long ago planned one
    # of these per message: move_messages drops the target placement from the
    # origins (leaving \\All as the only one) and _carrier then picks it as a
    # last resort. Four rows in five on a real account. Going out the server
    # takes them as no-ops; coming back they are an undo aimed at \\All, which
    # Proton answers "operation not allowed" — the move a keypress could never
    # have needed, failing minutes later under a button that reported success.
    # A placement we wrote ourselves does not count, and the distinction is the
    # whole of this check working. On a label server one keypress moves every
    # placement — trashing clears each label — so by the time the \\All one comes
    # round, the *first* move of the same operation has already written an
    # optimistic placement in the target. Reading that as "already there" would
    # leave the message showing in \\All after it had been trashed.
    if (target is not None and source.role == "all"
            and any(item.mailbox_id == target.id and not is_pending(item)
                    for item in msg.locations)):
        return False

    if enqueue_action:
        if is_pending(loc):
            outcome = _retarget_pending(db, msg, target, loc, op_id, op_kind)
            if outcome == "undone":
                # The queued move and this one cancelled out, and _undo_pending
                # has already put the placement back. Nothing to enqueue and
                # nothing to place — just retire the optimistic row below.
                db.delete(loc)
                if touched is None:
                    recompute(db, {source.id, target.id})
                else:
                    touched.update({source.id, target.id})
                return True
            if outcome is None and _in_flight(db, msg):
                # The agent has just applied the first move and the sync has not
                # yet brought the real placement back — a window of seconds, in
                # which there is no UID to address the message by. Refusing is
                # the honest answer: pressing the key again once the folder
                # settles works.
                #
                # Past that window there is nothing to wait for, and the
                # placement is filed locally instead: no UID means nothing to
                # tell the server, and a message that only exists here is still
                # one the user gets to move around.
                raise HTTPException(
                    status_code=409,
                    detail="This message is still being moved — try again in a moment")
        elif target is not None:
            enqueue(db, msg.account_id, msg.id, "move",
                    {**uid_ref(source, loc, "from_folder"), "to_folder": target.imap_name,
                     **undo.fields(op_id, op_kind,
                                   undo_from if undo_from is not None
                                   else [undo.snapshot(db, loc)])})
        else:
            # No target folder means delete for good, and the only caller that
            # asks for that is bulk_empty_trash — a separate, confirmed
            # operation, precisely so that no ordinary trash or bulk-delete can
            # arrive here by inferring it from where a message happens to sit. A
            # missing \Trash used to reach this too, which made "trash" mean
            # "destroy" on any server whose flags we could not read;
            # trash_mailbox now refuses that case before it gets this far.
            #
            # Recorded in the panel like everything else, and undoable by
            # nothing: the row exists so that "2,000 messages deleted from the
            # server" is something a person can see they did, which is more use
            # than the one destructive operation being the only invisible one.
            enqueue(db, msg.account_id, msg.id, "delete",
                    {**uid_ref(source, loc), **undo.fields(op_id, "delete", None)})

    if target is not None:
        place_pending(db, msg, target.id, loc)
    db.delete(loc)
    if touched is None:
        recompute(db, {source.id} | ({target.id} if target is not None else set()))
    else:
        touched.add(source.id)
        if target is not None:
            touched.add(target.id)
    return True


def move_messages(db: DBSession, msgs: list[Message], target: Mailbox | None,
                  touched: set[int], op_id: str | None = None,
                  op_kind: str = "move") -> int:
    """Move every placement of every message, without committing or recounting.

    Every placement goes, but on a label server only one of them is queued for
    the agent. The rest are the same message under another label, and the first
    move takes them all with it: trashing on Proton clears every other label by
    definition. Queueing them anyway meant a second command addressing a UID the
    move it followed had just retired — "Message does not exist (Code=2501)",
    logged once per trashed message, retried, and eventually settled as done
    having achieved nothing. It also gave the destructive half of the old
    hand-rolled move a second chance to run against a message already sitting in
    Trash, which is how those messages were deleted outright.
    """
    collapse = labels_one_message(db, msgs[0].account_id) if msgs else False
    moved = 0
    for msg in msgs:
        # Snapshotted: move_to deletes out of msg.locations as it goes.
        mailbox_ids = [loc.mailbox_id for loc in msg.locations
                       if target is None or loc.mailbox_id != target.id]
        carrier = _carrier(db, msg, mailbox_ids) if collapse else None
        # Taken here, for the same reason the mailbox ids are: by the time the
        # carrier's move is queued, the placements it has to describe have been
        # deleted out from under it.
        snaps = snapshots(db, msg, mailbox_ids)
        for mailbox_id in mailbox_ids:
            if move_to(db, msg, mailbox_id, target, touched,
                       enqueue_action=not collapse or mailbox_id == carrier,
                       op_id=op_id, op_kind=op_kind, undo_from=snaps):
                moved += 1
    return moved


def _carrier(db: DBSession, msg: Message, mailbox_ids: list[int]) -> int | None:
    """Which of a message's placements the one queued move should be made from.

    Not just any of them. A placement we wrote ourselves carries a UID no server
    has heard of, so it cannot address anything. And \\All is a folder nothing
    can be taken *out* of — a move from there is an added label and nothing
    else, which for an archive leaves the message sitting in the inbox it was
    supposed to leave. Either is fine as the last resort, when it is all the
    message has.
    """
    candidates = [loc for loc in msg.locations if loc.mailbox_id in mailbox_ids]
    if not candidates:
        return None

    def rank(loc: MessageLocation) -> tuple[bool, bool]:
        mb = db.get(Mailbox, loc.mailbox_id)
        return (is_pending(loc), (mb.role if mb else "") == "all")

    return min(candidates, key=rank).mailbox_id
