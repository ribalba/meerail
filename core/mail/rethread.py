"""Recompute ``thread_id`` for messages already in the database.

Threading is assigned once, at ingest, from the state of the mailbox at that
moment — so a change to the rules (or a bug in them) leaves the stored
conversations wrong until they are rebuilt. That is what this does: it drops
the existing ids for an account and replays :func:`assign_thread` over every
message in send order, which is the order live ingest would mostly have seen
them in.

Run against one account::

    python -m core.mail.rethread --account 3

or all of them, with ``--dry-run`` first to see what would change::

    python -m core.mail.rethread --all --dry-run
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import Account, Message
from .threading import assign_thread


@dataclass
class _Stored:
    """The subset of ``ParsedEmail`` that :func:`assign_thread` reads.

    Rebuilding from columns rather than re-parsing raw bytes keeps this cheap
    on large accounts; every field threading needs is already stored.
    """

    message_id: str | None
    in_reply_to: str | None
    references: list[str]
    subject: str
    subject_norm: str
    date_sent: datetime | None


def rethread_account(db: Session, account_id: int) -> tuple[int, int]:
    """Reassign every thread_id for one account. Returns (messages, changed).

    Leaves the transaction open — the caller commits or rolls back.
    """
    before = dict(
        db.execute(
            select(Message.id, Message.thread_id).where(Message.account_id == account_id)
        ).all()
    )
    # Clear first: assign_thread only ever adopts threads that already exist, so
    # a half-cleared table would let stale ids seed the rebuild.
    db.execute(
        update(Message).where(Message.account_id == account_id).values(thread_id=None)
    )
    db.flush()

    rows = db.execute(
        select(Message)
        .where(Message.account_id == account_id)
        .order_by(Message.date_sent.asc().nulls_first(), Message.id.asc())
    ).scalars().all()

    for m in rows:
        m.thread_id = assign_thread(
            db,
            account_id,
            _Stored(
                message_id=m.message_id,
                in_reply_to=m.in_reply_to,
                references=list(m.references or []),
                subject=m.subject or "",
                subject_norm=m.subject_norm or "",
                date_sent=m.date_sent,
            ),
        )
        # assign_thread's merge step rewrites sibling rows in the DB directly,
        # so the identity map has to see those before the next message queries.
        db.flush()

    after = dict(
        db.execute(
            select(Message.id, Message.thread_id).where(Message.account_id == account_id)
        ).all()
    )
    changed = sum(1 for mid, tid in after.items() if before.get(mid) != tid)
    return len(rows), changed


def _largest_threads(db: Session, account_id: int, n: int = 5) -> list[tuple[str, int]]:
    return db.execute(
        select(Message.thread_id, func.count())
        .where(Message.account_id == account_id, Message.thread_id.is_not(None))
        .group_by(Message.thread_id)
        .order_by(func.count().desc())
        .limit(n)
    ).all()


def main() -> None:
    ap = argparse.ArgumentParser(description="Rebuild conversation threading.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--account", type=int, help="account id to rethread")
    g.add_argument("--all", action="store_true", help="rethread every account")
    ap.add_argument("--dry-run", action="store_true", help="report, then roll back")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        ids = (
            list(db.execute(select(Account.id).order_by(Account.id)).scalars().all())
            if args.all
            else [args.account]
        )
        for account_id in ids:
            print(f"account {account_id}: before, largest threads {_largest_threads(db, account_id)}")
            total, changed = rethread_account(db, account_id)
            print(f"account {account_id}: {total} messages, {changed} reassigned")
            print(f"account {account_id}: after,  largest threads {_largest_threads(db, account_id)}")
        if args.dry_run:
            db.rollback()
            print("dry run — nothing committed")
        else:
            db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    main()
