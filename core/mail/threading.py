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

import hashlib
import uuid
from datetime import timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from .parse import ParsedEmail, looks_like_reply

SUBJECT_MATCH_WINDOW = timedelta(days=30)


def _new_thread_id(message_id: str | None) -> str:
    if not message_id:
        return f"thr-{uuid.uuid4()}"
    if len(message_id) <= 255:
        return message_id
    return "mid-sha256:" + hashlib.sha256(message_id.encode()).hexdigest()


def _merge_threads(db: Session, keep: str, drop: str, account_id: int) -> None:
    if keep == drop:
        return
    from ..models import Message

    db.execute(
        update(Message)
        .where(Message.account_id == account_id, Message.thread_id == drop)
        .values(thread_id=keep)
    )


def assign_thread(db: Session, account_id: int, parsed: ParsedEmail,
                  shared_id: bool = False) -> str:
    """Return the thread_id for a message about to be inserted (content is new).

    ``shared_id`` says this message's Message-ID is already worn by a different
    message in this account — the collision case core/mail/store.py files under
    the bytes instead. The id is then not a threading key at all: it cannot start
    a conversation, because the other message has already started one under that
    exact name and a thread action would move both, and it cannot be used to
    collect replies, because a reply naming it could be answering either.

    So this message threads on everything else it has — the parents it names, the
    subject window — and failing those it starts a conversation of its own, keyed
    by its content, which is the one name it does not share.
    """
    from ..models import Message

    related_ids = set(parsed.references)
    if parsed.in_reply_to:
        related_ids.add(parsed.in_reply_to)

    found_threads: set[str] = set()

    # (1a) Parents/ancestors we reference.
    #
    # Per id, not in one heap, because an id that resolves to more than one
    # conversation resolves to nothing. Two different messages can wear one
    # Message-ID; a reply naming it is answering one of them, and taking both
    # would not just guess — every thread_id found here is *merged* below, so a
    # single reply would join the collision's conversation to the unrelated one
    # it collided with, and one archive keypress would then file both. An
    # ambiguous parent is therefore no parent: the reply threads on whatever
    # else it names, or starts a conversation of its own.
    if related_ids:
        rows = db.execute(
            select(Message.message_id, Message.thread_id)
            .where(
                Message.account_id == account_id,
                Message.message_id.in_(related_ids),
                Message.thread_id.is_not(None),
            )
            .distinct()
        ).all()
        by_id: dict[str, set[str]] = {}
        for parent_id, thread_id in rows:
            if thread_id:
                by_id.setdefault(parent_id, set()).add(thread_id)
        for threads in by_id.values():
            if len(threads) == 1:
                found_threads.update(threads)

    # (1b) Children already stored that reference this message. Skipped when the
    # id is shared: a reply naming it is answering one of the two messages
    # wearing it, and nothing here says which.
    if parsed.message_id and not shared_id:
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
    #
    # Only a message that *presents itself as a reply* may join an existing
    # thread this way. Without that gate, machine-generated mail — monitoring
    # alerts, cron reports, CI notifications — merges into one pile: each has a
    # fresh Message-ID and no References, so it lands here, and identical
    # subjects make every one of them look like the same conversation.
    if parsed.subject_norm and parsed.date_sent and looks_like_reply(parsed.subject):
        # Anchor the window on the candidate thread's *root*, not on whichever
        # member happens to be nearest. Matching any member lets the window
        # slide: day 40 matches day 10, which already joined day 1, and a
        # recurring subject grows one unbounded thread spanning years.
        root = (
            select(Message.thread_id, func.min(Message.date_sent).label("root_date"))
            .where(
                Message.account_id == account_id,
                Message.subject_norm == parsed.subject_norm,
                Message.thread_id.is_not(None),
                Message.date_sent.is_not(None),
            )
            .group_by(Message.thread_id)
            .subquery()
        )
        row = db.execute(
            select(root.c.thread_id)
            .where(
                root.c.root_date >= parsed.date_sent - SUBJECT_MATCH_WINDOW,
                root.c.root_date <= parsed.date_sent + SUBJECT_MATCH_WINDOW,
            )
            .order_by(root.c.root_date)
            .limit(1)
        ).first()
        if row and row[0]:
            return row[0]

    # (3) New thread — under this message's own name, which for a shared
    # Message-ID is its content rather than the id two messages are using.
    return _new_thread_id(parsed.content_key if shared_id else parsed.message_id)
