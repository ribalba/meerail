"""Direct-to-database helpers for tests.

Mail ingest lives in the agent now, so tests exercise it the same way the agent
does: call ``core.ingest`` against the database. There is no ingest HTTP API left
to post to.

Requires the shared ``core`` dependencies (SQLAlchemy, psycopg, selectolax) —
``agent/.venv`` has them, so run the suite with that interpreter:

    agent/.venv/bin/python -m pytest tests/

Reads DATABASE_URL from the environment (or .env), defaulting to the loopback
port docker-compose publishes.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta
from email.utils import parseaddr

from core import ingest
from core.database import SessionLocal
from core.models import (
    Account, Attachment, Mailbox, Message, MessageLocation, Outbound, PendingAction,
    Reminder, UiSession, utcnow,
)


@contextlib.contextmanager
def session():
    """A committed-on-exit session, mirroring how the agent works."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    finally:
        db.close()


def create_account(email: str, label: str = "") -> dict:
    """Register an account the only way there is — the agent's path.

    There is no account-creation HTTP API: the web app never provisions accounts,
    it only edits presentation on rows the agent has already inserted.
    """
    with session() as db:
        account = ingest.get_or_create_account(db, email)
        if label:
            account.label = label
        db.flush()
        return {"id": account.id, "email": account.email, "label": account.label}


def _mailbox(db, account: Account, imap_name: str, role_hint: str = "",
             uidvalidity: int = 1) -> Mailbox:
    return ingest.register_folder(db, account, imap_name, role_hint, uidvalidity, None)


def ingest_raw_message(email: str, raw: bytes, uid: int = 1, folder: str = "INBOX",
                       flags: dict | None = None, role_hint: str = "",
                       uidvalidity: int = 1, received=None) -> None:
    """Ingest one raw message into a folder, exactly as a sync pass would.

    ``received`` is what the server's INTERNALDATE said — when the mail arrived,
    which is the clock the content window is measured on. It defaults to the
    message's own Date, which is the ordinary case (mail arrives when it is
    sent); passing them apart is how a test stages a backdated message.
    """
    from core.mail.parse import parse_email

    with session() as db:
        account = ingest.get_or_create_account(db, email)
        mailbox = _mailbox(db, account, folder, role_hint, uidvalidity)
        arrival = received if received is not None else parse_email(raw).date_sent
        ingest.store_message(db, account, mailbox, uid, flags or {}, raw,
                             received=arrival)
        ingest.advance_cursor(db, mailbox, uid)


def ingest_header_block(email: str, header_bytes: bytes, uid: int = 1, folder: str = "INBOX",
                        flags: dict | None = None, size_bytes: int | None = None) -> None:
    """Ingest a message's headers with no content, as the agent does for mail
    that falls outside the content window."""
    with session() as db:
        account = ingest.get_or_create_account(db, email)
        mailbox = _mailbox(db, account, folder)
        ingest.store_headers(db, account, mailbox, uid, flags or {}, header_bytes, size_bytes)
        ingest.advance_cursor(db, mailbox, uid)


def prune_content(cutoff) -> int:
    """Run one prune batch, as the agent's indexer thread does."""
    with session() as db:
        return ingest.prune_expired_content(db, cutoff)


def set_content_window(months: int) -> None:
    """Publish a content window the way a sync pass does."""
    with session() as db:
        ingest.record_content_window(db, months)


def register_folder(email: str, name: str = "INBOX", uidvalidity: int = 1) -> int:
    """Re-register a folder as the start of a sync pass does. Returns its cursor."""
    with session() as db:
        account = ingest.get_or_create_account(db, email)
        mailbox = _mailbox(db, account, name, uidvalidity=uidvalidity)
        db.flush()
        return mailbox.last_uid


def create_folder(email: str, name: str, role_hint: str = "") -> int:
    """Register a folder the way a sync pass's LIST would. Returns its id."""
    with session() as db:
        account = ingest.get_or_create_account(db, email)
        return _mailbox(db, account, name, role_hint).id


def record_placement(email: str, message_id: str, uid: int, folder: str,
                     flags: dict | None = None, role_hint: str = "",
                     uidvalidity: int = 1, size: int | None = None, date=None) -> bool:
    """Record a second folder placement for content already stored (Proton labels).

    Advances the cursor afterwards, as a real sync pass does — that is what
    refreshes the folder's denormalized counts.

    ``size`` and ``date`` are what the server reported about this UID in the
    header pass, and are what a real sync always has to hand. Passing them is how
    a test says "and this UID really is that message"; leaving them out matches
    by Message-ID alone.
    """
    with session() as db:
        account = ingest.get_or_create_account(db, email)
        mailbox = _mailbox(db, account, folder, role_hint, uidvalidity)
        matched = ingest.record_known(db, account, mailbox, uid, flags or {}, message_id,
                                      size=size, date=date)
        ingest.advance_cursor(db, mailbox, uid)
        return matched


def short_content_uids(email: str, folder: str, sizes: dict[int, int]) -> list[int]:
    """Which stored placements hold less than the server says, as the reconcile
    sweep asks it."""
    with session() as db:
        account = ingest.get_or_create_account(db, email)
        mailbox = _mailbox(db, account, folder)
        return ingest.find_short_content(db, mailbox, sizes)


def restore_message_content(email: str, folder: str, uid: int, raw: bytes) -> bool:
    """Re-store a message's content from freshly fetched bytes, as the sweep does
    once it has found one short."""
    with session() as db:
        account = ingest.get_or_create_account(db, email)
        mailbox = _mailbox(db, account, folder)
        return ingest.restore_content(db, mailbox, uid, raw)


def set_flags(email: str, folder: str, items: list[dict]) -> int:
    with session() as db:
        account = ingest.get_or_create_account(db, email)
        mailbox = _mailbox(db, account, folder)
        return ingest.update_flags(db, mailbox, items)


def set_present(email: str, folder: str, uids: list[int]) -> int:
    with session() as db:
        account = ingest.get_or_create_account(db, email)
        mailbox = _mailbox(db, account, folder)
        return ingest.prune_vanished(db, mailbox, uids)


def unplaced_uids(email: str, folder: str, uids: list[int]) -> list[int]:
    """Which of these server UIDs the folder holds no placement for — the
    question the agent's reconcile asks before fetching anything back."""
    with session() as db:
        account = ingest.get_or_create_account(db, email)
        mailbox = _mailbox(db, account, folder)
        return ingest.unplaced_uids(db, mailbox, uids)


def move_in_flight(email: str, message_id: str | None, headers: bytes | None = None,
                   date=None) -> bool:
    """The question the repair asks before putting a placement back. ``headers``
    and ``date`` are what it has to go on for mail whose sender wrote no
    Message-ID."""
    with session() as db:
        account = ingest.get_or_create_account(db, email)
        return ingest.has_move_in_flight(db, account, message_id, headers=headers, date=date)


def rethread(account_id: int) -> tuple[int, int]:
    """Rebuild every thread_id for an account, as core.mail.rethread does when a
    threading rule changes. Returns (messages, changed)."""
    from core.mail.rethread import rethread_account

    with session() as db:
        return rethread_account(db, account_id)


def prune_folders(email: str, present_names: set[str], after_hours: int = 0) -> int:
    """One pass's folder prune. ``after_hours`` moves the pass's clock forward,
    which is how a test says "this folder has been missing for that long" — a
    folder is not removed on one absence (core/ingest.py::prune_mailboxes)."""
    with session() as db:
        account = ingest.get_or_create_account(db, email)
        return ingest.prune_mailboxes(db, account, present_names,
                                      now=utcnow() + timedelta(hours=after_hours))


def queue_move_for(email: str, subject: str) -> int:
    """Queue a move naming a message, as the UI does when you archive it."""
    with session() as db:
        account = db.query(Account).filter(Account.email == email.lower()).one()
        msg = db.query(Message).filter(Message.account_id == account.id,
                                       Message.subject == subject).one()
        action = PendingAction(account_id=account.id, message_pk=msg.id, type="move",
                               payload={"from_folder": "INBOX", "uid": 1,
                                        "uidvalidity": 1, "to_folder": "Archive"})
        db.add(action)
        db.flush()
        return action.id


def queue_move_without_message(email: str, op_id: str) -> int:
    """Queue a move that names no message, under ``op_id``.

    The shape ``_cancel`` guards against and could not survive: a logged action
    whose ``message_pk`` is NULL, so the message lookup comes back empty. Nothing
    writes one today — every move is queued against a message, and the FK
    cascades the row away if that message is ever deleted — which is exactly why
    the branch that handles it went untested, and why the call in it was missing
    an argument and raised TypeError instead of retiring the row.
    """
    with session() as db:
        account = db.query(Account).filter(Account.email == email.lower()).one()
        action = PendingAction(
            account_id=account.id, message_pk=None, type="move",
            payload={"from_folder": "INBOX", "uid": 1, "uidvalidity": 1,
                     "to_folder": "Archive", "op_id": op_id, "op_kind": "archive",
                     "undo_from": []},
        )
        db.add(action)
        db.flush()
        return action.id


def action_status(action_id: int) -> tuple[str, bool]:
    """One queue row's status, and whether it has been marked undone."""
    with session() as db:
        action = db.query(PendingAction).filter(PendingAction.id == action_id).one()
        return action.status, "undone_at" in (action.payload or {})


def drop_placements(email: str, folder: str) -> int:
    """Take a folder's placements away without touching the messages — what a
    repointed UID leaves behind (see core.mail.store.upsert_location)."""
    with session() as db:
        account = db.query(Account).filter(Account.email == email.lower()).one()
        mb = db.query(Mailbox).filter(Mailbox.account_id == account.id,
                                      Mailbox.imap_name == folder).one()
        rows = db.query(MessageLocation).filter(MessageLocation.mailbox_id == mb.id).all()
        for loc in rows:
            db.delete(loc)
        return len(rows)


def collect_orphans(email: str, after_hours: int = 0) -> int:
    """Run the end-of-pass sweep for mail no folder points at any more.

    ``after_hours`` moves the sweep's clock forward, which is how a test says
    "and this has been unplaced since well before the pass started" — the window
    core.ingest.delete_orphan_messages keeps between "no folder holds this" and
    "no folder holds this yet".
    """
    with session() as db:
        account = ingest.get_or_create_account(db, email)
        return ingest.delete_orphan_messages(db, account,
                                             now=utcnow() + timedelta(hours=after_hours))


def folders_held_back(email: str) -> list[str]:
    """Folders the server has stopped listing whose mail is being kept anyway."""
    with session() as db:
        account = ingest.get_or_create_account(db, email)
        return ingest.deferred_folders(db, account)


def report_presentation(email: str, values: dict) -> None:
    """Pin display fields on an account the way the start of a sync pass does.

    `values` is what `AccountConfig.presentation()` hands over: the subset of
    label/color/footer that the agent's meerail.toml actually sets. An empty
    dict is a file that pins nothing, which is how a field is handed back.
    """
    with session() as db:
        account = ingest.get_or_create_account(db, email)
        ingest.record_presentation(db, account, values)


def report_sync(email: str, backfill_complete: bool | None = None,
                addresses: list[str] | None = None) -> None:
    """As the agent does at the end of a pass. `addresses` takes what the config
    takes: bare addresses, or `Name <addr>` to name one."""
    identities = None
    if addresses is not None:
        identities = [parseaddr(a) for a in addresses]
    with session() as db:
        account = ingest.get_or_create_account(db, email)
        ingest.record_sync(db, account, backfill_complete, identities)


def extract_all(max_batches: int = 50) -> int:
    """Drain pending Tika extraction, as the agent does after a sync pass."""
    total = 0
    with session() as db:
        for _ in range(max_batches):
            n = ingest.extract_pending(db)
            db.commit()
            if not n:
                break
            total += n
    return total


def thumb_all(max_batches: int = 50) -> int:
    """Drain pending preview rendering, as the agent does after a sync pass."""
    total = 0
    with session() as db:
        for _ in range(max_batches):
            n = ingest.thumb_pending(db)
            db.commit()
            if not n:
                break
            total += n
    return total


# --- Read helpers for asserting on agent-owned state ------------------------


def pending_actions(email: str, type_: str | None = None,
                    status: str = "pending") -> list[dict]:
    """The action queue the agent would drain, as plain dicts. ``status`` reads a
    different slice of it — "leased" is a row an agent is applying right now."""
    with session() as db:
        account = db.query(Account).filter(Account.email == email.lower()).one()
        q = db.query(PendingAction).filter(PendingAction.account_id == account.id,
                                           PendingAction.status == status)
        if type_:
            q = q.filter(PendingAction.type == type_)
        return [{"id": a.id, "type": a.type, "payload": a.payload,
                 "message_pk": a.message_pk} for a in q.order_by(PendingAction.created_at)]


def apply_actions(email: str, minutes_ago: int = 0) -> int:
    """Retire every queued action, as a successful agent pass does.

    ``minutes_ago`` backdates the settle. What the app does with a placement it
    wrote itself turns on how long ago the move behind it finished — seconds is
    a move still landing, an hour is one whose server copy is never coming — so
    a test of that needs to say which of the two it is staging.
    """
    with session() as db:
        account = db.query(Account).filter(Account.email == email.lower()).one()
        rows = db.query(PendingAction).filter(PendingAction.account_id == account.id,
                                              PendingAction.status == "pending").all()
        for a in rows:
            a.status = "done"
            a.attempts += 1
            a.updated_at = utcnow() - timedelta(minutes=minutes_ago)
        return len(rows)


def fail_actions(email: str, attempts: int, error: str = "move failed: nope") -> int:
    """Leave every queued action having failed `attempts` times, as a run of
    unsuccessful passes does. Nothing retires it — the agent never gives up on a
    failure — so the row stays pending, with the count and the reason on it.
    """
    with session() as db:
        account = db.query(Account).filter(Account.email == email.lower()).one()
        rows = db.query(PendingAction).filter(PendingAction.account_id == account.id,
                                              PendingAction.status == "pending").all()
        for a in rows:
            a.attempts = attempts
            a.error = error
            a.updated_at = utcnow()
        return len(rows)


def refuse_writes(email: str, folder: str) -> None:
    """Mark a folder as one the server will not accept mail into, which is what
    the agent writes down the first time it is told so (actions._mark_write_refused)."""
    with session() as db:
        account = db.query(Account).filter(Account.email == email.lower()).one()
        mailbox = db.query(Mailbox).filter(Mailbox.account_id == account.id,
                                           Mailbox.imap_name == folder).one()
        mailbox.writes_refused_at = utcnow()


def drop_actions(email: str, status: str, error: str) -> int:
    """Take every queued action out of the queue the way the agent does when it
    decides one cannot be carried out — "stale" for a UID that no longer names
    anything, "refused" for a destination the server will not take."""
    with session() as db:
        account = db.query(Account).filter(Account.email == email.lower()).one()
        rows = db.query(PendingAction).filter(PendingAction.account_id == account.id,
                                              PendingAction.status == "pending").all()
        for a in rows:
            a.status = status
            a.error = error
            a.updated_at = utcnow()
        return len(rows)


def set_mailbox_role(email: str, folder: str, role: str) -> None:
    """Put a folder's role back to what an older pass recorded.

    ``role`` is derived once, when the folder is first listed, and then stored —
    so a row written before a flag was published, or during a pass where the
    server answered LIST without its SPECIAL-USE flags, keeps whatever it got.
    That is not hypothetical (it is how an account with an Archive folder ended
    up archiving into All Mail), and it is not reachable through the fixture's
    ingest path, which always derives the role correctly.
    """
    with session() as db:
        account = db.query(Account).filter(Account.email == email.lower()).one()
        mailbox = db.query(Mailbox).filter(Mailbox.account_id == account.id,
                                           Mailbox.imap_name == folder).one()
        mailbox.role = role


def reminder_rows(email: str) -> list[dict]:
    """Every reminder on an account, whatever state it is in.

    The HTTP list only shows the pending ones — that is what the folder is —
    so this is how a test asserts that a fired reminder was retired rather than
    quietly left to fire again.
    """
    with session() as db:
        account = db.query(Account).filter(Account.email == email.lower()).one()
        rows = (db.query(Reminder).filter(Reminder.account_id == account.id)
                .order_by(Reminder.id).all())
        return [{"id": r.id, "message_pk": r.message_pk, "thread_id": r.thread_id,
                 "state": r.state, "due_at": r.due_at, "fired_at": r.fired_at,
                 "error": r.error, "parked": r.parked,
                 "park_mailbox_id": r.park_mailbox_id} for r in rows]


def make_reminder_due(email: str, seconds_ago: int = 5) -> int:
    """Move an account's pending reminders into the past.

    What waiting until Monday looks like from a test: the deadline is an
    absolute instant, so backdating it is the same event as the clock reaching
    it — and the worker (app/workers.py) then brings the mail back on its next
    tick with nothing else staged.
    """
    with session() as db:
        account = db.query(Account).filter(Account.email == email.lower()).one()
        rows = (db.query(Reminder)
                .filter(Reminder.account_id == account.id, Reminder.state == "pending")
                .all())
        for r in rows:
            r.due_at = utcnow() - timedelta(seconds=seconds_ago)
        return len(rows)


def attachment_rows(email: str) -> list[dict]:
    """Attachment state the read API hides (inline parts, preview status)."""
    with session() as db:
        account = db.query(Account).filter(Account.email == email.lower()).one()
        rows = (db.query(Attachment)
                .join(Message, Message.id == Attachment.message_pk)
                .filter(Message.account_id == account.id)
                .order_by(Attachment.id).all())
        return [{"filename": a.filename, "content_type": a.content_type,
                 "is_inline": a.is_inline, "extract_status": a.extract_status,
                 "thumb_status": a.thumb_status,
                 "has_thumb": a.thumb is not None} for a in rows]


def outbound_mime(outbound_id: int) -> str:
    with session() as db:
        ob = db.get(Outbound, outbound_id)
        return ob.raw_mime if ob else ""


def outbound_body_text(outbound_id: int) -> str:
    """The markdown the composer sent. For a formatted message this is the only
    record of it — the wire carries the rendering instead."""
    with session() as db:
        ob = db.get(Outbound, outbound_id)
        return ob.body_text if ob else ""


def stored_raw_mime(email: str, message_id: str) -> bytes | None:
    """The original bytes kept for a message — None when store_raw_mime is off."""
    with session() as db:
        account = db.query(Account).filter(Account.email == email.lower()).one()
        return (db.query(Message)
                .filter(Message.account_id == account.id,
                        Message.message_id == message_id)
                .one().raw_mime)


def drop_sessions() -> None:
    """Take every browser session away — what a logout does to one of them, and
    what a purge or a lost row does to all."""
    with session() as db:
        db.query(UiSession).delete()


def expire_sessions() -> None:
    """Age every session out, as thirty days would."""
    with session() as db:
        for row in db.query(UiSession).all():
            row.expires_at = utcnow() - timedelta(seconds=1)


def account_row(email: str) -> dict:
    """The account's stored presentation, straight from the table."""
    with session() as db:
        account = db.query(Account).filter(Account.email == email.lower()).one()
        return {"label": account.label, "color": account.color,
                "active": account.active, "footer": account.footer}


def message_count(email: str) -> int:
    with session() as db:
        account = db.query(Account).filter(Account.email == email.lower()).one()
        return db.query(Message).filter(Message.account_id == account.id).count()


def location_count(email: str, folder: str) -> int:
    with session() as db:
        account = db.query(Account).filter(Account.email == email.lower()).one()
        mb = db.query(Mailbox).filter(Mailbox.account_id == account.id,
                                      Mailbox.imap_name == folder).one_or_none()
        if mb is None:
            return 0
        return db.query(MessageLocation).filter(MessageLocation.mailbox_id == mb.id).count()




def record_send_failure(outbound_id: int, error: str, attempts: int = 1) -> None:
    """What the agent writes after a failed send attempt: still queued, with the
    reason on the row, and the attempt counted on the queue row beside it so the
    backoff has something to compute from. See agent/actions.py _settle."""
    with session() as db:
        ob = db.get(Outbound, outbound_id)
        ob.state = "queued"
        ob.error = error
        for action in _send_actions(db, outbound_id):
            action.attempts = attempts
            action.error = error
            action.status = "pending"
            action.updated_at = utcnow()


def mark_outbound_sent(outbound_id: int) -> None:
    """The other half of _settle: the server took it, so it leaves the outbox."""
    with session() as db:
        ob = db.get(Outbound, outbound_id)
        ob.state = "sent"
        ob.error = None
        ob.sent_at = utcnow()
        for action in _send_actions(db, outbound_id):
            action.status = "done"


def _send_actions(db, outbound_id: int) -> list[PendingAction]:
    return [a for a in db.query(PendingAction).filter(PendingAction.type == "send").all()
            if (a.payload or {}).get("outbound_id") == outbound_id]


def lease_actions(email: str) -> int:
    """Mark this account's queued actions as an agent is applying them right now.

    What ``_lease`` writes and commits before the first IMAP or SMTP command
    (agent/actions.py) — the state in which the UI must not rewrite the row it is
    holding.
    """
    with session() as db:
        account = db.query(Account).filter(Account.email == email.lower()).one()
        rows = db.query(PendingAction).filter(PendingAction.account_id == account.id,
                                              PendingAction.status == "pending").all()
        for action in rows:
            action.status = "leased"
            action.updated_at = utcnow()
        return len(rows)


def finish_send_action(outbound_id: int) -> None:
    """Settle only the queue row, leaving the Outbound as the caller found it.

    Not a state the agent ever commits — it writes both together — but exactly
    what a request that read the Outbound row a moment *before* that commit goes
    on to see: its own stale "queued", and a queue row that has finished. Which
    is the whole of the race, and the only way to stage it from outside.
    """
    with session() as db:
        for action in _send_actions(db, outbound_id):
            action.status = "done"
            action.attempts += 1
            action.updated_at = utcnow()


def lease_send_action(outbound_id: int) -> None:
    """Put a send in the state an agent leaves it in while it is inside the SMTP
    conversation for it: claimed, written down, and not yet settled. See
    agent/actions.py::_lease — this is what the Outbox has to refuse against."""
    with session() as db:
        for action in _send_actions(db, outbound_id):
            action.status = "leased"
            action.updated_at = utcnow()


def send_actions_for(outbound_id: int) -> list[dict]:
    """Every queue row driving one message's delivery, finished ones included —
    which is how a test says "and no second send was queued"."""
    with session() as db:
        return [{"id": a.id, "status": a.status} for a in _send_actions(db, outbound_id)]


def send_action_state(outbound_id: int) -> dict | None:
    """The queue row driving a send: its status, attempt count and retry clock."""
    with session() as db:
        rows = _send_actions(db, outbound_id)
        if not rows:
            return None
        a = rows[0]
        return {"status": a.status, "attempts": a.attempts, "updated_at": a.updated_at,
                "error": a.error, "payload": dict(a.payload or {})}
