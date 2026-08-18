"""The one-off that gives mail predating ``messages.search_tsv`` its index.

New messages are indexed by a trigger on the way in, so this code path only
runs on the upgrade that adds the column — which is exactly why it is worth a
test: it is the half nobody exercises again afterwards, on the volume that
matters most, and a silent failure there leaves search permanently on the slow
path with no symptom but the clock.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select

import dbfixture
from core import searchindex
from core.models import Message
from helpers import make_message

T0 = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)


def _one_message(email: str, body: str) -> int:
    mid = f"<bf-{uuid.uuid4().hex}@t>"
    dbfixture.ingest_raw_message(
        email, make_message(mid, "backfill", "x@y.com", email, body, T0), uid=1)
    with dbfixture.session() as db:
        return db.scalar(select(Message.id).where(Message.message_id == mid.strip("<>")))


def test_the_trigger_indexes_a_message_on_the_way_in(account):
    """Nothing to back-fill on a volume this code never predated."""
    pk = _one_message(account["email"], "Stromrechnung im Anhang")
    with dbfixture.session() as db:
        tsv = db.scalar(select(Message.search_tsv).where(Message.id == pk))
    # The compound's tail is a lexeme of its own — that is the suffix expansion,
    # and it is what lets a search for "rechnung" find this message at all.
    assert "'rechnung'" in tsv


def test_a_row_with_no_index_gets_one(account):
    """The upgrade path: a NULL search_tsv is filled in, and only that.

    `updated_at` is the assertion that matters as much as the tsvector. The
    backfill touches every message in the mailbox, and if it restamped them it
    would be rewriting the history of when mail changed in order to build an
    index — a lie about the data, told by the code that is supposed to be
    merely finding it.
    """
    pk = _one_message(account["email"], "Videokonferenz am Montag")

    # Put the row back the way an upgrade finds it.
    with dbfixture.session() as db:
        messages = Message.__table__
        db.execute(messages.update().where(messages.c.id == pk)
                   .values(search_tsv=None, updated_at=messages.c.updated_at))
    with dbfixture.session() as db:
        before = db.scalar(select(Message.updated_at).where(Message.id == pk))
        assert db.scalar(select(Message.search_tsv).where(Message.id == pk)) is None

    with dbfixture.session() as db:
        assert searchindex.build_batch(db) >= 1

    with dbfixture.session() as db:
        row = db.execute(
            select(Message.search_tsv, Message.updated_at).where(Message.id == pk)
        ).one()
    assert row.search_tsv and "'konferenz'" in row.search_tsv
    assert row.updated_at == before


def test_an_indexed_mailbox_asks_for_no_work(account):
    """Zero is the answer the loop rests on, so it has to be reachable."""
    _one_message(account["email"], "nothing to do here")
    with dbfixture.session() as db:
        while searchindex.build_batch(db):
            pass
        assert searchindex.build_batch(db) == 0
