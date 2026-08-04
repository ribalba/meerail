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

from datetime import timedelta

from sqlalchemy import select

from core import events
from core.models import Mailbox, Outbound, PendingAction, utcnow

import log
import smtp

# How long after a failure an action may be tried again: doubling from
# _RETRY_BASE up to _RETRY_CEILING, from the attempt count the row already
# carries.
#
# Not politeness towards the server — every failed send costs the sync pass its
# full SMTP timeout (a minute by default), at the head of the pass, before any
# mail is fetched. Retried every pass, one message with a wrong port would slow
# every sync for as long as it stayed wrong, which is how issue #7 read in the
# log: passes of 65s and 180s where a healthy one takes half a second. Backing
# off keeps a permanently-failing action at a cost that tends to nothing while
# still retrying it forever.
#
# The ceiling is deliberately short. An hour would be tidier arithmetic, but the
# case that matters is the laptop that has just come back online after four days
# — everything in the queue is long overdue by then, so the ceiling only ever
# decides how long the *last* stretch of a broken spell lasts once it is fixed.
_RETRY_BASE = 60          # seconds, after the first failure
_RETRY_CEILING = 900      # 15 minutes
_RETRY_DOUBLINGS = 8      # 60s * 2**8 is well past the ceiling; keeps it finite

# How deep to look for work in one pass. Nothing is ever retired, so the queue
# can hold rows that have been failing for weeks; ordering by age and scanning
# past the ones that are not due yet is what stops those from crowding out a
# message queued a second ago.
_SCAN = 500
_PER_PASS = 50

# Failure number at which a still-queued send says so in its own right, rather
# than only as one more line in a series. Far enough in that a Bridge restart
# never reaches it.
_NAG_AT = 10


def _is_all_mail(db, account_id: int, imap_name: str) -> bool:
    """Is this folder the account's \\All mailbox? Names differ per server
    ("All Mail", "[Gmail]/All Mail"), so it is the role the sync pass recorded
    from the SPECIAL-USE flag that decides, not the name."""
    return db.scalar(
        select(Mailbox.role).where(Mailbox.account_id == account_id,
                                   Mailbox.imap_name == imap_name)
    ) == "all"


def apply_action(db, bridge, account, action: PendingAction) -> None:
    t = action.type
    p = action.payload or {}
    # Not bridge.client: every command here has to sit under the same stall
    # watchdog as the fetch path, or a wedged write-back stops the account
    # syncing with nothing logged. See Bridge.ops.
    c = bridge.ops()

    if t == "setflags":
        c.select_folder(p["folder"])          # readwrite
        if p.get("add"):
            c.add_flags([p["uid"]], p["add"])
        if p.get("remove"):
            c.remove_flags([p["uid"]], p["remove"])

    elif t == "move":
        c.select_folder(p["from_folder"])
        c.copy([p["uid"]], p["to_folder"])
        # \All is not a folder a message can be taken out of. On Proton and
        # Gmail it is the union of everything the account holds — filing to
        # Archive or Trash is a label change that the COPY has already made,
        # and the EXPUNGE that would follow is a step with nothing to do. The
        # server says so ("EXPUNGE failed: operation not allowed") and the
        # whole action fails on it, retries, and fails again: 267 archived and
        # trashed messages piled up in the queue that way, every one of them
        # already filed on the server.
        if not _is_all_mail(db, action.account_id, p["from_folder"]):
            c.delete_messages([p["uid"]])
            c.expunge()

    elif t == "delete":
        c.select_folder(p["folder"])
        c.delete_messages([p["uid"]])
        c.expunge()

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


def retry_delay(attempts: int) -> timedelta:
    """How long an action that has failed ``attempts`` times waits before the
    next try. Doubles from _RETRY_BASE, flat at _RETRY_CEILING thereafter."""
    doublings = min(max(attempts - 1, 0), _RETRY_DOUBLINGS)
    return timedelta(seconds=min(_RETRY_BASE * 2 ** doublings, _RETRY_CEILING))


def _due(action: PendingAction, now) -> bool:
    """Has this action waited out its backoff?

    A row that has never been tried is due immediately — the common case is an
    action queued a moment ago by someone still looking at the window.
    ``updated_at`` is when the last attempt settled, which _settle writes on
    every pass through, successful or not.
    """
    if not action.attempts:
        return True
    last = action.updated_at
    return last is None or now - last >= retry_delay(action.attempts)


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


def drain_actions(db, bridge, account) -> tuple[int, int]:
    """Apply the queued actions that are due for this account.

    Returns (applied, failed, sent). Failures are counted rather than raised:
    one wedged action must not stop the rest of the queue, and the retry is a
    later pass's business. The caller only needs the numbers for its summary
    line — every failure has already reported itself in full by the time this
    returns — and ``sent``, which says whether mail has just gone out and the
    server therefore has a copy of it to finish assembling.
    """
    now = utcnow()
    queued = db.execute(
        select(PendingAction)
        .where(PendingAction.account_id == account.id, PendingAction.status == "pending")
        .order_by(PendingAction.created_at)
        .limit(_SCAN)
    ).scalars().all()
    actions = [a for a in queued if _due(a, now)][:_PER_PASS]

    applied = failed = 0
    sends = 0       # send actions tried
    sent = 0        # ...and the ones the server took
    for action in actions:
        is_send = action.type == "send"
        sends += is_send
        try:
            apply_action(db, bridge, account, action)
            _settle(db, action, True)
            applied += 1
            sent += is_send
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
