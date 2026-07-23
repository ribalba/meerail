"""Read APIs for the mail UI: message list, detail, thread, attachments."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session as DBSession

from core import ingest
from core.database import get_db
from .. import searchquery
from ..deps import require_ui_auth
from ..mail.render import sanitize_html
from core.models import (
    Account, Attachment, Mailbox, Message, MessageLocation, Recipient, Setting,
)

router = APIRouter(prefix="/api", tags=["messages"], dependencies=[Depends(require_ui_auth)])


def _resolve_mailbox_ids(db: DBSession, mailbox_id: int | None, scope: str | None) -> list[int]:
    if mailbox_id is not None:
        return [mailbox_id]
    if scope == "unified_inbox":
        return list(db.execute(select(Mailbox.id).where(Mailbox.role == "inbox")).scalars().all())
    return []  # flagged/other scopes filter differently (see below)


def _not_deleted():
    """A message still filed somewhere the user hasn't deleted it from.

    The list only ever joins non-deleted locations, so the thread view has to
    apply the same rule or a trashed message reappears inside the conversation.
    """
    return select(MessageLocation.id).where(
        MessageLocation.message_pk == Message.id,
        MessageLocation.deleted.is_(False),
    ).exists()


def _thread_counts(db: DBSession, keys: set[tuple[int, str]]) -> dict[tuple[int, str], int]:
    """How many messages each (account, thread) holds across all folders."""
    if not keys:
        return {}
    rows = db.execute(
        select(Message.account_id, Message.thread_id, func.count())
        .where(tuple_(Message.account_id, Message.thread_id).in_(keys), _not_deleted())
        .group_by(Message.account_id, Message.thread_id)
    ).all()
    return {(account_id, thread_id): n for account_id, thread_id, n in rows}


@router.get("/messages")
def list_messages(
    db: DBSession = Depends(get_db),
    mailbox_id: int | None = None,
    scope: str | None = Query(None, description="unified_inbox | flagged"),
    unread_only: bool = False,
    limit: int = Query(60, le=1000),
    offset: int = 0,
):
    """A date-descending list of *conversations* in a folder/scope.

    One row per thread, not per message: a reply landing in the inbox should
    bump the conversation you already have, not stack a second entry beside it.
    The row shows the newest message in this folder and opening it loads the
    whole thread in the reader.

    Unread/flagged are rolled up across the thread's messages *in this folder*,
    so a conversation reads as unread while any part of it is — which is what
    the badge in the sidebar counts too.
    """
    # Messages that never got threaded stand alone rather than collapsing into
    # one "no thread" pile.
    thread_key = func.coalesce(Message.thread_id, func.concat("msg:", Message.id)).label("thread_key")

    j = select(
        Message.id, Message.thread_id, Message.subject, Message.from_name, Message.from_addr,
        Message.date_sent, Message.snippet, Message.has_attachments, Message.content_status,
        MessageLocation.seen, MessageLocation.flagged, MessageLocation.answered,
        Message.account_id, Account.color, MessageLocation.mailbox_id, Mailbox.role,
        thread_key,
    ).select_from(MessageLocation).join(
        Message, Message.id == MessageLocation.message_pk
    ).join(Mailbox, Mailbox.id == MessageLocation.mailbox_id).join(
        Account, Account.id == Message.account_id
    ).where(MessageLocation.deleted.is_(False))

    if scope == "flagged":
        j = j.where(MessageLocation.flagged.is_(True))
    else:
        ids = _resolve_mailbox_ids(db, mailbox_id, scope)
        if not ids:
            return {"rows": [], "total": 0}
        j = j.where(MessageLocation.mailbox_id.in_(ids))

    if unread_only:
        j = j.where(MessageLocation.seen.is_(False))

    # DISTINCT ON keeps the first row per (account, thread) under this ORDER BY,
    # i.e. the newest message of each conversation. The outer query then sorts
    # those representatives by date, since DISTINCT ON dictates the inner order.
    reps = j.distinct(Message.account_id, thread_key).order_by(
        Message.account_id, thread_key, Message.date_sent.desc().nulls_last(), Message.id.desc()
    ).subquery()

    total = db.scalar(select(func.count()).select_from(reps))
    rows = db.execute(
        select(reps).order_by(reps.c.date_sent.desc().nulls_last()).limit(limit).offset(offset)
    ).all()

    # Unread/flagged roll up over the *folder-filtered* set: a conversation
    # reads as unread while any part of it here is, which is what the sidebar
    # badge counts too.
    keys = {(r.account_id, r.thread_key) for r in rows}
    rollup: dict[tuple[int, str], tuple[bool, bool]] = {}
    if keys:
        grouped = j.with_only_columns(
            Message.account_id, thread_key,
            func.bool_or(MessageLocation.seen.is_(False)),
            func.bool_or(MessageLocation.flagged),
        ).where(tuple_(Message.account_id, thread_key).in_(keys)).group_by(Message.account_id, thread_key)
        for account_id, key, any_unread, any_flagged in db.execute(grouped).all():
            rollup[(account_id, key)] = (bool(any_unread), bool(any_flagged))

    # The count, though, spans folders, because that is what opening the row
    # shows: the reader loads the whole conversation regardless of where its
    # messages live. Counting only this folder made the badge say "2" and the
    # reader then render every message of a 900-strong thread.
    counts = _thread_counts(db, {(a, k) for a, k in keys if not k.startswith("msg:")})

    return {
        "total": int(total or 0),
        "rows": [
            {
                "id": r.id, "thread_id": r.thread_id, "subject": r.subject or "(no subject)",
                "from_name": r.from_name, "from_addr": r.from_addr,
                "date": r.date_sent.isoformat() if r.date_sent else None,
                "snippet": r.snippet, "has_attachments": r.has_attachments,
                # A row with no snippet because there is no body to take one
                # from; the list says so rather than showing a blank line.
                "content_status": r.content_status,
                "seen": not rollup.get((r.account_id, r.thread_key), (not r.seen, r.flagged))[0],
                "flagged": rollup.get((r.account_id, r.thread_key), (not r.seen, r.flagged))[1],
                "answered": r.answered,
                "account_id": r.account_id, "account_color": r.color,
                "mailbox_id": r.mailbox_id, "mailbox_role": r.role,
                "thread_count": counts.get((r.account_id, r.thread_key), 1),
            }
            for r in rows
        ],
    }


def _recipients(db: DBSession, message_pk: int) -> dict[str, list[dict]]:
    rows = db.execute(
        select(Recipient.kind, Recipient.name, Recipient.address)
        .where(Recipient.message_pk == message_pk)
    ).all()
    out: dict[str, list[dict]] = {"to": [], "cc": [], "bcc": [], "reply_to": [], "from": []}
    for kind, name, addr in rows:
        out.setdefault(kind, []).append({"name": name, "address": addr})
    return out


def content_window_months(db: DBSession) -> int:
    """The agent's content window, as it last published it. 0 = keep everything.

    The app cannot read agent/config.toml — the two share nothing but the
    database — so the agent writes the number there each pass and this reads it
    back. Used only to explain a missing body, so an unset or unparseable value
    is not an error: the reader just says less.
    """
    row = db.get(Setting, ingest.CONTENT_WINDOW_KEY)
    try:
        return max(0, int(row.value)) if row else 0
    except (TypeError, ValueError):
        return 0


def _message_detail(db: DBSession, msg: Message, load_remote: bool,
                    window_months: int | None = None) -> dict:
    safe_html, blocked = sanitize_html(msg.body_html, msg.id, load_remote) if msg.body_html else ("", 0)
    # Columns, not entities: selecting Attachment would load `content` — the whole
    # payload — for every attachment just to render a filename chip, and a thread
    # view pays that per message. `thumb IS NOT NULL` is likewise tested in SQL so
    # the preview bytes stay in the database until something actually asks for them.
    atts = db.execute(
        select(
            Attachment.id, Attachment.filename, Attachment.content_type,
            Attachment.size_bytes, Attachment.is_inline,
            Attachment.thumb.is_not(None).label("has_thumb"),
            # Pruning empties the payload but keeps the row, so the reader can
            # still name what was attached — as a chip it will not offer to open.
            Attachment.content.is_not(None).label("stored"),
        )
        .where(Attachment.message_pk == msg.id, Attachment.is_inline.is_(False))
        .order_by(Attachment.id)
    ).all()
    # Any location's flags (a message may be in several folders; report the union).
    locs = db.execute(
        select(MessageLocation).where(MessageLocation.message_pk == msg.id)
    ).scalars().all()
    return {
        "id": msg.id, "account_id": msg.account_id, "thread_id": msg.thread_id,
        "message_id": msg.message_id, "subject": msg.subject or "(no subject)",
        "from_name": msg.from_name, "from_addr": msg.from_addr,
        "date": msg.date_sent.isoformat() if msg.date_sent else None,
        "recipients": _recipients(db, msg.id),
        "body_html": safe_html, "body_text": msg.body_text,
        "remote_blocked": blocked, "images_loaded": load_remote,
        "has_attachments": msg.has_attachments,
        # full | skipped | pruned, plus the window that explains the last two.
        # Looked up only when there is something to explain — normal mail must
        # not pay a settings read per message, and a thread is many messages.
        "content_status": msg.content_status,
        "content_window_months": (
            (content_window_months(db) if window_months is None else window_months)
            if msg.content_status != "full" else 0
        ),
        "seen": any(l.seen for l in locs), "flagged": any(l.flagged for l in locs),
        "answered": any(l.answered for l in locs),
        "locations": [
            {"mailbox_id": l.mailbox_id, "role": db.get(Mailbox, l.mailbox_id).role}
            for l in locs if not l.deleted
        ],
        "attachments": [
            {"id": a.id, "filename": a.filename, "content_type": a.content_type,
             "size": a.size_bytes, "is_inline": a.is_inline, "stored": a.stored,
             "has_thumb": a.has_thumb, "viewable": _inline_safe(a.content_type)}
            for a in atts
        ],
    }


@router.get("/messages/{message_id}")
def get_message(message_id: int, images: bool = False, db: DBSession = Depends(get_db)):
    msg = db.get(Message, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return _message_detail(db, msg, load_remote=images)


# --- Search-term hits inside attachments ---------------------------------
# A search matches Message.search_text, which folds in extracted attachment
# text. So a result can be one where the term appears in a PDF and nowhere in
# the mail itself — highlighting only the body would leave the reader staring
# at a message with no visible reason to be there. The client can mark up
# subject and body on its own; extracted text it has never seen, so the hits
# are found here and shipped as pre-split context windows.

_CONTEXT_CHARS = 90     # either side of the hit
_MAX_HITS = 3           # per attachment — a preview, not a concordance


def match_patterns(q: str, mode: str) -> list[re.Pattern]:
    """The client-visible mirror of the search WHERE clause.

    keyword -> each whitespace-separated term, case-insensitively, as a literal
    substring. regex -> the pattern itself. Postgres POSIX and Python `re` part
    ways on exotic syntax; a pattern that only one of them accepts costs a
    missing highlight, never a wrong result.

    Filter tokens narrowed the result set rather than matching text in it, so
    they are dropped here — otherwise `:unread` would come back highlighted as
    though it were something the user had searched for.
    """
    q = searchquery.parse(q).text
    if not q:
        return []
    try:
        if mode == "regex":
            return [re.compile(q, re.IGNORECASE)]
        return [re.compile(re.escape(t), re.IGNORECASE) for t in q.split()]
    except re.error:
        return []


def _contexts(text: str, pats: list[re.Pattern]) -> list[dict]:
    """Up to _MAX_HITS windows around matches, split so the client can wrap the
    matched span without us handing it HTML to trust."""
    spans: list[tuple[int, int]] = []
    for p in pats:
        for m in p.finditer(text):
            if m.end() > m.start():
                spans.append((m.start(), m.end()))
            if len(spans) >= _MAX_HITS * len(pats):
                break
    spans.sort()
    out, last_end = [], -1
    for start, end in spans:
        if start < last_end:          # overlapping windows read as one blur
            continue
        out.append({
            "before": text[max(0, start - _CONTEXT_CHARS):start].lstrip(),
            "match": text[start:end],
            "after": text[end:end + _CONTEXT_CHARS].rstrip(),
        })
        last_end = end + _CONTEXT_CHARS
        if len(out) >= _MAX_HITS:
            break
    return out


def _annotate_attachment_hits(db: DBSession, msgs, details: list[dict], q: str,
                              mode: str, pats: list[re.Pattern]) -> None:
    # The SQL filter is written the way search.py writes it — ILIKE for keyword,
    # POSIX for regex — rather than by feeding it the Python patterns, whose
    # backslash escaping Postgres reads differently.
    if mode == "regex":
        where = Attachment.extracted_text.op("~*")(q)
    else:
        where = or_(*[Attachment.extracted_text.ilike(f"%{t}%") for t in q.split()])
    # Filtered in SQL first: extracted text runs to whole PDFs, and a thread of
    # them should not cross the wire so Python can throw most of it away.
    rows = db.execute(
        select(Attachment.id, Attachment.extracted_text).where(
            Attachment.message_pk.in_([m.id for m in msgs]),
            Attachment.extracted_text.is_not(None),
            where,
        )
    ).all()
    texts = {aid: t for aid, t in rows}
    if not texts:
        return
    for d in details:
        for a in d["attachments"]:
            hits = _contexts(texts.get(a["id"]) or "", pats)
            if hits:
                a["match_contexts"] = hits


# `:path` because a thread_id is a Message-ID, and plenty of senders build those
# out of a path — GitHub's are `owner/repo/pull/21/c123@github.com`. The client
# percent-encodes the slashes, but the ASGI server unquotes the whole path before
# routing, so a plain `{thread_id}` never matches and every such thread 404s.
@router.get("/threads/{thread_id:path}")
def get_thread(
    thread_id: str,
    account_id: int,
    images: bool = False,
    q: str = "",
    mode: str = Query("keyword", pattern="^(keyword|regex)$"),
    db: DBSession = Depends(get_db),
):
    msgs = db.execute(
        select(Message)
        .where(Message.account_id == account_id, Message.thread_id == thread_id, _not_deleted())
        .order_by(Message.date_sent.asc().nulls_first())
    ).scalars().all()
    if not msgs:
        raise HTTPException(status_code=404, detail="Thread not found")
    # One settings read for the whole thread, not one per message in it.
    window = content_window_months(db) if any(m.content_status != "full" for m in msgs) else 0
    details = [_message_detail(db, m, load_remote=images, window_months=window) for m in msgs]
    pats = match_patterns(q, mode)
    if pats:
        try:
            _annotate_attachment_hits(db, msgs, details, q.strip(), mode, pats)
        except DBAPIError:
            # A pattern Postgres rejects costs the attachment highlights, not
            # the thread — the reader still opens.
            db.rollback()
    return {
        "thread_id": thread_id,
        "subject": msgs[-1].subject or "(no subject)",
        "messages": details,
    }


# Types safe to hand the browser with Content-Disposition: inline. Anything
# scriptable in a same-origin document (text/html, image/svg+xml) is deliberately
# absent: an attachment is attacker-controlled, and rendering one inline on our
# own origin would be stored XSS against the session.
_INLINE_SAFE = {
    "application/pdf",
    "image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp",
}


def _inline_safe(content_type: str) -> bool:
    return (content_type or "").split(";")[0].strip().lower() in _INLINE_SAFE


@router.get("/attachments/{attachment_id}")
def download_attachment(
    attachment_id: int, inline: bool = False, db: DBSession = Depends(get_db)
):
    att = db.get(Attachment, attachment_id)
    if att is None or att.content is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    filename = (att.filename or "attachment").replace('"', "")
    dispo = "inline" if inline and _inline_safe(att.content_type) else "attachment"
    return Response(
        content=att.content,
        media_type=att.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'{dispo}; filename="{filename}"',
            # Belt and braces around the allowlist: never let the browser sniff
            # its way to a different type, and neuter scripts if one slips past.
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )


@router.get("/attachments/{attachment_id}/thumb")
def attachment_thumb(attachment_id: int, db: DBSession = Depends(get_db)):
    att = db.get(Attachment, attachment_id)
    if att is None or att.thumb is None:
        raise HTTPException(status_code=404, detail="No preview")
    return Response(
        content=att.thumb,
        media_type="image/webp",
        headers={
            # Attachment bytes never change, so the preview never does either.
            "Cache-Control": "private, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/messages/{message_id}/cid/{content_id}")
def inline_cid(message_id: int, content_id: str, db: DBSession = Depends(get_db)):
    att = db.execute(
        select(Attachment).where(
            Attachment.message_pk == message_id, Attachment.content_id == content_id
        )
    ).scalars().first()
    if att is None or att.content is None:
        raise HTTPException(status_code=404, detail="Inline image not found")
    return Response(content=att.content,
                    media_type=att.content_type or "application/octet-stream")
