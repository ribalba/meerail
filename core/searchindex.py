"""Filling in ``messages.search_tsv`` for mail that predates it.

New messages get their search index from a trigger on the way in (see
core/database.py), so this exists for exactly one situation: the upgrade that
adds the column to a volume that already holds mail. Every one of those rows
starts NULL, and until the last of them is filled keyword search stays on the
old ILIKE path — a search that ran fast by quietly skipping half the mailbox
would be a worse answer than a slow one.

Done in batches from a background loop rather than in ``init_db``, because the
work is proportional to the mailbox: a 113k-message import is a few minutes of
it, and a server that would not answer until it finished is a server that looks
broken. Search gets faster the moment the last batch lands, not before, and
nothing else notices.
"""

from __future__ import annotations

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .models import Message

# Rows per batch. Each one costs roughly a millisecond of splitting plus its GIN
# insert, so this is a fraction of a second of work — short enough that the
# agent's writes are never waiting on it for long, large enough that a whole
# mailbox does not turn into thousands of round trips.
BATCH = 500


def pending(db: Session) -> int:
    """How many messages still have no search_tsv.

    Cheap despite counting the mailbox: ``ix_messages_search_tsv_missing`` is a
    partial index over exactly these rows, so this reads an index that is empty
    on every volume that has nothing to do and shrinks to empty on the one that
    does. Asked once, to say at startup how much there is to get through.
    """
    return db.scalar(
        select(func.count()).select_from(Message).where(Message.search_tsv.is_(None))
    ) or 0


def build_batch(db: Session, limit: int = BATCH) -> int:
    """Index the next `limit` messages that have no search_tsv. Returns how many.

    Zero means there is nothing left, which on any volume that was created after
    this column existed is the answer from the very first call.
    """
    # The ids first, then the update: `UPDATE ... WHERE search_tsv IS NULL LIMIT`
    # is not a thing, and doing it as one statement over the whole table would
    # take a batch's worth of row locks spread across the mailbox rather than a
    # contiguous few hundred.
    ids = db.execute(
        select(Message.id).where(Message.search_tsv.is_(None)).order_by(Message.id).limit(limit)
    ).scalars().all()
    if not ids:
        return 0

    # Computed in the database, from the column, so the text never crosses the
    # wire — the whole point is not reading it. The trigger is scoped to
    # `UPDATE OF search_text`, so it does not fire here and overwrite this.
    #
    # `updated_at` is assigned to itself to hold it still. It carries onupdate,
    # so leaving it out would restamp every message in the mailbox as changed
    # today — and this is the one write in meerail that touches nothing the
    # message says, only how it is found.
    messages = Message.__table__
    db.execute(
        messages.update()
        .where(messages.c.id.in_(ids))
        .values(
            search_tsv=func.meerail_search_tsv(messages.c.search_text),
            updated_at=messages.c.updated_at,
        )
    )
    db.commit()
    return len(ids)


def analyze(db: Session) -> None:
    """Re-collect the planner's statistics for `messages`.

    Run once, when the backfill has finished. It has just written a value into
    every row of a column the planner had no statistics for at all, and until
    it has some — autoanalyze gets there eventually, on its own schedule — the
    query it is costing is the one thing this whole change exists to make fast.
    """
    db.execute(text("ANALYZE messages"))
    db.commit()
