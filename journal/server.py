"""The journal server: an ordered log of blobs it cannot read.

This is the smallest service that can keep three meerail installs in agreement,
and it is small on purpose. It does four things — take a record, number it, hand
back records after a number, and forget the ones nobody needs any more — and it
understands none of what it holds. There is no reminder logic here, no notion of
an account, no schema for a record body. Adding a second kind of synced state
(footers, account names, whatever comes next) is a change to app/journal.py and
no change at all to this file, which is the property worth protecting: this is
the piece that runs on a rented machine, so it should be the piece that almost
never has to be redeployed.

**What it knows.** A space (the hash of a client's token), a sequence number, a
blob, a timestamp, and whether the client called the blob a snapshot. It cannot
tell a reminder from a footer, and it cannot read an address or a subject.

**What it is trusted with.** Ordering, and availability. A hostile server can
withhold records, replay old ones, or lie about what came after 42 — it cannot
forge one, because it has no key to seal with, and Fernet's authentication is
what makes a modified blob unopenable rather than merely wrong. So the failure
mode of a bad host is "these machines stop agreeing", not "these machines agree
on something the host made up".

**Why the sequence number is the ordering and the clocks are not.** Three
laptops' clocks disagree by seconds at best, and the two records that matter
most — two installs both claiming the same due reminder — are written within
milliseconds of each other. Whoever's INSERT commits first gets the lower
number, and every client reaches the same verdict from it.

Deployment is in journal/README.md; the short version is one container, one
volume, and a passphrase you paste into all three meerail installs.
"""

from __future__ import annotations

import hashlib
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import (
    BigInteger, Boolean, DateTime, Index, Integer, String, Text, create_engine, delete, func,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

# --- Configuration ---------------------------------------------------------
#
# Environment only. There is no config file because there is nothing worth
# putting in one: a database URL, a list of token hashes, and how long to keep
# records. A service this small should be configurable entirely from a compose
# file.

DATABASE_URL = os.environ.get("JOURNAL_DATABASE_URL") or "sqlite:///./journal.db"

# Which clients may speak to this server, as the sha256 of each one's derived
# token — printed by `python -m journal.keys <passphrase>`. Hashes rather than
# tokens so that reading this server's environment (or its database) does not
# hand anybody a working credential.
#
# A list rather than one value so that a single server can hold several
# unrelated journals — a household with two people syncing separately, a test
# space beside a real one. They share nothing: a space is only ever readable by
# the token that names it.
SPACES = {s.strip() for s in (os.environ.get("JOURNAL_SPACES") or "").split(",") if s.strip()}

# How long a record is kept once it is provably redundant. The clock alone is
# not enough to justify deleting anything — see _prune, which will not drop a
# record that no snapshot has yet superseded, however old it is. A laptop that
# was shut in a drawer for a month has to be able to catch up.
RETAIN_DAYS = int(os.environ.get("JOURNAL_RETAIN_DAYS") or 90)

# Ceilings, so that one client cannot fill the disk in a loop. A sealed reminder
# is a few hundred bytes and a snapshot of a busy account is a few tens of
# kilobytes; 256 KB is far above both and far below anything that hurts.
MAX_BLOB_BYTES = int(os.environ.get("JOURNAL_MAX_BLOB_BYTES") or 262144)
MAX_BATCH = int(os.environ.get("JOURNAL_MAX_BATCH") or 200)
MAX_PAGE = int(os.environ.get("JOURNAL_MAX_PAGE") or 500)


class Base(DeclarativeBase):
    pass


class Record(Base):
    """One sealed record, and the number that orders it.

    ``seq`` is global rather than per-space, which costs nothing (a client only
    ever sees its own space's numbers, and they are still increasing) and avoids
    a per-space counter that two concurrent appends would have to contend on.

    ``snapshot`` is the one thing a client tells this server in the clear, and it
    is the price of ever being able to delete anything: the server cannot read a
    record, so it cannot know that record 900 restates everything up to 899. The
    client says so. What leaks is the shape of the traffic — that at some point a
    client wrote a summary — which is already visible from the size and timing of
    the requests.
    """

    __tablename__ = "records"
    __table_args__ = (Index("ix_records_space_seq", "space", "seq"),)

    # The variant is not a detail: SQLite only auto-assigns a primary key when
    # the column is declared INTEGER (it is then an alias for the rowid), and a
    # BIGINT column just gets a NOT NULL violation on every insert. SQLite's
    # rowid is 64-bit regardless, so nothing is given up by asking for INTEGER
    # there and BIGINT on Postgres.
    seq: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    space: Mapped[str] = mapped_column(String(64), nullable=False)
    blob: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_space(authorization: str = Header(default="")) -> str:
    """Turn a bearer token into the space it may read and write, or refuse.

    The token is hashed and looked up; the token itself is never stored, and the
    comparison is over the hash so an equal-length constant-time check is the
    natural one to write. A token that names no configured space is a 401 with
    nothing else said — which space names exist is not a stranger's business.
    """
    if not SPACES:
        raise HTTPException(
            status_code=503,
            detail="This journal server has no spaces configured (JOURNAL_SPACES).")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Bearer token required")
    space = hashlib.sha256(token.strip().encode()).hexdigest()
    # `in` over a set of fixed-length hex digests: the secret was already run
    # through sha256, so there is no length or prefix to leak here.
    if space not in SPACES:
        raise HTTPException(status_code=401, detail="Unknown token")
    return space


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # One table, created on the way up. There is no migration machinery here and
    # there should not be: the schema is a sequence number and a blob, and if it
    # ever needs to change, the honest move is a new table rather than an ALTER
    # against a database somebody else is hosting.
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="meerail journal", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)


class RecordIn(BaseModel):
    blob: str = Field(max_length=MAX_BLOB_BYTES)
    # The client's assertion that this record restates everything before it for
    # whatever it covers. Only ever read by _prune.
    snapshot: bool = False


class AppendIn(BaseModel):
    records: list[RecordIn] = Field(min_length=1, max_length=MAX_BATCH)


@app.get("/healthz")
def healthz(db: Session = Depends(get_db)) -> dict:
    """Reachability, and nothing about anybody's data.

    Deliberately unauthenticated and deliberately empty: it exists so a proxy or
    a compose healthcheck can tell a running server from a dead one. It does not
    say how many records are held or for which spaces — that is a fact about the
    people using it.
    """
    db.execute(select(1))
    return {"ok": True, "service": "meerail-journal"}


@app.post("/journal")
def append(body: AppendIn, space: str = Depends(require_space),
           db: Session = Depends(get_db)) -> dict:
    """Take these records, number them, say what they were numbered.

    The numbers come back in the order they were sent, and a client needs them:
    an install that appends a claim on a due reminder has to know its own claim's
    number to find out whether it won (app/journal.py::claim_reminder).
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = [Record(space=space, blob=r.blob, snapshot=r.snapshot, created_at=now)
            for r in body.records]
    db.add_all(rows)
    db.commit()
    _prune(db, space)
    return {"seqs": [r.seq for r in rows], "seq": rows[-1].seq}


@app.get("/journal")
def read(since: int = Query(0, ge=0), limit: int = Query(MAX_PAGE, ge=1, le=MAX_PAGE),
         space: str = Depends(require_space), db: Session = Depends(get_db)) -> dict:
    """Everything this space holds after ``since``, oldest first.

    ``floor`` and ``reset`` are how a client learns it has fallen off the end of
    the retention window. A machine that was off for longer than RETAIN_DAYS asks
    for everything after the number it remembers, and that number may now be
    below the oldest record held — in which case the answer it would otherwise
    get (a page starting mid-history) is missing the records that explain it.
    ``reset`` says so, and the client restarts from the newest snapshot instead
    of quietly applying a partial history.
    """
    floor = db.execute(
        select(func.min(Record.seq)).where(Record.space == space)).scalar() or 0
    rows = db.execute(
        select(Record.seq, Record.blob)
        .where(Record.space == space, Record.seq > since)
        .order_by(Record.seq)
        .limit(limit)
    ).all()
    latest = db.execute(
        select(func.max(Record.seq)).where(Record.space == space)).scalar() or 0
    return {
        "records": [{"seq": seq, "blob": blob} for seq, blob in rows],
        "next": rows[-1][0] if rows else since,
        "latest": latest,
        "floor": floor,
        # since == 0 is a client that has never synced, which is not a gap.
        "reset": bool(since and floor and since < floor - 1),
    }


# How often pruning is even considered, in seconds. Deleting is cheap and
# pointless to do on every append; a server that is written to constantly should
# not run a DELETE per record.
_PRUNE_EVERY = 3600
_last_prune = 0.0


def _prune(db: Session, space: str) -> int:
    """Drop records that are both old and superseded. Returns how many.

    Both halves are required, and dropping either one loses data somebody still
    needs:

    * **Old alone** would delete the only record of a reminder due in three
      months, because it was *set* four months ago. Nothing here can tell that
      from a record that no longer matters — the server cannot read either of
      them.
    * **Superseded alone** would delete history the moment a snapshot lands,
      leaving a machine that was mid-sync asking for records that existed when it
      started and do not now.

    So: a record goes only when a later snapshot in the same space has restated
    whatever it said, *and* the retention window has passed since it was written.
    A space that never posts a snapshot keeps everything, which is the right
    failure: unbounded growth is visible and recoverable, and silently dropping a
    reminder is neither.
    """
    global _last_prune
    now = time.monotonic()
    if now - _last_prune < _PRUNE_EVERY:
        return 0
    _last_prune = now

    newest_snapshot = db.execute(
        select(func.max(Record.seq)).where(Record.space == space, Record.snapshot.is_(True))
    ).scalar()
    if not newest_snapshot:
        return 0
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=RETAIN_DAYS)
    result = db.execute(
        delete(Record).where(
            Record.space == space,
            Record.seq < newest_snapshot,
            Record.created_at < cutoff,
        )
    )
    db.commit()
    return result.rowcount or 0
