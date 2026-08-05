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
    Account, Attachment, Mailbox, Message, MessageLocation, Outbound, PendingAction, utcnow,
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
                       uidvalidity: int = 1) -> None:
    """Ingest one raw message into a folder, exactly as a sync pass would."""
    with session() as db:
        account = ingest.get_or_create_account(db, email)
        mailbox = _mailbox(db, account, folder, role_hint, uidvalidity)
        ingest.store_message(db, account, mailbox, uid, flags or {}, raw)
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
                     uidvalidity: int = 1) -> bool:
    """Record a second folder placement for content already stored (Proton labels).

    Advances the cursor afterwards, as a real sync pass does — that is what
    refreshes the folder's denormalized counts.
    """
    with session() as db:
        account = ingest.get_or_create_account(db, email)
        mailbox = _mailbox(db, account, folder, role_hint, uidvalidity)
        matched = ingest.record_known(db, account, mailbox, uid, flags or {}, message_id)
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


def move_in_flight(email: str, message_id: str) -> bool:
    with session() as db:
        account = ingest.get_or_create_account(db, email)
        return ingest.has_move_in_flight(db, account, message_id)


def prune_folders(email: str, present_names: set[str]) -> int:
    with session() as db:
        account = ingest.get_or_create_account(db, email)
        return ingest.prune_mailboxes(db, account, present_names)


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


def pending_actions(email: str, type_: str | None = None) -> list[dict]:
    """The action queue the agent would drain, as plain dicts."""
    with session() as db:
        account = db.query(Account).filter(Account.email == email.lower()).one()
        q = db.query(PendingAction).filter(PendingAction.account_id == account.id,
                                           PendingAction.status == "pending")
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


def send_action_state(outbound_id: int) -> dict | None:
    """The queue row driving a send: its status, attempt count and retry clock."""
    with session() as db:
        rows = _send_actions(db, outbound_id)
        if not rows:
            return None
        a = rows[0]
        return {"status": a.status, "attempts": a.attempts, "updated_at": a.updated_at,
                "error": a.error, "payload": dict(a.payload or {})}
