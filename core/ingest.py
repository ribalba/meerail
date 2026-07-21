"""Mail ingest orchestration — owned by the agent, executed against the DB.

This is the whole write path for incoming mail, in one place: discover folders,
work out which UIDs are new, parse and store them, reconcile flags, prune what
vanished, and extract attachment text via Tika. It used to be split across an
HTTP protocol (agent -> server) plus a background worker; now the agent calls
these functions directly and the web app only reads what they produce.

Every function takes a Session and leaves committing to the caller, so a whole
folder pass can be one transaction.
"""

from __future__ import annotations

from sqlalchemy import select

from . import events
from .mail import tika
from .mail.store import (
    ingest_location_only,
    ingest_raw,
    rebuild_search_text,
    recompute_counts,
)
from .models import Account, Attachment, Mailbox, Message, MessageLocation, utcnow

# Map an IMAP SPECIAL-USE flag / folder name to a meerail mailbox role.
_ROLE_BY_FLAG = {
    "\\sent": "sent",
    "\\drafts": "drafts",
    "\\junk": "junk",
    "\\trash": "trash",
    "\\archive": "archive",
    "\\all": "all",
    "\\flagged": "flagged",
}

EXTRACT_BATCH = 8


def derive_role(imap_name: str, role_hint: str = "") -> str:
    hint = (role_hint or "").strip().lower()
    if hint in _ROLE_BY_FLAG:
        return _ROLE_BY_FLAG[hint]
    if imap_name.upper() == "INBOX":
        return "inbox"
    leaf = imap_name.rsplit("/", 1)[-1].lower()
    return {"sent": "sent", "drafts": "drafts", "draft": "drafts", "trash": "trash",
            "junk": "junk", "spam": "junk", "archive": "archive"}.get(leaf, "custom")


def _leaf(imap_name: str) -> str:
    return imap_name.rsplit("/", 1)[-1]


def get_or_create_account(db, email: str) -> Account:
    """Look up an account by address, registering it on first sight so a newly
    configured agent shows up in the UI without a manual add."""
    normalized = email.strip().lower()
    acc = db.execute(select(Account).where(Account.email == normalized)).scalar_one_or_none()
    if acc is None:
        acc = Account(email=normalized, label=normalized.split("@")[0])
        db.add(acc)
        db.flush()
        events.publish({"type": "accounts", "account": normalized})
    acc.last_agent_seen = utcnow()
    return acc


def register_folder(db, account: Account, imap_name: str, role_hint: str = "",
                    uidvalidity: int | None = None, uidnext: int | None = None,
                    sort_order: int = 0) -> Mailbox:
    """Upsert a mailbox row and return it (carrying the UID cursor)."""
    mb = db.execute(
        select(Mailbox).where(Mailbox.account_id == account.id, Mailbox.imap_name == imap_name)
    ).scalar_one_or_none()
    if mb is None:
        mb = Mailbox(
            account_id=account.id,
            imap_name=imap_name,
            display_name=_leaf(imap_name),
            role=derive_role(imap_name, role_hint),
            sort_order=sort_order,
        )
        db.add(mb)
    else:
        # A UIDVALIDITY change invalidates our UID cursor for this folder.
        if mb.uidvalidity is not None and uidvalidity is not None and mb.uidvalidity != uidvalidity:
            mb.last_uid = 0
        if mb.role == "custom":
            mb.role = derive_role(imap_name, role_hint)
    if uidvalidity is not None:
        mb.uidvalidity = uidvalidity
    if uidnext is not None:
        mb.uidnext = uidnext
    db.flush()
    return mb


def record_known(db, account: Account, mailbox: Mailbox, uid: int, flags: dict,
                 message_id: str | None) -> bool:
    """Record a placement for content we already have. True if it matched, in
    which case the raw bytes need not be fetched (Proton shows one message under
    several labels)."""
    if not message_id:
        return False
    return ingest_location_only(db, account, mailbox, uid, flags, message_id)


def store_message(db, account: Account, mailbox: Mailbox, uid: int, flags: dict,
                  raw: bytes) -> bool:
    """Parse and store raw MIME. Returns True if this created new content."""
    _msg, created = ingest_raw(db, account, mailbox, uid, flags, raw)
    return created


def note_ingested(account: Account, mailbox: Mailbox, stored: int) -> None:
    """Tell the UI new mail landed. Called once per batch, not per message, so a
    large backfill doesn't flood the notification channel."""
    if stored:
        events.publish({"type": "messages", "account": account.email,
                        "folder": mailbox.imap_name, "stored": stored})


def advance_cursor(db, mailbox: Mailbox, last_uid: int) -> None:
    if last_uid > mailbox.last_uid:
        mailbox.last_uid = last_uid
    recompute_counts(db, mailbox)
    events.publish({"type": "cursor", "folder": mailbox.imap_name,
                    "last_uid": mailbox.last_uid, "total": mailbox.total_count,
                    "unread": mailbox.unread_count})


def update_flags(db, mailbox: Mailbox, items: list[dict]) -> int:
    """Apply flag state for already-synced UIDs. items: [{uid, flags}]."""
    updated = 0
    for item in items:
        loc = db.execute(
            select(MessageLocation).where(
                MessageLocation.mailbox_id == mailbox.id,
                MessageLocation.imap_uid == item["uid"],
            )
        ).scalar_one_or_none()
        if loc is None:
            continue
        f = item["flags"]
        loc.seen = bool(f.get("seen"))
        loc.flagged = bool(f.get("flagged"))
        loc.answered = bool(f.get("answered"))
        loc.draft = bool(f.get("draft"))
        loc.deleted = bool(f.get("deleted"))
        loc.keywords = f.get("keywords") or []
        updated += 1
    recompute_counts(db, mailbox)
    if updated:
        events.publish({"type": "flags", "folder": mailbox.imap_name,
                        "updated": updated, "unread": mailbox.unread_count})
    return updated


def prune_vanished(db, mailbox: Mailbox, present_uids: list[int]) -> int:
    """Drop placements whose UID is gone from the folder, and any message left
    with no placement at all."""
    present = set(present_uids)
    locs = db.execute(
        select(MessageLocation).where(MessageLocation.mailbox_id == mailbox.id)
    ).scalars().all()
    affected: set[int] = set()
    removed = 0
    for loc in locs:
        if loc.imap_uid not in present:
            affected.add(loc.message_pk)
            db.delete(loc)
            removed += 1
    db.flush()
    for pk in affected:
        remaining = db.scalar(
            select(MessageLocation.id).where(MessageLocation.message_pk == pk).limit(1)
        )
        if not remaining:
            # Attachments cascade; the raw bytes live on the row itself.
            db.query(Message).filter(Message.id == pk).delete()
    recompute_counts(db, mailbox)
    if removed:
        events.publish({"type": "present", "folder": mailbox.imap_name,
                        "removed": removed, "total": mailbox.total_count})
    return removed


def record_sync(db, account: Account, backfill_complete: bool | None = None,
                addresses: list[str] | None = None) -> None:
    """Update per-account sync status and the agent-declared sender addresses."""
    if backfill_complete is not None:
        account.backfill_complete = backfill_complete
    if addresses is not None:
        seen: set[str] = set()
        ordered: list[str] = []
        for addr in [account.email, *addresses]:
            low = (addr or "").strip().lower()
            if low and low not in seen:
                seen.add(low)
                ordered.append(low)
        extras = ordered[1:]
        if extras != account.send_addresses:
            account.send_addresses = extras
            events.publish({"type": "accounts", "account": account.email})
    account.last_sync_at = utcnow()


def extract_pending(db, limit: int = EXTRACT_BATCH) -> int:
    """Run Tika over a batch of pending attachments and refresh search text.

    Returns how many were processed, so callers can loop until it returns 0.
    """
    pending = db.execute(
        select(Attachment).where(Attachment.extract_status == "pending").limit(limit)
    ).scalars().all()
    if not pending:
        return 0

    touched: set[int] = set()
    for att in pending:
        text = tika.extract_text(att.content or b"", att.content_type)
        att.extracted_text = text or None
        att.extract_status = "done" if text else "error"
        touched.add(att.message_pk)

    # autoflush is off on these sessions, so push extracted_text to the DB before
    # rebuild_search_text re-reads it, or it sees stale NULLs.
    db.flush()

    for message_pk in touched:
        msg = db.get(Message, message_pk)
        if msg is None:
            continue
        rebuild_search_text(db, msg)
        still_pending = db.scalar(
            select(Attachment.id)
            .where(Attachment.message_pk == message_pk, Attachment.extract_status == "pending")
            .limit(1)
        )
        if not still_pending:
            msg.extract_status = "done"
    events.publish({"type": "extract", "processed": len(pending)})
    return len(pending)
