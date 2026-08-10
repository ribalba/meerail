"""Read APIs for the mail UI: message list, detail, thread, attachments."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session as DBSession

from core import ingest
from core.database import get_db
from core.mail.parse import html_to_text
from .. import reminders as reminders_core, searchquery
from ..deps import require_ui_auth
from ..mail.render import sanitize_html
from core.models import (
    Account, Attachment, Mailbox, Message, MessageLocation, Recipient, Reminder, Setting,
    still_filed,
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

    The predicate itself is `core.models.still_filed` — it moved there when a
    second reader outside this package needed it (app/threadtext.py, which turns
    a conversation into the text an AI feature sends out) and two copies of "does
    this mail still exist" would have been one too many. This name stays because
    every read path in this module is written in terms of it.
    """
    return still_filed()


def _readable(db: DBSession, message_id: int) -> Message:
    """The message behind an id, if the user has not deleted it. 404 otherwise —
    the same answer as for one that was never here, because for someone who
    emptied their Trash those are the same thing."""
    msg = db.get(Message, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    placed = db.scalar(
        select(MessageLocation.id).where(MessageLocation.message_pk == message_id,
                                         MessageLocation.deleted.is_(False)).limit(1)
    )
    if placed is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return msg


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
    # Bounded at both ends. A ceiling alone leaves the floor open, and a negative
    # limit is not a smaller page: SQLite reads it as "no limit at all" and
    # Postgres refuses the query outright, so the same URL is an uncapped read on
    # one and a 500 on the other. Neither is an answer to a request for -1 rows.
    limit: int = Query(60, ge=1, le=1000),
    offset: int = Query(0, ge=0),
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
    elif scope == "reminders":
        # Mail put off until later. Not a folder — the messages are sitting in
        # Archive, which is where a reminder parks them (app/reminders.py) — so
        # this selects them by the promise rather than by where they are.
        #
        # By conversation where there is one, because that is what a reminder
        # acts on: matching only the message it was set on would show an older
        # reply as the row for a thread whose newest mail is parked beside it.
        j = j.where(or_(
            Message.thread_id.in_(
                select(Reminder.thread_id)
                .where(Reminder.state == "pending", Reminder.thread_id.is_not(None))),
            Message.id.in_(
                select(Reminder.message_pk)
                .where(Reminder.state == "pending", Reminder.thread_id.is_(None))),
        ))
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
    remind = _remind_times(db, rows)

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
                # When this conversation is due back, for the one that is waiting
                # on a reminder. Null for everything else, which is nearly every
                # row — the list draws a small clock on the ones that have it.
                "remind_at": remind.get((r.account_id, r.thread_key)),
            }
            for r in rows
        ],
    }


def _remind_times(db: DBSession, rows) -> dict[tuple[int, str], str]:
    """When each conversation on this page comes back, for the ones put off.

    Two queries for the whole page rather than one per row, and skipped entirely
    when nothing is waiting on a reminder — which is the ordinary state of a
    mailbox, and this must not cost it anything.
    """
    threaded = {(r.account_id, r.thread_key) for r in rows if not r.thread_key.startswith("msg:")}
    loose = {r.id for r in rows if r.thread_key.startswith("msg:")}
    out: dict[tuple[int, str], str] = {}
    for key, when in reminders_core.pending_by_thread(db, threaded).items():
        out[key] = when.isoformat()
    if loose:
        by_message = reminders_core.pending_by_message(db, loose)
        for r in rows:
            when = by_message.get(r.id)
            if when is not None:
                out[(r.account_id, r.thread_key)] = when.isoformat()
    return out


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

    On a split deployment the app has no copy of meerail.toml — the two share
    nothing but the database — so the agent writes the number there each pass
    and this reads it back. Used only to explain a missing body, so an unset or
    unparseable value is not an error: the reader just says less.
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
            # Whether Tika got words out of it. A length test rather than a NULL
            # test: an extraction that ran and found nothing (a scan with no OCR
            # behind it, a spreadsheet of numbers) stores an empty string, and
            # offering to explain that is offering a button that can only say
            # "there is nothing here". Costs a length(), not the text itself —
            # the column is deliberately absent from this select.
            (func.coalesce(func.length(Attachment.extracted_text), 0) > 0).label("has_text"),
        )
        .where(Attachment.message_pk == msg.id, Attachment.is_inline.is_(False))
        .order_by(Attachment.id)
    ).all()
    # Any *live* location's flags (a message may be in several folders; report
    # the union). Deleted placements are excluded for the same reason the list
    # excludes them: they keep the flags they had when the user deleted them, so
    # a mail read in Trash and emptied out of it would otherwise go on marking
    # the copy still sitting in the inbox as read — and one flagged there would
    # keep a star on a conversation nothing is flagging any more. The row
    # outlives the keypress by an agent pass (see _not_deleted).
    locs = db.execute(
        select(MessageLocation).where(MessageLocation.message_pk == msg.id,
                                      MessageLocation.deleted.is_(False))
    ).scalars().all()
    # Whether "view source" has anything to show. Asked as a predicate rather
    # than by touching msg.raw_mime, which is deferred precisely so a thread
    # view never drags every message's original bytes across the wire — the
    # test costs a boolean, reading the attribute would cost the blob.
    has_source = bool(db.execute(
        select(Message.raw_mime.is_not(None)).where(Message.id == msg.id)
    ).scalar())
    return {
        "id": msg.id, "account_id": msg.account_id, "thread_id": msg.thread_id,
        "message_id": msg.message_id, "subject": msg.subject or "(no subject)",
        "from_name": msg.from_name, "from_addr": msg.from_addr,
        "date": msg.date_sent.isoformat() if msg.date_sent else None,
        "recipients": _recipients(db, msg.id),
        "body_html": safe_html, "body_text": msg.body_text,
        # What the reader's "show plain text" switch displays. Most HTML mail
        # here carries no text/plain part at all, and that mail is exactly the
        # mail whose layout misbehaves — so fall back to flattening the HTML
        # rather than withholding the escape hatch. Flattened from the sanitized
        # copy, so nothing nh3 stripped can come back as text. Link addresses
        # are spelled out: dropping them would leave "click here" pointing at
        # nothing, and reading where a link goes is half of why you switch.
        "body_plain": msg.body_text or html_to_text(safe_html, links=True),
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
        "has_source": has_source,
        "seen": any(l.seen for l in locs), "flagged": any(l.flagged for l in locs),
        "answered": any(l.answered for l in locs),
        "locations": [
            {"mailbox_id": l.mailbox_id, "role": db.get(Mailbox, l.mailbox_id).role}
            for l in locs
        ],
        "attachments": [
            {"id": a.id, "filename": a.filename, "content_type": a.content_type,
             "size": a.size_bytes, "is_inline": a.is_inline, "stored": a.stored,
             "has_thumb": a.has_thumb, "viewable": _inline_safe(a.content_type),
             # Is there anything for a model to read? Extracted text, or the
             # picture itself. Decided here because only the server can see the
             # first of the two — see the AI router's "explain this attachment".
             "has_text": a.has_text}
            for a in atts
        ],
    }


@router.get("/messages/{message_id}")
def get_message(message_id: int, images: bool = False, db: DBSession = Depends(get_db)):
    return _message_detail(db, _readable(db, message_id), load_remote=images)


@router.get("/messages/{message_id}/source")
def message_source(message_id: int, db: DBSession = Depends(get_db)):
    """The message exactly as it arrived — headers, MIME structure and all.

    Served as text/plain so a new tab shows it rather than saving it, and under
    the same sandbox/nosniff headers as an attachment: these are the sender's
    bytes, and a browser that decided for itself that they were HTML would be
    running the sender's markup on our own origin.

    Two things stop this having a body: `store_raw_mime` off when the mail was
    ingested, and pruning as it aged out of the content window. Neither is an
    error worth a different code than "there is nothing here to show" — the
    reader already draws the button that leads here disabled in both cases.
    """
    _readable(db, message_id)      # deleted mail has no source to show either
    row = db.execute(
        select(Message.id, Message.raw_mime).where(Message.id == message_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if row.raw_mime is None:
        raise HTTPException(status_code=404, detail="Original message bytes are not stored")
    return Response(
        content=row.raw_mime,
        media_type="text/plain; charset=utf-8",
        headers={
            # Named so that saving the tab lands a file a mail client can open.
            "Content-Disposition": f'inline; filename="message-{message_id}.eml"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )


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

    keyword -> each term the search ANDed together, case-insensitively, as a
    literal substring — a quoted run stays one term, so `"was sent"` marks the
    phrase it matched on. regex -> the pattern itself. Postgres POSIX and Python
    `re` part ways on exotic syntax; a pattern that only one of them accepts
    costs a missing highlight, never a wrong result.

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
        return [re.compile(re.escape(t), re.IGNORECASE) for t in searchquery.keyword_terms(q)]
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
    # POSIX for regex, quoted runs kept whole — rather than by feeding it the
    # Python patterns, whose backslash escaping Postgres reads differently. The
    # filter tokens are already gone: `pats` comes from the same stripped text,
    # and searching attachments for the literal ":unread" would find nothing.
    if mode == "regex":
        where = Attachment.extracted_text.op("~*")(q)
    else:
        where = or_(*[
            Attachment.extracted_text.ilike(f"%{searchquery.like_escape(t)}%", escape="\\")
            for t in searchquery.keyword_terms(q)
        ])
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
            _annotate_attachment_hits(db, msgs, details, searchquery.parse(q).text, mode, pats)
        except DBAPIError:
            # A pattern Postgres rejects costs the attachment highlights, not
            # the thread — the reader still opens.
            db.rollback()
    reminder = reminders_core.pending_for(db, msgs[-1])
    return {
        "thread_id": thread_id,
        "subject": msgs[-1].subject or "(no subject)",
        "messages": details,
        # Set when this conversation is waiting on a reminder, so the reader can
        # say when it is coming back and offer to take that back — the button
        # that put it there is not on screen any more, because the conversation
        # left the folder it was pressed in.
        "reminder": reminders_core.describe(reminder) if reminder else None,
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
    _readable(db, att.message_pk)   # nor do its files outlive the message
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
    _readable(db, att.message_pk)
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
    """One inline part of a message, for a `cid:` the sanitizer rewrote.

    Gated like every other way of reading a message, because it is one: a
    `cid:` is a handle on the bytes of a mail, and mail the user has deleted has
    no bytes to hand out (see _readable).

    Served under the same headers as a downloaded attachment, for the same
    reason: these are the sender's bytes with the sender's Content-Type on them,
    and a browser that sniffed its way to text/html here would be running a
    stranger's markup on our own origin — inside the reader's iframe, where the
    sanitizer's work would then be beside the point.
    """
    _readable(db, message_id)
    att = db.execute(
        select(Attachment).where(
            Attachment.message_pk == message_id, Attachment.content_id == content_id
        )
    ).scalars().first()
    if att is None or att.content is None:
        raise HTTPException(status_code=404, detail="Inline image not found")
    return Response(
        content=att.content,
        media_type=att.content_type or "application/octet-stream",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )
