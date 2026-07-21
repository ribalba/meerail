"""Regex + keyword search over the whole corpus (subject + participants + body +
extracted attachment text), accelerated by the pg_trgm GIN index on search_text.

- mode=regex   -> Postgres ~ / ~* (real POSIX regex). The date-window filter
  bounds patterns that can't use the trigram index (no literal >=3 chars).
- mode=keyword -> AND of case-insensitive substrings.
"""

from __future__ import annotations

import re
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, exists, func, select, tuple_
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session as DBSession

from ..database import get_db
from ..deps import require_ui_auth
from ..models import Account, Message, MessageLocation, utcnow

router = APIRouter(prefix="/api", tags=["search"], dependencies=[Depends(require_ui_auth)])


@router.get("/search")
def search(
    db: DBSession = Depends(get_db),
    q: str = "",
    mode: str = Query("keyword", pattern="^(keyword|regex)$"),
    case_sensitive: bool = False,
    mailbox_id: int | None = None,
    account_id: int | None = None,
    years: int = 0,
    limit: int = Query(60, le=200),
    offset: int = 0,
):
    q = q.strip()
    if not q:
        return {"query": q, "mode": mode, "total": 0, "rows": []}

    if mode == "regex":
        try:
            re.compile(q)
        except re.error as e:
            raise HTTPException(status_code=400, detail=f"Invalid regex: {e}")
        match = Message.search_text.op("~" if case_sensitive else "~*")(q)
    else:
        terms = q.split()
        match = and_(*[Message.search_text.ilike(f"%{t}%") for t in terms])

    base = (
        select(
            Message.id, Message.thread_id, Message.subject, Message.from_name,
            Message.from_addr, Message.date_sent, Message.snippet,
            Message.has_attachments, Message.account_id, Account.color,
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

    try:
        total = db.scalar(select(func.count()).select_from(base.subquery()))
        rows = db.execute(
            base.order_by(Message.date_sent.desc().nulls_last()).limit(limit).offset(offset)
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
