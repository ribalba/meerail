"""Incremental conversation threading.

Messages arrive one folder/batch at a time and possibly out of order, so we
assign ``thread_id`` incrementally:

1. Look for existing messages this one references (parents) or that reference it
   (children already stored) via Message-ID → adopt/merge their thread_id.
2. Fall back to a normalized-subject match within a recent window.
3. Otherwise start a new thread keyed on this message's own id.

When a new message bridges two previously-separate threads, they are merged
(the lexicographically smaller id wins) so the conversation stays whole.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from .parse import ParsedEmail

SUBJECT_MATCH_WINDOW = timedelta(days=30)


def _merge_threads(db: Session, keep: str, drop: str, account_id: int) -> None:
    if keep == drop:
        return
    from ..models import Message

    db.execute(
        update(Message)
        .where(Message.account_id == account_id, Message.thread_id == drop)
        .values(thread_id=keep)
    )


def assign_thread(db: Session, account_id: int, parsed: ParsedEmail) -> str:
    """Return the thread_id for a message about to be inserted (content is new)."""
    from ..models import Message

    related_ids = set(parsed.references)
    if parsed.in_reply_to:
        related_ids.add(parsed.in_reply_to)

    found_threads: set[str] = set()

    # (1a) Parents/ancestors we reference.
    if related_ids:
        rows = db.execute(
            select(Message.thread_id)
            .where(
                Message.account_id == account_id,
                Message.message_id.in_(related_ids),
                Message.thread_id.is_not(None),
            )
            .distinct()
        ).all()
        found_threads.update(r[0] for r in rows if r[0])

    # (1b) Children already stored that reference this message.
    if parsed.message_id:
        mid = parsed.message_id
        rows = db.execute(
            select(Message.thread_id)
            .where(
                Message.account_id == account_id,
                Message.thread_id.is_not(None),
                or_(
                    Message.in_reply_to == mid,
                    Message.references.contains([mid]),
                ),
            )
            .distinct()
        ).all()
        found_threads.update(r[0] for r in rows if r[0])

    if found_threads:
        keep = min(found_threads)
        for other in found_threads:
            _merge_threads(db, keep, other, account_id)
        return keep

    # (2) Subject-based fallback for chains lacking References/In-Reply-To.
    if parsed.subject_norm and parsed.date_sent:
        lo = parsed.date_sent - SUBJECT_MATCH_WINDOW
        hi = parsed.date_sent + SUBJECT_MATCH_WINDOW
        row = db.execute(
            select(Message.thread_id)
            .where(
                Message.account_id == account_id,
                Message.subject_norm == parsed.subject_norm,
                Message.thread_id.is_not(None),
                Message.date_sent.is_not(None),
                Message.date_sent >= lo,
                Message.date_sent <= hi,
            )
            .order_by(Message.date_sent)
            .limit(1)
        ).first()
        if row and row[0]:
            return row[0]

    # (3) New thread.
    return parsed.message_id or f"thr-{uuid.uuid4()}"
