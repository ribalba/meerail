"""Regex + keyword search over the whole corpus (subject + participants + body +
extracted attachment text), accelerated by the pg_trgm GIN index on search_text.

- mode=regex   -> Postgres ~* (real POSIX regex). The date-window filter
  bounds patterns that can't use the trigram index (no literal >=3 chars).
- mode=keyword -> AND of substrings; "quoted runs" stay one term so spaces
  inside them are matched literally.

Both modes are case-insensitive: mail is not typed consistently enough for
case to be a useful filter, and a miss looks identical to "no such mail".

`:unread`, `:read`, `:has-attachment`, `:from <pattern>` and `:to <pattern>`
are lifted out of the query first (see app.searchquery) and applied as SQL
filters. They narrow rather than search, so a query that is nothing but
filters is still a search — `:unread :has-attachment` is a perfectly good
question to ask.
"""

from __future__ import annotations

import re
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, exists, func, or_, select, tuple_
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session as DBSession

from core.database import get_db
from .. import searchquery
from ..deps import require_ui_auth
from core.models import Account, Message, MessageLocation, Recipient, utcnow

router = APIRouter(prefix="/api", tags=["search"], dependencies=[Depends(require_ui_auth)])

# A double-quoted run, or a bare run of non-space characters.
_TERMS = re.compile(r'"([^"]*)"|(\S+)')


def _like_escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _check_regex(pattern: str, where: str) -> str:
    """Reject a pattern the engine can't compile, naming the filter it came from.

    Python's `re` is not Postgres' POSIX dialect, so this catches the ordinary
    typos (an unclosed group) and leaves the exotic differences to the DBAPIError
    handler further down.
    """
    try:
        re.compile(pattern)
    except re.error as e:
        raise HTTPException(status_code=400, detail=f"Invalid regex in {where}: {e}")
    return pattern


def keyword_terms(q: str) -> list[str]:
    """Split a keyword query into substrings to AND together.

    Quoted runs survive as a single term, so `"how to build"` matches that
    phrase rather than the three words scattered anywhere in the mail. An
    unbalanced trailing quote is treated as an open phrase to end-of-query
    (`"how to bui` while still typing) instead of erroring — search runs on
    every keystroke, so a half-typed quote must not blank the results.
    """
    if q.count('"') % 2:
        q += '"'
    terms = [(a or b) for a, b in _TERMS.findall(q)]
    return [t for t in terms if t.strip()]


@router.get("/search")
def search(
    db: DBSession = Depends(get_db),
    q: str = "",
    mode: str = Query("keyword", pattern="^(keyword|regex)$"),
    mailbox_id: int | None = None,
    account_id: int | None = None,
    years: int = 0,
    limit: int = Query(60, le=200),
    offset: int = 0,
):
    q = q.strip()
    if not q:
        return {"query": q, "mode": mode, "total": 0, "rows": []}

    parsed = searchquery.parse(q)
    empty = {"query": q, "mode": mode, "total": 0, "rows": []}

    clauses = []
    if parsed.text:
        if mode == "regex":
            _check_regex(parsed.text, "the query")
            clauses.append(Message.search_text.op("~*")(parsed.text))
        else:
            terms = keyword_terms(parsed.text)
            if not terms:
                return empty
            # A term is a literal, so % and _ in it are characters the user typed
            # ("50% off"), not LIKE wildcards.
            clauses.extend(
                Message.search_text.ilike(f"%{_like_escape(t)}%", escape="\\") for t in terms
            )
    elif not parsed.filtered:
        # The whole query was a filter still being typed (`:from `).
        return empty

    if parsed.unread is not None:
        # A conversation counts as unread while any copy of the message is
        # unread anywhere it still lives — the same rollup the folder list and
        # the sidebar badges use, so the three never disagree.
        unseen = exists(
            select(MessageLocation.id).where(
                MessageLocation.message_pk == Message.id,
                MessageLocation.deleted.is_(False),
                MessageLocation.seen.is_(False),
            )
        )
        clauses.append(unseen if parsed.unread else ~unseen)
    if parsed.has_attachments:
        clauses.append(Message.has_attachments.is_(True))
    if parsed.from_pat:
        # Address or display name: "who sent this" is a name to the user, and
        # the address is what they reach for when the name is ambiguous.
        pat = _check_regex(parsed.from_pat, ":from")
        clauses.append(or_(Message.from_addr.op("~*")(pat), Message.from_name.op("~*")(pat)))
    if parsed.to_pat:
        pat = _check_regex(parsed.to_pat, ":to")
        clauses.append(
            exists(
                select(Recipient.id).where(
                    Recipient.message_pk == Message.id,
                    Recipient.kind.in_(("to", "cc", "bcc")),
                    or_(Recipient.address.op("~*")(pat), Recipient.name.op("~*")(pat)),
                )
            )
        )

    match = and_(*clauses)

    # Messages that never got threaded stand alone rather than collapsing into
    # one "no thread" pile — same key the folder list builds.
    thread_key = func.coalesce(Message.thread_id, func.concat("msg:", Message.id)).label("thread_key")

    base = (
        select(
            Message.id, Message.thread_id, Message.subject, Message.from_name,
            Message.from_addr, Message.date_sent, Message.snippet,
            Message.has_attachments, Message.account_id, Account.color, thread_key,
        )
        .join(Account, Account.id == Message.account_id)
        .where(match)
    )
    if years > 0:
        base = base.where(Message.date_sent >= utcnow() - timedelta(days=365 * years))
    if mailbox_id is not None:
        base = base.where(
            exists(
                select(MessageLocation.id).where(
                    MessageLocation.message_pk == Message.id,
                    MessageLocation.mailbox_id == mailbox_id,
                    MessageLocation.deleted.is_(False),
                )
            )
        )
    elif account_id is not None:
        base = base.where(Message.account_id == account_id)

    # One row per conversation, not per message: a term that appears in a mail
    # and again in every reply quoting it would otherwise fill the list with the
    # same thread. DISTINCT ON keeps the first row per (account, thread) under
    # this ORDER BY — the newest *matching* message, so the snippet and sender
    # on the row belong to a real hit rather than to an unrelated later reply.
    # Opening it loads the whole thread, with every hit in it marked.
    reps = base.distinct(Message.account_id, thread_key).order_by(
        Message.account_id, thread_key, Message.date_sent.desc().nulls_last(), Message.id.desc()
    ).subquery()

    try:
        # Counts conversations, so the "N results" line agrees with the rows.
        total = db.scalar(select(func.count()).select_from(reps))
        rows = db.execute(
            select(reps).order_by(reps.c.date_sent.desc().nulls_last()).limit(limit).offset(offset)
        ).all()
    except DBAPIError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Search failed — the engine rejected that pattern.")

    ids = [r.id for r in rows]
    flags: dict[int, tuple[bool, bool]] = {}
    if ids:
        for pk, seen_all, flagged_any in db.execute(
            select(MessageLocation.message_pk, func.bool_and(MessageLocation.seen),
                   func.bool_or(MessageLocation.flagged))
            .where(MessageLocation.message_pk.in_(ids))
            .group_by(MessageLocation.message_pk)
        ).all():
            flags[pk] = (bool(seen_all), bool(flagged_any))

    tids = {r.thread_id for r in rows if r.thread_id}
    sizes: dict[tuple[int, str], int] = {}
    if tids:
        account_threads = {(r.account_id, r.thread_id) for r in rows if r.thread_id}
        for aid, tid, n in db.execute(
            select(Message.account_id, Message.thread_id, func.count())
            .where(tuple_(Message.account_id, Message.thread_id).in_(account_threads))
            .group_by(Message.account_id, Message.thread_id)
        ).all():
            sizes[(aid, tid)] = n

    return {
        "query": q, "mode": mode, "total": int(total or 0),
        "rows": [
            {
                "id": r.id, "thread_id": r.thread_id, "subject": r.subject or "(no subject)",
                "from_name": r.from_name, "from_addr": r.from_addr,
                "date": r.date_sent.isoformat() if r.date_sent else None,
                "snippet": r.snippet, "has_attachments": r.has_attachments,
                "seen": flags.get(r.id, (True, False))[0],
                "flagged": flags.get(r.id, (True, False))[1],
                "account_id": r.account_id, "account_color": r.color,
                "thread_count": sizes.get((r.account_id, r.thread_id), 1),
            }
            for r in rows
        ],
    }
