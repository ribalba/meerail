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

from sqlalchemy import text

import dbfixture
from core.database import engine, _retire_disk_columns


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
