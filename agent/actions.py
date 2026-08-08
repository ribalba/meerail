"""Apply queued actions to Bridge over IMAP/SMTP (the write-back half of
two-way sync).

The UI enqueues PendingAction rows when you mark read, flag, move or send; the
agent drains them here and reports the outcome back on the same rows.

Nothing in this file ever gives up. A queued action is something the user asked
for, and a message waiting in the outbox is their mail — no number of failed
attempts turns either into ours to throw away. meerail is expected to run for
days with no connection at all (a laptop that opens twice a week, a Bridge that
is signed out, an SMTP server that has been down since Friday), and the only
correct behaviour across all of that is to keep the work queued and keep trying.
What an attempt count buys instead is the retry *cadence*: see _RETRY_BASE.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta

from sqlalchemy import and_, func, or_, select, update

from core import events, outbox
from core.database import SessionLocal
from core.mail import store
from core.mail.parse import content_key
from core.models import Account, Mailbox, Message, Outbound, PendingAction, utcnow

import imap
import log
import smtp

# The retry cadence lives in core/outbox.py, because the server shows the same
# numbers in the Outbox folder — "next attempt in 4 minutes" has to mean the
# same thing on both sides of the database. Re-exported under the old names so
# this module still reads as the owner of the drain loop.
_RETRY_BASE = outbox.RETRY_BASE
_RETRY_CEILING = outbox.RETRY_CEILING
_RETRY_DOUBLINGS = outbox.RETRY_DOUBLINGS
retry_delay = outbox.retry_delay

# How much work one pass takes on. Nothing is ever retired, so the queue can
# hold rows that have been failing for weeks — and, since the send delay landed,
# rows that are deliberately not due for another day. Both are why the *due*
# filter runs in SQL (see _due_clause) rather than over a fixed slice of the
# oldest rows: this limit is applied to what is ready to go, so no depth of
# waiting mail can crowd out an action queued a second ago.
_PER_PASS = 50

# Failure number at which a still-queued send says so in its own right, rather
# than only as one more line in a series. Far enough in that a Bridge restart
# never reaches it.
_NAG_AT = 10

# How long a lease may go untouched before another pass is allowed to take it
# back, and how often the agent holding one says it is still there.
#
# Untouched, not held: a fixed expiry would have to be longer than the slowest
# single action, and there is no such number. A 300 MB attachment over a hotel
# uplink is a send that is *working* for half an hour, and the socket timeout
# does not bound it — every chunk that goes out resets that clock. Any TTL
# picked here would eventually be crossed by a healthy transfer, and a second
# agent would then reclaim a row whose message was at that moment still going
# out, which is the duplicate send the lease exists to prevent.
#
# So the lease is renewed while the work is genuinely in progress (see
# _LeaseKeeper) and the TTL only measures silence. It is then a statement about
# the agent rather than about the action: nothing has touched this row for
# fifteen minutes, so whatever held it is gone.
_LEASE_TTL = timedelta(minutes=15)
_LEASE_RENEW = 60.0     # seconds between renewals; well inside the TTL


class _LeaseKeeper:
    """Say, every minute, that the agent applying this action is still here.

    On its own connection and its own thread, because the point is to keep
    speaking while this pass's thread is blocked inside a single SMTP or IMAP
    command that may run for many minutes. The session it uses is its own for
    the same reason: a SQLAlchemy Session belongs to one thread.

    Deliberately unable to fail loudly. A renewal that does not land leaves the
    lease ageing exactly as it would have without any of this, which the TTL
    already covers; taking the pass down over it would turn a slow upload into a
    failed one.
    """

    def __init__(self, action_id: int):
        self._action_id = action_id
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_LeaseKeeper":
        self._thread = threading.Thread(target=self._run, name="meerail-lease", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> bool:
        self._stop.set()
        if self._thread is not None:
            # Before the caller settles the row: the last word on it has to be
            # the outcome, not a renewal landing a moment later.
            self._thread.join(timeout=5)
        return False

    def _run(self) -> None:
        # wait() returns True the moment __exit__ sets the event, so a short
        # action never touches the database at all.
        while not self._stop.wait(_LEASE_RENEW):
            db = SessionLocal()
            try:
                db.execute(
                    update(PendingAction)
                    .where(PendingAction.id == self._action_id,
                           PendingAction.status == "leased")
                    .values(updated_at=utcnow())
                )
                db.commit()
            except Exception:  # noqa: BLE001 — see the class docstring
                pass
            finally:
                db.close()


class StaleUid(RuntimeError):
    """This folder is not the one the action's UID was recorded against.

    A UID names a message only within one UIDVALIDITY epoch. When a server
    starts a new one — a Bridge re-login, a rebuilt cache, a restored mailbox —
    every UID in the database goes back into the pool, and the number that meant
    "the newsletter I trashed" can now mean "the contract that arrived this
    morning". Applying a queued delete against that is how the wrong mail gets
    destroyed, and there is no undo for it.

    So it is refused rather than retried: the UID cannot become valid again, and
    a delete aimed at a message we can no longer identify is not something to
    keep trying. See _settle_stale.
    """


class Refused(RuntimeError):
    """The server was asked, and said no to something no retry can change.

    The other half of the rule this module is built on. "Never give up" is right
    for everything that could still work later — a Bridge that is signed out, a
    port that is wrong until someone fixes it, a laptop that has been shut since
    Friday — and it is not right for an instruction the server has considered and
    rejected on its own terms. Retrying that is not persistence, it is a loop:
    the same command, to the same server, for the same answer, every fifteen
    minutes, for as long as the database exists.

    Worse than useless, because a queued action is also a claim on the local view
    (core.mail.store.move_in_flight) — so the loop does not merely fail, it goes
    on insisting the app is right about where a message is while the server has
    it somewhere else. Refusing once, out loud, and letting the next sweep read
    the truth back is the honest end. See _settle_refused.
    """


def _is_all_mail(db, account_id: int, imap_name: str) -> bool:
    """Is this folder the account's \\All mailbox? Names differ per server
    ("All Mail", "[Gmail]/All Mail"), so it is the role the sync pass recorded
    from the SPECIAL-USE flag that decides, not the name."""
    return db.scalar(
        select(Mailbox.role).where(Mailbox.account_id == account_id,
                                   Mailbox.imap_name == imap_name)
    ) == "all"


def _write_into(db, action: PendingAction, to_folder: str, fn, *args) -> None:
    """Run a command that files a message into ``to_folder``, and tell a refusal
    that will never stop being a refusal from one worth retrying.

    The case this exists for is \\All as a *destination*. On Gmail the folder is
    a real place a message can be put, and "archive" means exactly this move:
    INBOX -> All Mail, which drops the INBOX label and leaves the mail in the
    union. On Proton Bridge the same command is rejected outright — All Mail is
    the union and nothing else, and there is no operation that adds to it. Both
    servers publish an \\All mailbox and nothing in a LIST tells the two apart,
    so the command is sent and the server's answer is what decides.

    Only the answer, though. A connection that broke mid-command decided nothing
    (see imap.refused), and a move dropped on the strength of a dropped socket is
    a filing the user has to do twice.

    Anything else the server refuses is left alone deliberately. "Over quota" and
    "no such mailbox" are also answers that will not change on their own, but
    they change when someone empties a mailbox or the folder is created — and
    this module's whole disposition is that an action outlives the condition that
    stopped it. \\All is the one destination where there is nothing to wait for:
    the folder is not a folder.
    """
    try:
        fn(*args)
    except Exception as e:
        if imap.refused(e) and _is_all_mail(db, action.account_id, to_folder):
            raise Refused(
                f"{to_folder} is this account's All Mail, and the server will not accept "
                f"mail into it — it is the union of everything the account holds, not a "
                f"folder a message can be filed in ({e}). Archiving on this account needs "
                f"a real Archive folder: create one on the server, or mark an existing "
                f"one \\Archive, and sync again."
            ) from e
        raise


def _copy_then_remove(db, c, action: PendingAction, uid: int, from_folder: str,
                      to_folder: str, message_id: str | None,
                      content_hash: str | None) -> None:
    """Move by hand, for a server that does not advertise MOVE (RFC 6851).

    COPY the message into the target folder, then take it out of the source one
    — and that second half is where a move turns into a deletion if it is done
    blind, so it only happens against positive evidence that there is still
    something in the source folder to take out.

    On a server where folders are labels, the COPY *is* the move: applying
    Trash removes every other label, so by the time the EXPUNGE runs the
    message is not in the source folder any more, it is sitting in Trash — and
    "delete this message, which is in Trash" is how Proton spells "delete it
    for good". That is not hypothetical. It destroyed 22 messages on a live
    account over two days, one per trash keypress, each of which the UI went on
    showing from its own optimistic placement while the server had no copy left
    anywhere. Those servers all advertise MOVE, so they no longer come through
    here at all; the guard stays because this is the path that cannot tell.

    Being wrong in the safe direction leaves a copy in the source folder, which
    the next sync shows and the user can move again. Being wrong in the other
    direction is mail nobody can get back.

    ``message_id`` is what lets a second attempt tell whether the first one got
    anywhere. Nothing here is transactional: the COPY can land and its response
    be lost — a dropped connection, a Bridge restart, the stall watchdog closing
    the socket — and the action then stays queued and is tried again, because a
    move that has not *visibly* happened is one this module retries forever.
    Copying again is how the same message ends up in Trash twice. So the target
    folder is asked first, and a copy that is provably already there is not made
    twice; the move continues from where it got to, which is the removal.

    "Provably" carries the whole weight of that sentence, because the removal
    follows either way. A message in the target that merely *shares* a
    Message-ID is not this message — ids are written by senders and two mails can
    wear one (see core/mail/store.py) — and treating it as proof skips the copy
    and then expunges the source, leaving the mail nowhere on the server at all.
    See _copy_landed for what counts.

    Which folder is *selected* is the other half of that, and the half that is
    easy to lose. Every command here — COPY, SEARCH, STORE, EXPUNGE — acts on
    the selected mailbox and names its messages by that mailbox's UIDs, and
    asking the target whether the copy is there means selecting the target. Left
    that way, the COPY that follows says "copy uid 4051" to the folder we are
    moving *into*: it copies whatever that folder's 4051 is, or nothing at all,
    and the source is expunged either way on the strength of a copy that was
    never made. So the source is selected again — and its UID epoch checked
    again, since a SELECT is the only place that answer comes from — immediately
    before the copy, and nothing between there and the removal changes it.
    """
    landed = _copy_landed(c, uid, to_folder, message_id, content_hash)
    # Back to the source, whatever the check above left selected, and proved to
    # still be the folder these UIDs were written for. COPY does not change the
    # selection, so this holds through the removal below as well.
    _select_verified(db, c, action, from_folder)
    if not landed:
        _write_into(db, action, to_folder, c.copy, [uid], to_folder)
    if not _still_present(c, uid):
        return
    _remove_message(c, uid)


# How many same-id candidates in the target folder are worth reading before
# giving up and copying anyway. A retry has one copy to find; a folder holding
# more messages than this under one Message-ID is not a case to spend a mailbox
# download on, and copying again is the safe answer.
_CANDIDATE_LIMIT = 5


def _copy_landed(c, uid: int, to_folder: str, message_id: str | None,
                 content_hash: str | None) -> bool:
    """Is this exact message in the target folder already — because a previous
    attempt copied it there?

    Message-ID finds the candidates: it is the only handle that survives a COPY,
    since the UID the copy was given belongs to the target folder and was never
    reported back to us (UIDPLUS would say in a COPYUID, but only to the attempt
    that made it — and that is precisely the attempt whose answer went missing).

    The message itself then decides. A candidate is read back and hashed the way
    every message here is hashed (core.mail.parse), and only a hash equal to this
    message's own is proof. Nothing weaker will do, because the answer licenses
    an EXPUNGE of the source: agreeing on a Message-ID, or on a Message-ID and a
    size and an internal date, is agreement between *headers*, and headers are
    written by whoever sent the mail. A message engineered to match, or two that
    match by accident, would mean the copy is skipped and the original expunged —
    the mail then nowhere on the server at all.

    Reading a candidate body costs a fetch, on the retry of a move on a server
    with no MOVE command. That is a rare path twice over, and the alternative is
    a guess with mail on the other side of it.

    Unknowable is a no, every time. No Message-ID, no stored hash to compare
    against, a server that will not answer the search, an error anywhere along
    the way: none of them are evidence that the copy landed, and the two ways of
    being wrong are not equal — a move that copies twice leaves a duplicate the
    user can delete, a move that copies never deletes their mail.
    """
    if not message_id or not content_hash:
        return False
    try:
        c.select_folder(to_folder, readonly=True)
        candidates = list(c.search(["HEADER", "MESSAGE-ID", message_id]))[:_CANDIDATE_LIMIT]
        if not candidates:
            return False
        fetched = c.fetch(candidates, [b"BODY.PEEK[]"]) or {}
        for data in fetched.values():
            raw = data.get(b"BODY[]") or data.get(b"RFC822")
            if raw and content_key(raw) == content_hash:
                return True
        return False
    except Exception:  # noqa: BLE001 — see the docstring
        return False


def _remove_message(c, uid: int) -> None:
    """Take one message out of the selected folder, and only that one.

    A bare EXPUNGE removes every message in the folder carrying \\Deleted, not
    just this one — including messages another client flagged and has not
    expunged yet. That is somebody's mail destroyed by a keypress that named a
    different message, and there is no undo for it. UID EXPUNGE (RFC 4315,
    advertised as UIDPLUS) is the only form that can be aimed, so where the
    server does not offer it this refuses rather than guesses.

    Refusing leaves the action queued and retried, which is what this module
    does with everything it cannot do yet (see _settle): a delete that has not
    happened can still happen, expunged mail cannot come back. The capability is
    checked *before* the \\Deleted flag goes on, so a refusal also leaves no
    message sitting flagged for the next client's EXPUNGE to sweep up.

    Bridge, Gmail, Proton, Dovecot and Cyrus all advertise UIDPLUS; the server
    this closes the door on is a rare one.
    """
    if not c.has_capability("UIDPLUS"):
        raise RuntimeError(
            "server does not advertise UIDPLUS, so this message cannot be expunged "
            "on its own — a folder-wide EXPUNGE would take every other \\Deleted "
            "message in the folder with it")
    c.delete_messages([uid])
    c.expunge([uid])


def _uidvalidity(info) -> int | None:
    """The UID epoch out of a SELECT response, whatever shape it arrives in."""
    if not info:
        return None
    raw = info.get(b"UIDVALIDITY", info.get("UIDVALIDITY")) if hasattr(info, "get") else None
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _select_verified(db, c, action: PendingAction, folder: str) -> None:
    """Open the folder this action names, and refuse unless it is still the
    folder the action was written against.

    The check is here, one command before the flag change or the deletion, and
    not on the row when it was queued: the epoch the server is serving *now* is
    the only one that matters, and between the queueing and this line there is a
    poll interval, a laptop lid, or a week offline. Doing it anywhere earlier
    would be checking a number against itself.

    The epoch the action was written against comes off the payload, and from
    nowhere else. The folder's own recorded UIDVALIDITY looks like a reasonable
    stand-in for a row queued by a version that did not write one, and is not:
    that column is *mutable*, and the thing that changes it is a sync pass
    noticing the epoch has moved. So a legacy row that survives one such pass —
    it was in its retry backoff, or sat past this pass's limit, or the pass ended
    between the folder walk and the drain — is then compared against the new
    epoch and matches it, and the UID it carries from the old one is applied to
    whatever message inherited that number. The fallback would have been most
    likely to fail in exactly the case it existed for.

    So there is no fallback. A row with no epoch of its own cannot be checked and
    is refused, which on an upgrade costs the handful of actions that happened to
    be queued at the time: a flag re-derived on the next reconcile, a move that
    can be made again, a delete the user can ask for again. Every one of those is
    recoverable and the alternative is not.

    Refusing is on the safe side of all of it: a flag not set can be set again, a
    move not made can be made again, and a message not deleted is still there to
    delete. The opposite mistake is somebody's mail.
    """
    live = _uidvalidity(c.select_folder(folder))
    known = (action.payload or {}).get("uidvalidity")
    try:
        known = int(known) if known is not None else None
    except (TypeError, ValueError):
        known = None
    uid = (action.payload or {}).get("uid")
    if known is None:
        raise StaleUid(
            f"uid {uid} in {folder} was queued without the UIDVALIDITY it was read under "
            f"— by a version of meerail that did not record it — so there is no way to "
            f"tell whether it still names the same message, and a guess here is "
            f"somebody's mail")
    if live is None:
        raise StaleUid(
            f"{folder} did not report a UIDVALIDITY, so uid {uid} cannot be checked "
            f"against the {known} it was recorded under")
    if live != known:
        raise StaleUid(
            f"{folder} is on UIDVALIDITY {live}, not the {known} uid {uid} was recorded "
            f"under — the folder has been rebuilt since, so that number no longer names "
            f"the message this was queued for")


def _identity(db, action: PendingAction) -> tuple[str | None, str | None]:
    """(Message-ID, content hash) of the mail this action is about.

    The id is the only name for a message that survives being copied into
    another folder, and so the only way to *find* it there; the hash is what
    says a candidate found that way is this message and not one that shares its
    id. Either can be absent — mail whose sender wrote no Message-ID, a row whose
    body was never stored — which is why every caller has to have an answer for
    "unknown"."""
    if action.message_pk is None:
        return None, None
    row = db.execute(
        select(Message.message_id, Message.content_hash).where(Message.id == action.message_pk)
    ).first()
    return (row[0], row[1]) if row else (None, None)


def _still_present(c, uid: int) -> bool:
    """Is this UID still in the selected folder? Anything unclear is a no."""
    try:
        return bool(c.search(["UID", uid]))
    except Exception:  # noqa: BLE001
        return False


def apply_action(db, bridge, account, action: PendingAction) -> None:
    t = action.type
    p = action.payload or {}
    # Not bridge.client: every command here has to sit under the same stall
    # watchdog as the fetch path, or a wedged write-back stops the account
    # syncing with nothing logged. See Bridge.ops.
    c = bridge.ops()

    if t == "setflags":
        _select_verified(db, c, action, p["folder"])          # readwrite
        if p.get("add"):
            c.add_flags([p["uid"]], p["add"])
        if p.get("remove"):
            c.remove_flags([p["uid"]], p["remove"])

    elif t == "move":
        _select_verified(db, c, action, p["from_folder"])
        if _is_all_mail(db, action.account_id, p["from_folder"]):
            # \All is not a folder a message can be taken out of. On Proton and
            # Gmail it is the union of everything the account holds — filing to
            # Archive or Trash is a label change that the COPY has already
            # made, and removing it from \All afterwards is a step with nothing
            # to do. The server says so ("EXPUNGE failed: operation not
            # allowed") and the whole action fails on it, retries, and fails
            # again: 267 archived and trashed messages piled up in the queue
            # that way, every one of them already filed on the server.
            _write_into(db, action, p["to_folder"], c.copy, [p["uid"]], p["to_folder"])
        elif c.has_capability("MOVE"):
            # One command, and the server decides what leaving the source
            # folder means — which is the whole reason to prefer it. See
            # _copy_then_remove for what doing it by hand costs on a server
            # where folders are labels, and for what it costs on a retry.
            _write_into(db, action, p["to_folder"], c.move, [p["uid"]], p["to_folder"])
        else:
            _copy_then_remove(db, c, action, p["uid"], p["from_folder"], p["to_folder"],
                              *_identity(db, action))

    elif t == "delete":
        # Deleting for good, which the UI only ever queues for a message the
        # user emptied out of Trash. Scoped to this UID alone — see
        # _remove_message for what the unscoped form costs — and to the UID
        # epoch it was queued in, which is what keeps "this one" from becoming
        # "whichever message inherited that number" (see _select_verified).
        _select_verified(db, c, action, p["folder"])
        _remove_message(c, p["uid"])

    elif t == "create_folder":
        # Idempotent: a retry after a timeout that actually landed must not fail
        # the action permanently on an ALREADYEXISTS. The folder row itself is
        # created by the LIST pass that follows this drain, not here.
        # Where a user folder is allowed to live is the server's business, not
        # the web app's, and only this side can see it — so the name arrives as
        # a bare leaf and gets its namespace here.
        parent = bridge.user_folder_parent()
        name = p["name"]
        if parent and not name.startswith(parent):
            name = parent + name
        if not c.folder_exists(name):
            c.create_folder(name)
        # Best-effort: Bridge subscribes on create and then rejects the
        # redundant SUBSCRIBE outright ("already subscribed to this mailbox").
        # Letting that propagate would fail — and endlessly retry — an action
        # whose folder is already sitting on the server.
        try:
            c.subscribe_folder(name)
        except Exception:  # noqa: BLE001
            pass

    elif t == "send":
        outbound = db.get(Outbound, p["outbound_id"])
        if outbound is None or not outbound.raw_mime:
            raise ValueError(f"outbound {p.get('outbound_id')} has no MIME to send")
        smtp.send_raw(bridge.acc, p["mail_from"], p["rcpt_to"], outbound.raw_mime.encode("utf-8"))

    else:
        raise ValueError(f"unknown action type: {t}")


def _due(action: PendingAction, now) -> bool:
    """Has this action waited out its backoff?

    A row that has never been tried is due immediately — the common case is an
    action queued a moment ago by someone still looking at the window.
    ``updated_at`` is when the last attempt settled, which _settle writes on
    every pass through, successful or not.

    Two clocks, and a row is due only when both have run out. The second is the
    deliberate wait a send can be given when it is composed (core/outbox.py):
    it is not a backoff and does not double, and it applies to the first attempt
    as much as to the fortieth — which is exactly the difference between "this
    failed and will be retried later" and "this has not been sent yet on
    purpose". The user can end it from the Outbox at any time; that clears the
    field rather than reaching in here.
    """
    hold = outbox.not_before(action)
    if hold is not None and now < hold:
        return False
    if not action.attempts:
        return True
    last = action.updated_at
    return last is None or now - last >= retry_delay(action.attempts)


def _due_clause(now):
    """``_due``, as SQL — the filter that decides what a pass even looks at.

    Reading the oldest N rows and sorting the due ones out in Python is fine
    while everything in the queue is trying to happen now, and wrong the moment
    something in it is deliberately waiting: a few hundred sends scheduled for
    tomorrow morning are the oldest pending rows in the table, they stay that
    way all day, and every flag change queued behind them sits unapplied until
    they clear. Asking the database for *due* rows and taking the oldest of
    those makes the queue's depth stop mattering.

    Both of _due's clocks are here. The backoff is the same doubling curve
    ``retry_delay`` computes, written out as an interval; the hold is the
    ``not_before`` instant a send can carry, compared as text — ISO-8601 in a
    fixed layout sorts chronologically, and (unlike a cast) a hand-edited value
    that is not a timestamp at all cannot fail the whole query. Such a value is
    treated as no hold, which is what ``outbox.not_before`` does with it: sending
    a message a little early is a smaller failure than never sending it.
    """
    doublings = func.least(func.greatest(PendingAction.attempts - 1, 0), _RETRY_DOUBLINGS)
    backoff = func.least(_RETRY_BASE * func.power(2, doublings), _RETRY_CEILING)
    hold = PendingAction.payload["not_before"].astext
    return and_(
        or_(PendingAction.attempts == 0,
            PendingAction.updated_at.is_(None),
            PendingAction.updated_at + func.make_interval(0, 0, 0, 0, 0, 0, backoff) <= now),
        or_(hold.is_(None),
            ~hold.op("~")(r"^\d{4}-\d{2}-\d{2}T"),
            hold <= now.isoformat()),
    )


def _settle(db, action: PendingAction, ok: bool, error: str | None = None) -> None:
    """Record an attempt's outcome. Success retires the action; failure never
    does — it only schedules the next attempt (see _due).

    There is deliberately no path here that ends in ``status = "error"``.
    Retiring a failed action was how a queued message could be quietly lost: five
    failures inside three minutes — one wrong port, one signed-out Bridge — and
    the mail was marked failed and never mentioned again. The state a failure
    leaves behind is the same state it started in, "queued and waiting", which
    is the truth.
    """
    action.attempts += 1
    action.status = "done" if ok else "pending"
    action.error = error
    # Explicit rather than left to onupdate: this is the retry clock _due reads,
    # and a settle that changed no other column (same error text as last time,
    # which is the norm for a broken config) would otherwise not touch it and
    # the action would come back due on every pass.
    action.updated_at = utcnow()

    # A successful send flips its Outbound to "sent" (Proton then auto-saves it
    # to Sent, which the next folder sync ingests normally). A failed one stays
    # "queued", because it is: the bytes are still here and the agent is still
    # going to send them.
    if action.type == "send":
        outbound = db.get(Outbound, (action.payload or {}).get("outbound_id"))
        if outbound:
            outbound.state = "sent" if ok else "queued"
            outbound.error = error
            if ok:
                outbound.sent_at = utcnow()


def _undo_optimistic_placement(db, action: PendingAction) -> None:
    """Take back the copy the UI filed for a move that is now never going to run.

    A move writes its target placement immediately, so the message is in the
    folder you filed it into before the agent has said anything to the server
    (core.mail.store.place_pending). That placement is retired when the real one
    arrives — and if the move is dropped, none ever will: the message would sit
    in the target folder here and in its old folder on the server, for good.

    Safe to remove precisely because of what causes a drop. A new UIDVALIDITY is
    also what rewinds the folder's cursor, so the pass that drops this action
    re-walks every UID in the mailbox minutes later and re-places the message
    wherever the server actually has it. A refused destination (see
    _settle_refused) has no cursor rewind behind it and repairs a sweep later
    instead: the source folder still lists the UID, the dropped action no longer
    claims the move is coming, and _restore_unplaced puts the placement back.
    """
    payload = action.payload or {}
    folder = payload.get("to_folder")
    if action.type != "move" or not folder or not action.message_pk:
        return
    mailbox_id = db.scalar(
        select(Mailbox.id).where(Mailbox.account_id == action.account_id,
                                 Mailbox.imap_name == folder)
    )
    if mailbox_id is not None:
        store.drop_pending_placement(db, action.message_pk, mailbox_id)


def _settle_unknown(db, account, action: PendingAction, exc: Exception) -> None:
    """Park a send whose outcome nobody can find out, and say so.

    The connection died between the message going out and the server saying it
    had it. Retrying might deliver it twice; not retrying might never deliver it
    at all; and there is no third question to ask, because SMTP has no "did you
    get that". Both mistakes are the user's to choose between, so the row goes to
    "held" — the same state Cancel uses, which the drain does not select — and
    the Outbox offers Send now beside an explanation of what is known.

    The attempt is counted and the reason recorded, so the row does not look
    untouched to anyone reading it later.
    """
    action.attempts += 1
    action.status = "held"
    action.error = repr(exc)
    action.updated_at = utcnow()
    # Marked on the row, so the Outbox can say "this may already have been sent"
    # rather than "you cancelled this" — the same parked state, two very
    # different sentences. Replaced rather than mutated: payload is plain JSONB
    # and an edit in place is committed as nothing at all.
    action.payload = {**(action.payload or {}), "delivery_unknown": True}
    outbound = db.get(Outbound, (action.payload or {}).get("outbound_id"))
    if outbound:
        outbound.state = "held"
        outbound.error = str(exc)
    log.warn(f"{_describe(action)}: {exc}. It has NOT been retried — the server may "
             f"have taken it, and sending again would be a second copy. It is in the "
             f"Outbox, where Send now will try again if it never arrived.",
             getattr(account, "email", None))


def _settle_stale(db, account, action: PendingAction, exc: StaleUid) -> None:
    """Take an action out of the queue because its UID no longer names anything.

    The one thing in here that is dropped rather than retried, and the exception
    proves the rule: everything else stays queued because trying again can still
    work. A UIDVALIDITY change is the opposite — the number the action carries
    will never again mean the message it was written for, so a retry is not a
    second chance at the user's instruction, it is the same instruction pointed
    at a stranger's mail.

    Nothing is lost by dropping it. A flag or a move that did not reach the
    server is re-derived from the placement on the next reconcile; a delete that
    did not happen leaves the message where it is, in Trash, where the user can
    ask again. Both are said out loud, because "the folder was rebuilt and your
    queued changes were dropped" is news.
    """
    action.status = "stale"
    action.error = str(exc)          # read by a person, not a log — see _settle_refused
    action.updated_at = utcnow()
    _undo_optimistic_placement(db, action)
    log.warn(f"{_describe(action)} was dropped: {exc}. Nothing was changed on the "
             "server — the folder will be re-read on this pass, and the change can "
             "be made again from there.", getattr(account, "email", None))


def _mark_write_refused(db, action: PendingAction) -> None:
    """Write down that this account's server will not take mail into that folder.

    So that the next archive does not queue the same doomed move. The app picks
    the destination and cannot try it — only the agent ever holds a connection —
    so the one place that finds out has to leave the answer somewhere the app
    reads. With the flag set, app/routers/actions.py stops offering \\All as a
    fallback archive target and says what is missing instead, which turns a
    silent failure fifteen minutes later into a refusal at the keypress.

    Cleared by nothing. A server that starts accepting mail into its \\All
    folder is not a thing that happens; a folder that is deleted and re-listed
    gets a new row anyway.
    """
    folder = (action.payload or {}).get("to_folder")
    if not folder:
        return
    # One statement rather than a read and a write: the column is written here
    # and read by the other process, nothing in this pass looks at it again, and
    # the WHERE keeps the first refusal's timestamp rather than moving it
    # forward every time another queued move finds the same closed door.
    db.execute(
        update(Mailbox)
        .where(Mailbox.account_id == action.account_id,
               Mailbox.imap_name == folder,
               Mailbox.writes_refused_at.is_(None))
        .values(writes_refused_at=utcnow())
    )


def _settle_refused(db, account, action: PendingAction, exc: Refused) -> None:
    """Take an action out of the queue because the server has said no for good.

    The second of the two exceptions to "nothing here ever gives up", and it is
    the same exception as the first (see _settle_stale): retrying can no longer
    produce a different outcome, so the retry is not a second chance at the
    user's instruction — it is a loop that costs a command every quarter of an
    hour and, worse, goes on telling the rest of meerail that the move is about
    to land.

    Nothing is lost by dropping it. The message never moved, so it is still
    exactly where the server has always had it; the optimistic placement written
    at the keypress is taken back here, and the next sweep restores the source
    placement the UI removed (agent/sync._restore_unplaced, which was being told
    to wait by the very action this drops). What the user asked for did not
    happen, which is why this is a warning with the reason in it rather than a
    line in a counter.
    """
    action.status = "refused"
    # The sentence, not the repr. A dropped action's error is read by a person —
    # the status panel puts it on screen verbatim, because it is the only place
    # the reason for a change that did not happen is ever stated — and
    # `Refused("...")` around it is noise from this file's implementation. The
    # retryable failures below keep repr(): those are diagnostics for a log.
    action.error = str(exc)
    action.updated_at = utcnow()
    _mark_write_refused(db, action)
    _undo_optimistic_placement(db, action)
    log.warn(f"{_describe(action)} was NOT done and has been dropped: {exc} "
             f"Nothing on the server was changed — the message is still where it "
             f"was, and this pass reads that back.", getattr(account, "email", None))


def _describe(action: PendingAction) -> str:
    """What an action was trying to do, in the terms the user thinks in.

    ``PendingAction`` row 41 of type ``send`` is not something anyone can act
    on; "send to arne@example.com" is.
    """
    p = action.payload or {}
    if action.type == "send":
        return f"send to {', '.join(p.get('rcpt_to') or []) or '(no recipients)'}"
    if action.type == "move":
        return f"move uid {p.get('uid')} from {p.get('from_folder')} to {p.get('to_folder')}"
    if action.type == "delete":
        return f"delete uid {p.get('uid')} in {p.get('folder')}"
    if action.type == "setflags":
        return f"flag uid {p.get('uid')} in {p.get('folder')}"
    if action.type == "create_folder":
        return f"create folder {p.get('name')}"
    return action.type


def _log_failure(account, action: PendingAction, exc: Exception) -> None:
    """Say out loud that an action failed, and whether it will be tried again.

    The failure was always recorded — on ``PendingAction.error`` and, for a
    send, on ``Outbound.error`` — but nothing reads those columns, so the only
    thing the agent printed was "applied 1 queued action(s)" and the mail
    simply never arrived (issue #7). A wrong ``smtp_security`` then looks
    exactly like a healthy agent, right up until someone notices the reply they
    wrote yesterday is not in Sent.

    Sends name the SMTP endpoint they were attempted against, because the
    failure is nearly always the endpoint rather than the message: Bridge picks
    its own ports and its own security mode per platform, and the config saying
    something different is what this line exists to show.
    """
    email = getattr(account, "email", None)
    what = _describe(action)
    where = ""
    if action.type == "send":
        where = (f" via {account.smtp_host}:{account.smtp_port} "
                 f"({account.smtp_security})")
    wait = retry_delay(action.attempts)
    when = (f"{int(wait.total_seconds() // 60)}m" if wait.total_seconds() >= 60
            else f"{int(wait.total_seconds())}s")
    log.error(f"{what}{where} failed (attempt {action.attempts}, "
              f"retrying in {when}): {exc!r}", email)
    advice = log.hint(exc)
    if advice:
        log.warn(advice, email)
    # Said once, at the point where a wrong password stops looking like a
    # hiccup: this will now go on failing quietly on a fifteen-minute clock, and
    # nobody reading the log an hour later should have to work out that the mail
    # sitting in the outbox is still there because of it.
    if action.attempts == _NAG_AT and action.type == "send":
        log.warn(f"{what} has now failed {action.attempts} times and is still queued — "
                 "it will keep being retried, but nothing will send until the cause "
                 "above is fixed", email)


def _claimable(action: PendingAction, now) -> bool:
    """May this pass take this row? Asked again under the lock, in _lease."""
    if action.status == "pending":
        return _due(action, now)
    if action.status == "leased":
        # A lease nobody came back for. The agent holding it was killed between
        # taking it and settling it — a redeploy, a laptop lid, an OOM — and the
        # row would otherwise sit leased for good, which for a send is mail that
        # never goes out. Reclaimed as due rather than as a failure: no attempt
        # was recorded against it, because none finished.
        return action.updated_at is None or now - action.updated_at >= _LEASE_TTL
    return False


def _lease(db, action_id: int, now) -> PendingAction | None:
    """Take one row for this agent, visibly, and commit that before doing any
    work. None means somebody else has it.

    Two separate races end here, and both are about a row that says "pending"
    while an SMTP conversation about it is already under way.

    The first is two agents over one database — an old process that has not
    exited, a restart that overlaps itself, a laptop and a server both pointed at
    the same Postgres. ``FOR UPDATE SKIP LOCKED`` settles that: the first to
    reach the row gets it and the second is handed nothing rather than blocking
    behind it.

    The second is the user, and a row lock alone could not settle it, because the
    Outbox's Cancel and Send-now do not take one — they read the row, see
    "pending", and write "held" or move the clocks while the message is at that
    moment going down the wire. Cancel then reported success for mail that had
    already been sent, and Send-now re-queued a send that was mid-flight, which
    is how one message arrives twice. So the claim is *written down*: status
    "leased", committed before the first SMTP or IMAP command, which is a state
    the Outbox can see and refuse against (app/routers/outbox.py).

    The lease is not permanent — nothing here ever is. See _claimable for the
    agent that dies holding one.
    """
    action = db.execute(
        select(PendingAction).where(PendingAction.id == action_id).with_for_update(skip_locked=True)
    ).scalars().first()
    if action is None or not _claimable(action, now):
        # Nothing written, and the lock (if we even got one) goes back now rather
        # than being held to the end of the pass over a row we are not touching.
        db.rollback()
        return None
    action.status = "leased"
    action.updated_at = now
    db.commit()
    return action


def drain_actions(db, bridge, account) -> tuple[int, int, int]:
    """Apply the queued actions that are due for this account.

    Returns (applied, failed, sent). Failures are counted rather than raised:
    one wedged action must not stop the rest of the queue, and the retry is a
    later pass's business. The caller only needs the numbers for its summary
    line — every failure has already reported itself in full by the time this
    returns — and ``sent``, which says whether mail has just gone out and the
    server therefore has a copy of it to finish assembling.

    One transaction per action, not one per pass. A lease has to be committed
    before the work it covers begins or it is not a lease at all (see _lease),
    and settling in the same small transaction means a pass killed at action
    thirty cannot take the record of the twenty-nine before it down with it —
    including the "done" on a message that has already been handed to SMTP.
    """
    now = utcnow()
    queued = db.execute(
        select(PendingAction)
        .where(PendingAction.account_id == account.id,
               or_(PendingAction.status == "pending",
                   and_(PendingAction.status == "leased",
                        PendingAction.updated_at <= now - _LEASE_TTL)),
               _due_clause(now))
        .order_by(PendingAction.created_at)
        .limit(_PER_PASS)
    ).scalars().all()

    applied = failed = 0
    sends = 0       # send actions tried
    sent = 0        # ...and the ones the server took
    for row in queued:
        action = _lease(db, row.id, now)
        if action is None:
            continue
        is_send = action.type == "send"
        sends += is_send
        try:
            # The lease is kept alive for as long as this takes, so that "no
            # agent has touched this in fifteen minutes" stays a statement about
            # a dead agent rather than about a large attachment.
            with _LeaseKeeper(action.id):
                apply_action(db, bridge, account, action)
            _settle(db, action, True)
            applied += 1
            sent += is_send
            # Only sends get a line of their own. A flag or a move is the tail
            # end of something the user watched happen in the UI; a send is the
            # one action whose success they have no other way to confirm, and
            # "did that mail actually go out on Friday" is a question the log
            # should be able to answer months later.
            if is_send:
                rcpt = ", ".join((action.payload or {}).get("rcpt_to") or []) or "(no recipients)"
                log.ok(f"sent to {rcpt}", getattr(bridge.acc, "email", None))
        except smtp.PartlyRefused as e:
            # Delivered — to everyone the server would take. Settled as a success
            # for exactly that reason: retrying would send it a second time to
            # the people who did get it, for the sake of the ones no number of
            # attempts will reach. The refusals ride along on the message.
            _settle(db, action, True, repr(e))
            applied += 1
            sent += is_send
            log.warn(f"{_describe(action)}: {e}", getattr(bridge.acc, "email", None))
        except smtp.Delivered as e:
            # The server may have it and cannot be asked. Neither answer is
            # available, so neither is invented: the row is parked and the
            # Outbox says what happened, because "send it again" is a decision
            # with a duplicate on one side of it and a lost message on the other,
            # and it is not the agent's to make.
            _settle_unknown(db, bridge.acc, action, e)
            sent += is_send
        except StaleUid as e:
            # Not a failure to retry: the message this names cannot be addressed
            # again, by this pass or any later one.
            _settle_stale(db, bridge.acc, action, e)
        except Refused as e:
            # Also not a failure to retry, for the other reason: the server has
            # answered, and the answer is not going to change. Counted with
            # neither the applied nor the failed, because it is neither — it has
            # reported itself in full and in the user's terms already.
            _settle_refused(db, bridge.acc, action, e)
        except Exception as e:  # noqa: BLE001
            _settle(db, action, False, repr(e))
            failed += 1
            # After _settle: the attempt count it writes is what the log line
            # reports, and the backoff is computed from it.
            _log_failure(bridge.acc, action, e)
        db.commit()
    # One event for the drain, not one per message: the UI reads this as "the
    # outbox changed, re-read the count". Only when a send was actually tried —
    # a pass that pushed nothing but flags leaves the outbox exactly as it was.
    if sends:
        events.publish({"type": "outbox", "sent": applied, "failed": failed})
    return applied, failed, sent


# --- mail that is still waiting ----------------------------------------------
#
# A failed *attempt* reports itself in full (see _log_failure). A send that is
# never attempted reports nothing at all, and that is the case people actually
# hit: Bridge is down, or signed out, or the host has been asleep, so the pass
# dies at connect() and drain_actions is never reached. The log then says "sync
# failed" over and over and never once mentions that there is mail in the
# outbox behind it — which is exactly the report this was written for, mail
# sitting in `outbound` with no way to know it was there.
#
# So the queue gets said out loud too: at startup, and after a failed pass.

# Per account, so a retry loop failing every thirty seconds does not reprint the
# same list two thousand times before anyone reads it. The failure itself is
# still logged every time; this is the reminder of what is riding on it.
_REPORT_EVERY = 600.0
_last_report: dict[str, float] = {}


def _report_due(key: str) -> bool:
    now = time.monotonic()
    if now - _last_report.get(key, -_REPORT_EVERY) < _REPORT_EVERY:
        return False
    _last_report[key] = now
    return True


def report_waiting(db, email: str | None = None, throttle: bool = False) -> int:
    """Log the mail that has been written and not yet sent. Returns the count.

    ``email`` limits it to one account (the retry loop knows which one it is);
    without it the whole queue is reported, which is what startup wants.
    """
    account_id = None
    if email is not None:
        account_id = db.scalar(select(Account.id).where(Account.email == email))
        if account_id is None:
            return 0

    # ACTIVE_STATES, not everything in the folder: a send the user cancelled is
    # sitting there because they asked it to, and warning about it every pass
    # would make the log's one real signal — mail that cannot get out — harder
    # to see rather than easier.
    rows = outbox.unsent(db, account_id, states=outbox.ACTIVE_STATES)
    if not rows:
        return 0
    if throttle and not _report_due(email or ""):
        return len(rows)

    sends = outbox.send_actions(db, [r.id for r in rows])
    log.warn(f"{len(rows)} message(s) in the outbox have not been sent yet:", email)
    for row in rows[:10]:
        when = row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "?"
        action = sends.get(row.id)
        why = row.error or (action.error if action else None)
        if why:
            state = f"failed {action.attempts if action else 0}x: {why}"
        elif action is None:
            # No queue row at all: an older agent retired it, and nothing is
            # going to pick it up again until it is put back.
            state = "not queued — run with --requeue-abandoned"
        else:
            state = "queued, not tried yet"
        log.warn(f"  {when}  {outbox.describe(row)}  ({state})", email)
    if len(rows) > 10:
        log.warn(f"  ...and {len(rows) - 10} more", email)
    return len(rows)


# --- mail an older agent gave up on ------------------------------------------
#
# Nothing writes ``status = "error"`` any more, but a database that has been
# through the version which did is still carrying those rows — among them
# messages that were queued to send, failed five times inside three minutes, and
# have been sitting unsent and unmentioned ever since. The bytes are all still
# there. They are simply not in the queue.
#
# Putting them back is a separate, explicit command rather than something
# startup does by itself, because the one thing worse than a message that did
# not send is a message that sends on its own a week late, to a thread that has
# moved on. The agent says what it found; the user decides.


def abandoned(db, account_id: int | None = None) -> list[PendingAction]:
    """Actions an older agent retired as permanently failed, oldest first."""
    q = select(PendingAction).where(PendingAction.status == "error")
    if account_id is not None:
        q = q.where(PendingAction.account_id == account_id)
    return db.execute(q.order_by(PendingAction.created_at)).scalars().all()


def report_abandoned(db) -> int:
    """Log what the old attempt cap left behind. Returns how many rows there are."""
    rows = abandoned(db)
    if not rows:
        return 0
    sends = [r for r in rows if r.type == "send"]
    if sends:
        log.warn(f"{len(sends)} message(s) were given up on by an older version of the "
                 f"agent and never sent. They are still in the database:")
        for action in sends[:10]:
            when = action.created_at.strftime("%Y-%m-%d %H:%M") if action.created_at else "?"
            log.warn(f"  {when}  {_describe(action)}  ({action.error or 'no error recorded'})")
        if len(sends) > 10:
            log.warn(f"  ...and {len(sends) - 10} more")
    other = len(rows) - len(sends)
    if other:
        log.warn(f"{other} other queued action(s) were given up on in the same way.")
    log.warn("Run the agent once with --requeue-abandoned to put them back in the "
             "queue; nothing is sent until you do.")
    return len(rows)


def requeue_abandoned(db) -> int:
    """Put every retired action back in the queue, from the top. Returns the count."""
    rows = abandoned(db)
    for action in rows:
        action.attempts = 0
        action.status = "pending"
        action.error = None
        action.updated_at = utcnow()
        if action.type == "send":
            outbound = db.get(Outbound, (action.payload or {}).get("outbound_id"))
            if outbound is not None and outbound.state == "error":
                outbound.state = "queued"
                outbound.error = None
    db.commit()
    return len(rows)
