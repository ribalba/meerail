"""Two requests filing mail at once, and the deadlock that used to end one of them.

Reported 2026-11-03. The reader opens the next conversation without waiting for
the trash of the last one, so the trash and the next mail's mark-read run side by
side, and both recount the folders they touched. The recount walked a Python set
and locked the folder rows in whatever order the set came out in. Two requests
that shared INBOX and a second folder took them in opposite orders, Postgres
broke the deadlock by aborting the trash, and the browser showed "Internal Server
Error".

There are two fixes and tests for both. The recount now locks folders in id order
(core/mail/store.py::recompute_mailboxes). The thread trash and the mark-read run
again when Postgres picks them as a victim anyway (app/routers/actions.py). The
HTTP tests build the lock cycle by hand, because two real requests only meet in
it by luck.
"""

from __future__ import annotations

import threading
import time
import uuid

import psycopg.errors
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm.exc import StaleDataError

import dbfixture
from conftest import T0
from core.database import SessionLocal, engine, is_deadlock
from core.mail.store import recompute_mailboxes
from core.models import Mailbox
from helpers import api, make_message

# Takes the row lock an UPDATE of the counts takes, and changes nothing.
_LOCK = text("UPDATE mailboxes SET updated_at = updated_at WHERE id = :id")


def _wrapped(orig: Exception) -> OperationalError:
    return OperationalError("UPDATE mailboxes SET total_count=...", {}, orig)


def test_is_deadlock_reads_the_sqlstate():
    assert is_deadlock(_wrapped(psycopg.errors.DeadlockDetected("deadlock detected")))
    # A lock_timeout ends the same way as far as SQLAlchemy is concerned, and
    # running again would only wait out the same timeout once more.
    assert not is_deadlock(_wrapped(psycopg.errors.LockNotAvailable("lock timeout")))
    assert not is_deadlock(_wrapped(RuntimeError("no sqlstate at all")))
    assert not is_deadlock(StaleDataError("expected to update 1 row(s); 0 were matched"))


def test_is_deadlock_knows_the_error_postgres_raises():
    """The hand-built exceptions above only count if they look like the real one.

    Two sessions take two folder rows in opposite orders, which is the cycle
    from the report. Postgres aborts exactly one of them.
    """
    email = f"cycle-{uuid.uuid4().hex[:10]}@example.com"
    dbfixture.create_account(email)
    a, b = dbfixture.create_folder(email, "INBOX"), dbfixture.create_folder(email, "Archive")
    first, second = SessionLocal(), SessionLocal()
    errors: list[Exception] = []

    def finish(db, mailbox_id):
        try:
            db.execute(_LOCK, {"id": mailbox_id})
            db.commit()
        except OperationalError as exc:
            errors.append(exc)
            db.rollback()

    try:
        first.execute(_LOCK, {"id": a})
        second.execute(_LOCK, {"id": b})
        threads = [threading.Thread(target=finish, args=(first, b)),
                   threading.Thread(target=finish, args=(second, a))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
    finally:
        first.close()
        second.close()
    assert len(errors) == 1
    assert is_deadlock(errors[0])


def test_recount_locks_folders_in_id_order():
    """The set the old loop walked, and the order the rows are written in now.

    The pair is picked so that the set really does hand the larger id out
    first, which is the order the old loop locked them in. The search keeps the
    test free of any assumption about how CPython lays out a set.
    """
    email = f"order-{uuid.uuid4().hex[:10]}@example.com"
    dbfixture.create_account(email)
    folders = [dbfixture.create_folder(email, f"Folder {n}") for n in range(9)]

    def backwards(low, high):
        ids = set()
        ids.add(high)
        ids.add(low)
        return ids if list(ids) == [high, low] else None

    low, high, touched = next((a, b, backwards(a, b)) for a in folders for b in folders
                              if a < b and backwards(a, b))

    with SessionLocal() as db:
        # Stale counts, so each recount has something to write.
        for mailbox_id in (low, high):
            db.get(Mailbox, mailbox_id).total_count = 99
        db.commit()

    written: list[int] = []

    def note(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE MAILBOXES"):
            for row in params if executemany else [params]:
                written.append(row["mailboxes_id"])

    event.listen(engine, "before_cursor_execute", note)
    try:
        with SessionLocal() as db:
            recompute_mailboxes(db, touched)
            db.commit()
    finally:
        event.remove(engine, "before_cursor_execute", note)
    assert written == [low, high]


# --- the retry, over HTTP -----------------------------------------------------


def _mailbox_ids(email: str) -> dict[str, int]:
    _, boxes = api("GET", "/api/mailboxes")
    mine = next(x for x in boxes["accounts"] if x["email"] == email)["mailboxes"]
    return {m["role"]: m["id"] for m in mine}


def _wait_until_blocked(holder) -> None:
    """Wait for a server backend to queue for a folder row ``holder`` has locked.

    A fresh transaction for every look, because pg_stat_activity is read once
    per transaction and would otherwise keep answering with the first picture.
    """
    me = holder.execute(text("SELECT pg_backend_pid()")).scalar_one()
    deadline = time.monotonic() + 15
    with SessionLocal() as watch:
        while time.monotonic() < deadline:
            waiting = watch.execute(text(
                "SELECT pid FROM pg_stat_activity "
                "WHERE :me = ANY(pg_blocking_pids(pid)) AND query ILIKE 'UPDATE mailboxes%'"),
                {"me": me}).first()
            watch.rollback()
            if waiting:
                return
            time.sleep(0.05)
    raise AssertionError("the request never queued for the folder row this test holds")


def _deadlocked(request, low: int, high: int):
    """Run ``request`` in a thread and close the lock cycle from the report around it.

    The test locks ``high``. The request recounts in id order, so it writes
    ``low`` and then queues for ``high``. The test then asks for ``low``, and
    each side is waiting for the other. The test's own deadlock_timeout is a
    minute, so the request's check always runs first and Postgres always picks
    the request as the victim. Only a retry can still bring it to a 200, and it
    gets there once the test has committed.
    """
    result = []
    thread = threading.Thread(target=lambda: result.append(request()))
    holder = SessionLocal()
    try:
        holder.execute(text("SET LOCAL deadlock_timeout = '60s'"))
        holder.execute(_LOCK, {"id": high})
        thread.start()
        _wait_until_blocked(holder)
        # Returns once Postgres has rolled the request back.
        holder.execute(_LOCK, {"id": low})
        holder.commit()
    finally:
        holder.close()
        if thread.ident is not None:
            thread.join(timeout=30)
    return result[0]


def test_thread_trash_survives_a_deadlock(account):
    email, aid = account["email"], account["id"]
    tok = "DEADTRASH" + uuid.uuid4().hex[:6]
    dbfixture.ingest_raw_message(email, make_message(
        f"<dl-{uuid.uuid4().hex}@t>", f"Subject {tok}", "x@y.com", email, f"{tok} body", T0),
        uid=1)
    dbfixture.ingest_raw_message(email, make_message(
        f"<trash-seed-{uuid.uuid4().hex}@t>", "Seed", "x@y.com", email, "seed", T0),
        uid=1, folder="Trash", role_hint="\\Trash")
    boxes = _mailbox_ids(email)
    _, rows = api("GET", f"/api/messages?mailbox_id={boxes['inbox']}&limit=50")
    thread_id = next(r for r in rows["rows"] if tok in r["subject"])["thread_id"]

    low, high = sorted((boxes["inbox"], boxes["trash"]))
    code, body = _deadlocked(
        lambda: api("POST", f"/api/messages/threads/{thread_id}/trash?account_id={aid}"),
        low, high)
    assert code == 200, body
    assert body["moved"] == 1
    _, rows = api("GET", f"/api/messages?mailbox_id={boxes['trash']}&limit=50")
    assert any(tok in r["subject"] for r in rows["rows"])


def test_mark_read_survives_a_deadlock(account):
    """The other side of the report. A label server files the mail twice, so
    marking it read recounts INBOX and All Mail."""
    email = account["email"]
    tok = "DEADREAD" + uuid.uuid4().hex[:6]
    message_id = f"dl-{uuid.uuid4().hex}@t"
    dbfixture.ingest_raw_message(email, make_message(
        f"<{message_id}>", f"Subject {tok}", "x@y.com", email, f"{tok} body", T0), uid=1)
    dbfixture.record_placement(email, message_id, uid=101, folder="All Mail",
                               role_hint="\\All")
    pk = dbfixture.message_pk_for(email, message_id)
    boxes = _mailbox_ids(email)

    low, high = sorted((boxes["inbox"], boxes["all"]))
    code, body = _deadlocked(lambda: api("POST", f"/api/messages/{pk}/mark?seen=1"),
                             low, high)
    assert code == 200, body
    _, detail = api("GET", f"/api/messages/{pk}")
    assert detail["seen"] is True
