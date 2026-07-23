"""Ingest pipeline: parsed email -> DB rows (including the raw/attachment bytes).

Content is stored once per (account, dedup_key); each folder placement is a
MessageLocation. Raw MIME and attachment payloads live in the database, so the
ingesting process and the serving web app need no shared filesystem.

Attachment text extraction is deferred: attachments land with
extract_status='pending' and the agent's extraction pass fills them in, then
rebuilds the message's search_text.

Content is optional. A message can be stored as headers alone — never fetched
(outside the content window when it was seen) or fetched and later stripped as
the window slid past it. Message.content_status says which; see ingest_raw and
strip_content.
"""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    Account,
    Attachment,
    Mailbox,
    Message,
    MessageLocation,
    PendingAction,
    Recipient,
    utcnow,
)
from .parse import ParsedEmail, canonical_message_id, html_to_text, parse_email
from .threading import assign_thread
from .thumbs import should_thumb
from .tika import should_extract


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


# The flags a new placement can inherit: the two the reader writes back, and so
# the two that can be locally ahead of what the server has been told.
_INHERITED = {"seen": "\\Seen", "flagged": "\\Flagged"}


def _local_state(db: Session, message_pk: int) -> dict:
    """How this message is already flagged wherever else it sits."""
    rows = db.execute(
        select(MessageLocation.seen, MessageLocation.flagged).where(
            MessageLocation.message_pk == message_pk
        )
    ).all()
    return {"seen": any(r.seen for r in rows), "flagged": any(r.flagged for r in rows)}


def _queue_flag_catchup(
    db: Session, message_pk: int, mailbox_id: int, uid: int, ahead: list[str]
) -> None:
    """Tell the agent to bring the server's copy of this placement into step.

    Inheriting locally is only half of it. The reconcile sweep applies server
    flags verbatim, so without this the next pass reads the server's 'unseen'
    back over what was just inherited and the mail goes unread again — on a
    server that keeps flags per folder rather than per message, once a pass,
    forever.
    """
    msg = db.get(Message, message_pk)
    mailbox = db.get(Mailbox, mailbox_id)
    if msg is None or mailbox is None:
        return
    db.add(PendingAction(
        account_id=msg.account_id, message_pk=message_pk, type="setflags",
        payload={"folder": mailbox.imap_name, "uid": uid,
                 "add": [_INHERITED[name] for name in ahead], "remove": []},
    ))


def upsert_location(
    db: Session, message_pk: int, mailbox_id: int, uid: int, flags: dict
) -> MessageLocation:
    loc = db.execute(
        select(MessageLocation).where(
            MessageLocation.mailbox_id == mailbox_id, MessageLocation.imap_uid == uid
        )
    ).scalar_one_or_none()
    if loc is None:
        # A placement showing up for mail the account already holds inherits the
        # read/flag state we have locally. Servers that file one message under
        # several labels hand us each placement separately, and the second one
        # can arrive after the message has been read — the reader can only mark
        # the placements that existed when it ran. Taking the server's flags
        # verbatim there resurrects mail as unread seconds after you read it.
        #
        # Escalate only: a flag the server has and we do not still wins on its
        # own, so this can never quietly un-read something.
        local = _local_state(db, message_pk)
        ahead = [name for name in _INHERITED if local.get(name) and not flags.get(name)]
        if ahead:
            flags = {**flags, **{name: True for name in ahead}}
        loc = MessageLocation(message_pk=message_pk, mailbox_id=mailbox_id, imap_uid=uid)
        db.add(loc)
        if ahead:
            _queue_flag_catchup(db, message_pk, mailbox_id, uid, ahead)
    loc.message_pk = message_pk
    _apply_flags(loc, flags)
    return loc


def find_message_by_message_id(db: Session, account_id: int, message_id: str) -> Message | None:
    message_id = canonical_message_id(message_id)
    if not message_id:
        return None
    return db.execute(
        select(Message).where(Message.account_id == account_id, Message.message_id == message_id)
    ).scalars().first()


def _store_content(db: Session, msg: Message, parsed: ParsedEmail, raw: bytes) -> None:
    """Fill in everything that comes from the body: text, attachments, search."""
    msg.size_bytes = parsed.size_bytes
    msg.snippet = parsed.snippet
    msg.has_attachments = bool(parsed.attachments)
    msg.body_text = parsed.body_text
    msg.body_html = parsed.body_html
    msg.search_text = build_search_text(parsed)
    msg.content_status = "full"
    # size_bytes, the body, the attachments and search_text are all derived
    # above, so the raw copy is purely for future features — and it is the
    # bulk of the database. settings.store_raw_mime (STORE_RAW_MIME /
    # store_raw_mime in the agent config) leaves the column NULL instead.
    msg.raw_mime = raw if get_settings().store_raw_mime else None

    needs_extract = any(
        should_extract(a.content_type, a.filename) and a.payload for a in parsed.attachments
    )
    msg.extract_status = "pending" if needs_extract else "none"

    for att in parsed.attachments:
        extractable = should_extract(att.content_type, att.filename) and bool(att.payload)
        # Inline parts are the signature logos and tracking pixels embedded in
        # the body; they are never listed as attachments, so a preview of one
        # would only ever be rendering nobody looks at.
        thumbable = (
            should_thumb(att.content_type) and bool(att.payload) and not att.is_inline
        )
        db.add(
            Attachment(
                message_pk=msg.id,
                filename=att.filename,
                content_type=att.content_type,
                size_bytes=len(att.payload),
                content_id=att.content_id,
                is_inline=att.is_inline,
                content=att.payload,
                extract_status="pending" if extractable else "skipped",
                thumb_status="pending" if thumbable else "skipped",
            )
        )


def ingest_raw(
    db: Session, account: Account, mailbox: Mailbox, uid: int, flags: dict, raw: bytes,
    headers_only: bool = False, size_bytes: int | None = None,
) -> tuple[Message, bool]:
    """Parse + store raw bytes. Returns (message, created_new_content).

    With ``headers_only``, ``raw`` is just the message's header block — what the
    agent fetches for mail that falls outside the content window. The row that
    lands carries every header (so it lists, threads and shows in search by
    subject and correspondent) with content_status='skipped' and no body,
    attachments or raw MIME. ``size_bytes`` then has to come from the server's
    RFC822.SIZE, since the headers are not the message's size.

    A later full fetch of a message stored that way fills the content in — that
    is what makes widening the window plus a full recheck a way to get old mail
    back, rather than a one-way door.
    """
    parsed = parse_email(raw)
    msg = db.execute(
        select(Message).where(
            Message.account_id == account.id, Message.dedup_key == parsed.dedup_key
        )
    ).scalar_one_or_none()

    created = msg is None
    if created:
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
            # Headers only: the body is not here to be measured, so take the
            # size the server reported. Everything else is header-derived and
            # therefore already correct.
            size_bytes=size_bytes if size_bytes is not None else parsed.size_bytes,
            search_text=build_search_text(parsed),
            content_status="skipped" if headers_only else "full",
        )
        db.add(msg)
        db.flush()  # assign msg.id

        for kind, pairs in parsed.recipients.items():
            for name, addr in pairs:
                db.add(Recipient(message_pk=msg.id, kind=kind, name=name, address=addr))

        if not headers_only:
            _store_content(db, msg, parsed, raw)
    elif not headers_only and msg.content_status == "skipped":
        # We have the whole thing now and only had the headers before: the
        # window was widened and a recheck re-walked this UID. Recipients and
        # the header fields are already right; only content was ever missing.
        _store_content(db, msg, parsed, raw)

    upsert_location(db, msg.id, mailbox.id, uid, flags)
    return msg, created


def strip_content(db: Session, msg: Message) -> None:
    """Walk a stored message back to its headers, as the window slides past it.

    Attachment *rows* stay: the filename, type and size are header-scale data
    the reader still shows (as chips it will not offer to open), and it is the
    payloads, previews and extracted text that are worth the disk. Both queues
    go to 'skipped' so the indexer does not pick the emptied rows back up.
    """
    msg.body_text = ""
    msg.body_html = ""
    msg.snippet = ""
    msg.raw_mime = None
    msg.extract_status = "none"
    msg.content_status = "pruned"
    db.execute(
        update(Attachment)
        .where(Attachment.message_pk == msg.id)
        .values(content=None, thumb=None, extracted_text=None,
                extract_status="skipped", thumb_status="skipped")
    )
    # Before the rebuild, which re-reads the attachment text it just cleared.
    db.flush()
    rebuild_search_text(db, msg)


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
