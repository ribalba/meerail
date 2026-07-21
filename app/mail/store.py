"""Ingest pipeline: parsed email -> disk blobs + DB rows.

Content is stored once per (account, dedup_key); each folder placement is a
MessageLocation. Attachment text extraction is deferred (attachments land with
extract_status='pending' and a background worker fills them in, then rebuilds
the message's search_text).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Account, Attachment, Mailbox, Message, MessageLocation, Recipient, utcnow
from .parse import ParsedEmail, html_to_text, parse_email
from .threading import assign_thread
from .tika import should_extract

settings = get_settings()

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str, fallback: str = "attachment") -> str:
    name = (name or "").strip().replace("\x00", "")
    name = _UNSAFE.sub("_", name).strip("._") or fallback
    return name[:180]


def _eml_path(account_id: int, dedup_key: str) -> Path:
    h = hashlib.sha1(dedup_key.encode("utf-8")).hexdigest()
    p = settings.eml_dir / str(account_id) / f"{h}.eml"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _attachment_path(account_id: int, message_pk: int, idx: int, filename: str) -> Path:
    p = settings.attachments_dir / str(account_id) / str(message_pk) / f"{idx}_{_safe_filename(filename)}"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def build_search_text(parsed: ParsedEmail, attachment_texts: list[str] | None = None) -> str:
    parts: list[str] = [parsed.subject, parsed.from_name, parsed.from_addr]
    for kind in ("to", "cc", "bcc", "reply_to"):
        for name, addr in parsed.recipients.get(kind, []):
            parts.extend((name, addr))
    parts.append(parsed.body_text or html_to_text(parsed.body_html))
    if attachment_texts:
        parts.extend(attachment_texts)
    return "\n".join(p for p in parts if p)


def rebuild_search_text(db: Session, message: Message) -> None:
    """Recompute search_text from stored fields + current attachment texts."""
    parts: list[str] = [message.subject, message.from_name, message.from_addr]
    recs = db.execute(
        select(Recipient.name, Recipient.address).where(Recipient.message_pk == message.id)
    ).all()
    for name, addr in recs:
        parts.extend((name or "", addr or ""))
    parts.append(message.body_text or html_to_text(message.body_html))
    att_texts = db.execute(
        select(Attachment.extracted_text).where(
            Attachment.message_pk == message.id, Attachment.extracted_text.is_not(None)
        )
    ).all()
    parts.extend(t[0] for t in att_texts if t[0])
    message.search_text = "\n".join(p for p in parts if p)


def _apply_flags(loc: MessageLocation, flags: dict) -> None:
    loc.seen = bool(flags.get("seen"))
    loc.flagged = bool(flags.get("flagged"))
    loc.answered = bool(flags.get("answered"))
    loc.draft = bool(flags.get("draft"))
    loc.deleted = bool(flags.get("deleted"))
    loc.keywords = flags.get("keywords") or []


def upsert_location(
    db: Session, message_pk: int, mailbox_id: int, uid: int, flags: dict
) -> MessageLocation:
    loc = db.execute(
        select(MessageLocation).where(
            MessageLocation.mailbox_id == mailbox_id, MessageLocation.imap_uid == uid
        )
    ).scalar_one_or_none()
    if loc is None:
        loc = MessageLocation(message_pk=message_pk, mailbox_id=mailbox_id, imap_uid=uid)
        db.add(loc)
    loc.message_pk = message_pk
    _apply_flags(loc, flags)
    return loc


def find_message_by_message_id(db: Session, account_id: int, message_id: str) -> Message | None:
    return db.execute(
        select(Message).where(Message.account_id == account_id, Message.message_id == message_id)
    ).scalars().first()


def ingest_raw(
    db: Session, account: Account, mailbox: Mailbox, uid: int, flags: dict, raw: bytes
) -> tuple[Message, bool]:
    """Parse + store raw bytes. Returns (message, created_new_content)."""
    parsed = parse_email(raw)
    msg = db.execute(
        select(Message).where(
            Message.account_id == account.id, Message.dedup_key == parsed.dedup_key
        )
    ).scalar_one_or_none()

    created = msg is None
    if created:
        needs_extract = any(should_extract(a.content_type, a.filename) and a.payload for a in parsed.attachments)
        msg = Message(
            account_id=account.id,
            message_id=parsed.message_id,
            dedup_key=parsed.dedup_key,
            thread_id=assign_thread(db, account.id, parsed),
            in_reply_to=parsed.in_reply_to,
            references=parsed.references,
            subject=parsed.subject,
            subject_norm=parsed.subject_norm,
            from_name=parsed.from_name,
            from_addr=parsed.from_addr,
            date_sent=parsed.date_sent,
            date_received=utcnow(),
            size_bytes=parsed.size_bytes,
            snippet=parsed.snippet,
            has_attachments=bool(parsed.attachments),
            body_text=parsed.body_text,
            body_html=parsed.body_html,
            search_text=build_search_text(parsed),
            extract_status="pending" if needs_extract else "none",
        )
        db.add(msg)
        db.flush()  # assign msg.id

        eml_path = _eml_path(account.id, parsed.dedup_key)
        eml_path.write_bytes(raw)
        msg.raw_path = str(eml_path)

        for kind, pairs in parsed.recipients.items():
            for name, addr in pairs:
                db.add(Recipient(message_pk=msg.id, kind=kind, name=name, address=addr))

        for idx, att in enumerate(parsed.attachments):
            disk_path = _attachment_path(account.id, msg.id, idx, att.filename)
            disk_path.write_bytes(att.payload)
            extractable = should_extract(att.content_type, att.filename) and bool(att.payload)
            db.add(
                Attachment(
                    message_pk=msg.id,
                    filename=att.filename,
                    content_type=att.content_type,
                    size_bytes=len(att.payload),
                    content_id=att.content_id,
                    is_inline=att.is_inline,
                    disk_path=str(disk_path),
                    extract_status="pending" if extractable else "skipped",
                )
            )

    upsert_location(db, msg.id, mailbox.id, uid, flags)
    return msg, created


def ingest_location_only(
    db: Session, account: Account, mailbox: Mailbox, uid: int, flags: dict, message_id: str
) -> bool:
    """Record a folder placement for content we already have. Returns True if matched."""
    msg = find_message_by_message_id(db, account.id, message_id)
    if msg is None:
        return False
    upsert_location(db, msg.id, mailbox.id, uid, flags)
    return True


def recompute_counts(db: Session, mailbox: Mailbox) -> None:
    # autoflush is off, and this counts via raw SELECTs — flush pending flag/location
    # changes first so callers that just mutated locations get accurate counts.
    db.flush()
    total = db.scalar(
        select(func.count())
        .select_from(MessageLocation)
        .where(MessageLocation.mailbox_id == mailbox.id, MessageLocation.deleted.is_(False))
    )
    unread = db.scalar(
        select(func.count())
        .select_from(MessageLocation)
        .where(
            MessageLocation.mailbox_id == mailbox.id,
            MessageLocation.deleted.is_(False),
            MessageLocation.seen.is_(False),
        )
    )
    mailbox.total_count = int(total or 0)
    mailbox.unread_count = int(unread or 0)
