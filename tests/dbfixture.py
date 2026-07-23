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

from core import ingest
from core.database import SessionLocal
from core.models import (
    Account, Attachment, Mailbox, Message, MessageLocation, Outbound, PendingAction,
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


def prune_folders(email: str, present_names: set[str]) -> int:
    with session() as db:
        account = ingest.get_or_create_account(db, email)
        return ingest.prune_mailboxes(db, account, present_names)


def report_sync(email: str, backfill_complete: bool | None = None,
                addresses: list[str] | None = None) -> None:
    with session() as db:
        account = ingest.get_or_create_account(db, email)
        ingest.record_sync(db, account, backfill_complete, addresses)


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
