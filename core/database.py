import re
import time
from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

settings = get_settings()

# check_same_thread=False only matters for SQLite; harmless to compute regardless.
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    future=True,
    pool_pre_ping=True,  # recycle stale connections (long-lived sync + IDLE sessions)
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Shapes of the DDL below, mapped to the catalog query that tells us whether it
# has already been applied. Anything not matched here is simply run.
_COLUMN_ADD = re.compile(r"ALTER TABLE\s+(\w+)\s+ADD COLUMN IF NOT EXISTS\s+(\w+)", re.I)
_COLUMN_DROP = re.compile(r"ALTER TABLE\s+(\w+)\s+DROP COLUMN IF EXISTS\s+(\w+)", re.I)
_INDEX_ADD = re.compile(r"CREATE INDEX IF NOT EXISTS\s+(\w+)", re.I)
_SET_STORAGE = re.compile(r"ALTER TABLE\s+(\w+)\s+ALTER COLUMN\s+(\w+)\s+SET STORAGE\s+(\w+)", re.I)

# pg_attribute.attstorage codes.
_STORAGE_CODES = {"plain": "p", "external": "e", "extended": "x", "main": "m"}

_COLUMN_Q = text(
    "SELECT 1 FROM information_schema.columns "
    "WHERE table_name = :table AND column_name = :column"
)
_INDEX_Q = text("SELECT 1 FROM pg_class WHERE relkind = 'i' AND relname = :name")
_STORAGE_Q = text(
    "SELECT attstorage FROM pg_attribute "
    "WHERE attrelid = to_regclass(:table) AND attname = :column AND NOT attisdropped"
)


def _column_exists(table: str, column: str) -> bool:
    """Whether a column is already there — asked before the ALTER that adds it.

    Only used by fixups that have a *data* half as well as a schema one: the
    ALTER is idempotent on its own, but the UPDATE that follows it is a
    one-time judgement about rows that predate the column, and re-running it on
    every startup would keep overwriting whatever happened since. Best-effort,
    like `_already_applied`: an error reads as "already there", which skips the
    backfill rather than repeating it.
    """
    try:
        with engine.connect() as conn:
            return conn.execute(_COLUMN_Q, {"table": table, "column": column}).scalar() is not None
    except Exception:  # noqa: BLE001 — never block startup over this
        return True


def _already_applied(stmt: str) -> bool:
    """Whether this statement would be a no-op, judged from the system catalog.

    Worth the extra round trip because the IF [NOT] EXISTS forms are *not* free:
    Postgres takes ACCESS EXCLUSIVE on the table before it looks, so a migration
    with nothing to do still has to out-wait every reader. On a populated volume
    that is every statement here, and the agent — which holds attachments open
    across a batch of multi-second Tika calls — reliably wins that race, leaving
    the server unable to start at all.

    Read-only and best-effort: an unrecognised shape, or any error, returns False
    and we just run the statement. The IF [NOT] EXISTS clauses stay for safety.
    """
    if m := _COLUMN_ADD.match(stmt):
        query, args, want = _COLUMN_Q, {"table": m[1], "column": m[2]}, True
    elif m := _COLUMN_DROP.match(stmt):
        query, args, want = _COLUMN_Q, {"table": m[1], "column": m[2]}, False
    elif m := _INDEX_ADD.match(stmt):
        query, args, want = _INDEX_Q, {"name": m[1]}, True
    elif m := _SET_STORAGE.match(stmt):
        code = _STORAGE_CODES.get(m[3].lower())
        if code is None:
            return False
        with engine.connect() as conn:
            return conn.execute(_STORAGE_Q, {"table": m[1], "column": m[2]}).scalar() == code
    else:
        return False

    with engine.connect() as conn:
        return (conn.execute(query, args).scalar() is not None) is want


def _run_migration(stmt: str, params: dict | None = None, attempts: int = 6) -> None:
    """Run one schema fixup in its own transaction, retrying if the agent is busy.

    One statement per transaction is what makes this deadlock-free: an ALTER only
    ever holds the single table lock it needs, so it cannot form a lock cycle with
    a concurrent agent query. Batching them meant init_db accumulated exclusive
    locks on accounts/messages/attachments and then blocked on mailboxes, while the
    agent held mailboxes and waited on accounts — and Postgres shot the migration.

    lock_timeout keeps a long-running agent query from stalling startup forever;
    we back off and retry instead, since the agent's transactions are short.
    """
    try:
        if _already_applied(stmt):
            return
    except Exception:  # noqa: BLE001 — the statement itself is the source of truth
        pass

    for attempt in range(attempts):
        try:
            with engine.begin() as conn:
                conn.execute(text("SET LOCAL lock_timeout = '5s'"))
                conn.execute(text(stmt), params or {})
            return
        except OperationalError:
            if attempt == attempts - 1:
                raise
            # Say so: a busy agent can push this to ~45s per statement, and a
            # silent retry loop looks identical to a hung "application startup".
            print(f"[init_db] lock contention, retry {attempt + 1}/{attempts - 1}: {stmt[:60]}")
            time.sleep(1 + attempt)  # 1s, 2s, 3s… ~15s total before giving up


def init_db() -> None:
    # Register models on Base before create_all.
    from . import models  # noqa: F401

    # pg_trgm must exist BEFORE create_all builds the GIN trigram index on
    # messages.search_text, so create the extension first.
    if settings.database_url.startswith("postgresql"):
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))

    # Greenfield schema: models define everything, create_all makes it in one shot.
    # (No incremental migrations pre-1.0 — recreate the volume on schema changes.)
    Base.metadata.create_all(bind=engine)

    # Idempotent column fixups so an existing volume upgrades in place instead of
    # needing a wipe. create_all never alters existing tables.
    if settings.database_url.startswith("postgresql"):
        from .models import DEFAULT_FOOTER

        # Asked before the ALTER below adds it: on an existing volume the answer
        # is "no", which is the one run that gets to guess which accounts were
        # imported rather than synced. See the backfill after the loop.
        had_local = _column_exists("accounts", "local")

        for stmt in (
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS "
            "send_addresses JSONB NOT NULL DEFAULT '[]'::jsonb",
            # Display names for those addresses. Empty on an existing volume
            # until the agent's next pass reports them, which sends exactly what
            # was sent before: the bare address.
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS "
            "send_names JSONB NOT NULL DEFAULT '{}'::jsonb",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS "
            "footer TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS "
            "footer_customized BOOLEAN NOT NULL DEFAULT FALSE",
            # Presentation fields pinned in the agent's meerail.toml. Empty on an
            # existing volume until the agent's next pass says otherwise, which
            # is exactly the behaviour of an install that pins nothing.
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS "
            "config_fields JSONB NOT NULL DEFAULT '[]'::jsonb",
            # Raw MIME and attachment payloads moved from disk into the DB, so
            # the agent (which writes them) and the app (which serves them)
            # share no filesystem. The columns that used to hold the *paths* are
            # not dropped here — see _retire_disk_columns, below.
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS raw_mime BYTEA",
            "ALTER TABLE attachments ADD COLUMN IF NOT EXISTS content BYTEA",
            # Precomputed attachment previews. Existing rows default to
            # 'skipped' rather than 'pending' so upgrading does not silently
            # queue a full-mailbox render; see backfill_thumbs.
            "ALTER TABLE attachments ADD COLUMN IF NOT EXISTS thumb BYTEA",
            "ALTER TABLE attachments ADD COLUMN IF NOT EXISTS "
            "thumb_status VARCHAR(16) NOT NULL DEFAULT 'skipped'",
            "CREATE INDEX IF NOT EXISTS ix_attachments_thumb_pending "
            "ON attachments (id) WHERE thumb_status = 'pending'",
            # What a message's bytes hash to, which is what tells two messages
            # sharing a Message-ID apart. NULL on every existing row, and read as
            # "cannot say" — those fall back to comparing headers until the next
            # time the message is stored in full.
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS content_hash VARCHAR(80)",
            # Content window (agent: content_window_months). Existing rows are
            # 'full' — anything already stored was stored in full, and the
            # agent's prune pass is what walks them back if a window is set.
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS "
            "content_status VARCHAR(16) NOT NULL DEFAULT 'full'",
            # Partial: the prune pass asks "what is still full and now too old",
            # which over a whole mailbox is a seq scan every time it runs, and it
            # runs on a timer. The index only covers rows that can still match,
            # so it shrinks as the window walks forward.
            "CREATE INDEX IF NOT EXISTS ix_messages_prunable "
            "ON messages (date_sent) WHERE content_status = 'full'",
            # Agent health, surfaced in the UI's agent-status modal.
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS last_error TEXT",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS last_error_at TIMESTAMP",
            # Full-recheck request, raised in the UI and cleared by the agent.
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS "
            "recheck_requested BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS "
            "recheck_requested_at TIMESTAMP",
            # Agent progress through the current pass, for the status panel.
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS sync_progress JSONB",
            # User-pinned sidebar folders.
            "ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS "
            "favorite BOOLEAN NOT NULL DEFAULT FALSE",
            # When the server stopped listing a folder. NULL on every existing
            # row, which is what "the server is still listing it" looks like —
            # the first pass after an upgrade fills it in for anything that has
            # genuinely gone. See core/ingest.py::prune_mailboxes.
            "ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS missing_since TIMESTAMP",
            # When the server refused mail into a folder. NULL everywhere until
            # an agent actually hits one, which is the state every existing row
            # is correctly in. See agent/actions.py::_mark_write_refused.
            "ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS writes_refused_at TIMESTAMP",
            # Backs the ingest-rate counters. Brief write lock on first run;
            # seconds on a personal mailbox, which is the target here.
            "CREATE INDEX IF NOT EXISTS ix_messages_account_created "
            "ON messages (account_id, created_at)",
            # --- Analytics (app/routers/analytics.py) ---
            # Sender rollups (top correspondents, volume by domain) group the
            # whole window by from_addr, which was a seq scan plus an external
            # sort on every open of the stats modal.
            "CREATE INDEX IF NOT EXISTS ix_messages_account_from "
            "ON messages (account_id, from_addr)",
            # Reply latency correlates a second pass over messages looking for
            # "earliest message in this thread after this one". ix_messages_thread
            # gets it to the thread; without the date it then filters every
            # member of that thread, once per message in the window.
            "CREATE INDEX IF NOT EXISTS ix_messages_thread_date "
            "ON messages (account_id, thread_id, date_sent)",
            # The outbound half of the correspondents panel joins recipients and
            # keeps only to/cc, so the kind belongs in the index rather than as a
            # filter over every address row of a message.
            "CREATE INDEX IF NOT EXISTS ix_recipients_message_kind "
            "ON recipients (message_pk, kind)",
            # When a message was first observed to become read — see
            # models.Message.read_at. NULL for everything already in the volume,
            # and for everything that was read before this column existed.
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS read_at TIMESTAMP",
            # Partial, because read_at is NULL for the great majority of rows on
            # any mailbox with history: the "when mail is read" panel scans only
            # the ones that are set, and the index is a fraction of the size a
            # full one would be.
            "CREATE INDEX IF NOT EXISTS ix_messages_account_read "
            "ON messages (account_id, read_at) WHERE read_at IS NOT NULL",
            # Which install is bringing a reminder back, on the installs that
            # share a journal. NULL everywhere on an existing volume, which is
            # exactly the state an install with no journal stays in.
            "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS claim_seq BIGINT",
            "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS claim_by VARCHAR(64)",
            "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS claim_at TIMESTAMP",
            # Attachment payloads and WebP previews are already-compressed
            # formats (PDF/JPEG/PNG/zip/WebP). EXTERNAL stores them TOASTed
            # but uncompressed, so ingest stops burning CPU on compression
            # attempts that cannot win. Metadata-only; affects new rows.
            "ALTER TABLE attachments ALTER COLUMN content SET STORAGE EXTERNAL",
            "ALTER TABLE attachments ALTER COLUMN thumb SET STORAGE EXTERNAL",
            # An account no agent syncs — imported mail — and a folder that
            # exists only here. FALSE for everything already in the volume,
            # which is right for every account an agent has ever run for; the
            # backfill below is what finds the ones it is wrong for.
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS "
            "local BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS "
            "local BOOLEAN NOT NULL DEFAULT FALSE",
            # What the mail server lets a folder be called. Unknown on an
            # existing volume until the agent's next pass reports it, which
            # reads as "flat names only" — exactly what the app did before
            # these columns existed.
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS "
            "folder_delimiter VARCHAR(8) NOT NULL DEFAULT ''",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS "
            "folder_nesting BOOLEAN NOT NULL DEFAULT FALSE",
        ):
            _run_migration(stmt)

        if not had_local:
            # One-time, on the upgrade that introduces the column. An account no
            # agent has ever stamped is one nothing is syncing — an mbox import,
            # in practice, since that is the only other way a row gets here — so
            # meerail owns its folders and can create and move within them.
            #
            # A guess, and deliberately one that corrects itself: an agent that
            # does turn up clears the flag on its first pass
            # (ingest.get_or_create_account). What must not be got wrong in the
            # meantime is the mail, which is why folders made under the guess
            # carry mailboxes.local and prune_mailboxes leaves those alone.
            _run_migration("UPDATE accounts SET local = TRUE WHERE last_agent_seen IS NULL")
            _run_migration(
                "UPDATE mailboxes SET local = TRUE WHERE account_id IN "
                "(SELECT id FROM accounts WHERE local)"
            )
            # Instructions for an agent that does not exist. They were queued by
            # the UI before it knew the difference, and every one of them
            # addresses a UID this install invented for mail that came off disk
            # — so they are not merely undrainable, they are dangerous to drain:
            # an agent configured for the address later would EXPUNGE by a number
            # that names somebody else's message on the real server. Sends are
            # left alone: those are mail waiting to go out, and nothing about
            # having no agent makes them wrong.
            _run_migration(
                "DELETE FROM pending_actions WHERE type <> 'send' AND account_id IN "
                "(SELECT id FROM accounts WHERE local)"
            )

        # Give accounts predating the default footer one — but only those the
        # user has never touched, so a deliberately cleared footer stays clear.
        _run_migration(
            "UPDATE accounts SET footer = :footer "
            "WHERE footer = '' AND NOT footer_customized",
            {"footer": DEFAULT_FOOTER},
        )

        _retire_disk_columns()


# The columns of the on-disk era: message bodies were .eml files and attachment
# payloads were files beside them, and these held the paths.
_DISK_COLUMNS = (
    ("messages", "raw_path", "raw_mime"),
    ("attachments", "disk_path", "content"),
)


def _retire_disk_columns() -> None:
    """Drop the old path columns — but only once nothing is left in them.

    They used to be dropped outright, in the same breath as adding the blob
    columns that replaced them, and nothing ever copied the files across. On an
    install from that era the upgrade therefore threw away the only pointer to
    every stored attachment and every raw message: the files stayed on disk,
    unreferenced and unreachable, and the reader started answering "not stored"
    for mail it had been serving the day before. A schema change is not allowed
    to be the thing that loses somebody's mail.

    So the drop is conditional. An empty column is a column nothing needs and it
    goes; a column with rows in it is left exactly where it is, and startup says
    what to run. tools/migrate_blobs.py does the copying, on the machine that can
    actually see those paths — which on a split deployment is not this one.
    """
    for table, column, replacement in _DISK_COLUMNS:
        try:
            with engine.connect() as conn:
                exists = conn.execute(_COLUMN_Q, {"table": table, "column": column}).scalar()
                if not exists:
                    continue
                left = conn.execute(text(
                    f"SELECT count(*) FROM {table} WHERE {column} IS NOT NULL"
                )).scalar() or 0
        except Exception as exc:  # noqa: BLE001 — never block startup over this
            print(f"[init_db] could not check {table}.{column}: {exc!r}")
            continue

        if not left:
            _run_migration(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
            continue

        print(f"[init_db] {left} row(s) in {table} still point at files on disk "
              f"({table}.{column}). Their content has NOT been copied into "
              f"{table}.{replacement}, and this column will not be dropped while it "
              f"holds anything. Run tools/migrate_blobs.py on the machine holding "
              f"those files to bring them in.")
