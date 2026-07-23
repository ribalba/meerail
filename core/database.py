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

        for stmt in (
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS "
            "send_addresses JSONB NOT NULL DEFAULT '[]'::jsonb",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS "
            "footer TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS "
            "footer_customized BOOLEAN NOT NULL DEFAULT FALSE",
            # Raw MIME and attachment payloads moved from disk into the DB, so
            # the agent (which writes them) and the app (which serves them)
            # share no filesystem.
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS raw_mime BYTEA",
            "ALTER TABLE messages DROP COLUMN IF EXISTS raw_path",
            "ALTER TABLE attachments ADD COLUMN IF NOT EXISTS content BYTEA",
            "ALTER TABLE attachments DROP COLUMN IF EXISTS disk_path",
            # Precomputed attachment previews. Existing rows default to
            # 'skipped' rather than 'pending' so upgrading does not silently
            # queue a full-mailbox render; see backfill_thumbs.
            "ALTER TABLE attachments ADD COLUMN IF NOT EXISTS thumb BYTEA",
            "ALTER TABLE attachments ADD COLUMN IF NOT EXISTS "
            "thumb_status VARCHAR(16) NOT NULL DEFAULT 'skipped'",
            "CREATE INDEX IF NOT EXISTS ix_attachments_thumb_pending "
            "ON attachments (id) WHERE thumb_status = 'pending'",
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
            # Attachment payloads and WebP previews are already-compressed
            # formats (PDF/JPEG/PNG/zip/WebP). EXTERNAL stores them TOASTed
            # but uncompressed, so ingest stops burning CPU on compression
            # attempts that cannot win. Metadata-only; affects new rows.
            "ALTER TABLE attachments ALTER COLUMN content SET STORAGE EXTERNAL",
            "ALTER TABLE attachments ALTER COLUMN thumb SET STORAGE EXTERNAL",
        ):
            _run_migration(stmt)

        # Give accounts predating the default footer one — but only those the
        # user has never touched, so a deliberately cleared footer stays clear.
        _run_migration(
            "UPDATE accounts SET footer = :footer "
            "WHERE footer = '' AND NOT footer_customized",
            {"footer": DEFAULT_FOOTER},
        )
