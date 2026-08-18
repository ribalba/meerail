"""What the schema fixups in init_db are allowed to throw away.

Needs the database (they are DDL), but nothing else: no server, no agent.

The one pinned here is the columns of the on-disk era. Message bodies used to be
`.eml` files and attachment payloads files beside them, with the paths in
`messages.raw_path` and `attachments.disk_path`; the upgrade to blobs-in-Postgres
dropped both columns in the same breath as adding the ones that replaced them,
and copied nothing. The files stayed on disk with nothing pointing at them, and
an install that had been serving attachments the day before started answering
"not stored" for every one of them. A schema change does not get to be the thing
that loses somebody's mail.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import text

import dbfixture
from core.database import engine, init_db, _retire_disk_columns
from helpers import make_message

T0 = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)


def _column_exists(name: str) -> bool:
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'messages' AND column_name = :c"
        ), {"c": name}).scalar() is not None


def _add_legacy_column() -> None:
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS raw_path VARCHAR(1024)"))


def _set_paths(value) -> None:
    with engine.begin() as conn:
        conn.execute(text("UPDATE messages SET raw_path = :v"), {"v": value})


def test_a_path_column_with_rows_in_it_is_not_dropped(account):
    """Anything still in there is the only record of where that content lives."""
    dbfixture.ingest_raw_message(
        account["email"],
        b"Message-ID: <legacy@t>\r\nSubject: Legacy\r\nFrom: a@b.c\r\n\r\nbody\r\n")
    _add_legacy_column()
    _set_paths("/var/lib/meerail/mail/1/legacy.eml")

    _retire_disk_columns()

    assert _column_exists("raw_path"), "a column holding paths must survive the migration"

    # Emptied — by tools/migrate_blobs.py in a real upgrade — it has nothing left
    # to lose, and the same migration takes it away.
    _set_paths(None)
    _retire_disk_columns()
    assert not _column_exists("raw_path")


def test_the_migration_does_nothing_when_the_column_was_never_there(account):
    """The ordinary case, on every start of every current install."""
    assert not _column_exists("raw_path")
    _retire_disk_columns()
    assert not _column_exists("raw_path")


def _att_status(subject: str) -> str:
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT a.extract_status FROM attachments a "
            "JOIN messages m ON m.id = a.message_pk "
            "WHERE m.subject = :s AND a.filename = 'photo.png'"
        ), {"s": subject}).scalar()


def test_inline_images_already_queued_for_ocr_are_retired(account):
    """The rows the old ingest path left behind, which nothing else clears.

    Extraction OCRs images, and it used to queue the inline ones — every
    signature logo and tracking pixel in the mailbox. An archive import left
    tens of thousands of them pending, and because the queue is global they came
    out during whatever ran next: importing a single message spent a quarter of
    an hour OCRing logos from mail imported an hour earlier. New mail no longer
    queues them (core/mail/store.py::_extractable); this is the backlog.
    """
    email = account["email"]
    inline, attached = (f"Logo {uuid.uuid4().hex[:8]}", f"Photo {uuid.uuid4().hex[:8]}")
    for n, subject in enumerate((inline, attached), start=1):
        dbfixture.ingest_raw_message(email, make_message(
            f"<{uuid.uuid4().hex}@t>", subject, "x@y.com", email, "body", T0,
            png=True), uid=900 + n)

    # What an old row looks like: inline, and queued for OCR anyway.
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE attachments SET is_inline = TRUE, extract_status = 'pending' "
            "WHERE message_pk IN (SELECT id FROM messages WHERE subject = :s)"
        ), {"s": inline})
    assert _att_status(inline) == "pending"

    init_db()

    assert _att_status(inline) == "skipped"
    # And the attached one is untouched: it is listed as an attachment, somebody
    # may well search for what the photo says, and it is one file rather than a
    # mailbox's worth of decoration.
    assert _att_status(attached) == "pending"
