"""Two installs of meerail, one journal, and what each of them ends up believing.

The transport is stubbed to an in-memory log — a list, plus the sequence numbers
a real server would hand out — so these run against Postgres and nothing else.
What is being tested is not the HTTP (tests/test_journal_server_unit.py covers
that) but the part that is easy to get wrong: applying a record must write the
promise without moving the mail, a record naming mail this install has not synced
must wait rather than vanish, and two machines reaching nine o'clock together
must not both bring the same conversation back.

The "other machine" here is a second set of rows in the same test database,
because the thing under test never sees a machine — it sees an account email, a
dedup_key and a folder name, and resolves all three locally. That is the whole
portability claim, so exercising it through one database is not a shortcut: an
install that could only apply records it had written itself would pass nothing
here.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import dbfixture
from core import journal as wire
from core.models import Account, Mailbox, Message, Reminder, utcnow
from helpers import make_message

from app import journal as client
from app import reminders as reminders_core

PASSPHRASE = "a-long-enough-journal-passphrase"
SENT = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)


class FakeLog:
    """The journal server, as far as a client can tell: an ordered list.

    Numbering from one, exactly as the real one does, because the claim rule
    compares those numbers and a fake that handed out zeros would make a
    genuinely broken comparison pass.
    """

    def __init__(self) -> None:
        self.records: list[tuple[int, str]] = []

    def post(self, records):
        seqs = []
        for record, _snapshot in records:
            seq = len(self.records) + 1
            self.records.append((seq, wire.seal(client.keys(), record)))
            seqs.append(seq)
        return seqs

    def fetch(self, since):
        rows = [{"seq": s, "blob": b} for s, b in self.records if s > since]
        return {"records": rows, "next": rows[-1]["seq"] if rows else since,
                "latest": self.records[-1][0] if self.records else 0,
                "floor": self.records[0][0] if self.records else 0, "reset": False}


@pytest.fixture
def log(monkeypatch):
    """A journal, configured and stubbed, torn down after."""
    monkeypatch.setattr(client.settings, "journal_url", "http://journal.invalid")
    monkeypatch.setattr(client.settings, "journal_passphrase", PASSPHRASE)
    monkeypatch.setattr(client.settings, "journal_instance", "machine-a")
    monkeypatch.setattr(client, "_keys", None)
    fake = FakeLog()
    monkeypatch.setattr(client, "_post", fake.post)
    monkeypatch.setattr(client, "_fetch", fake.fetch)
    yield fake
    client._keys = None


def as_other_machine(monkeypatch, name="machine-b"):
    """Write the next records as though a different install produced them."""
    monkeypatch.setattr(client.settings, "journal_instance", name)


def _clear_journal_state(db) -> None:
    """The rows that are not owned by any account: the outbox, the deferred
    records and the cursor. Nothing cascades them, so they are cleared by hand."""
    from core.models import JournalDeferred, JournalOutbox, Setting

    for model in (JournalOutbox, JournalDeferred):
        for row in db.execute(select(model)).scalars().all():
            db.delete(row)
    if (cursor := db.get(Setting, client.CURSOR_KEY)) is not None:
        db.delete(cursor)


@pytest.fixture
def acct():
    """An account with an inbox and an archive — the two folders a reminder needs.

    Torn down completely, and that matters more here than in most fixtures:
    parking a conversation writes a PendingAction, and several suites assert on
    the *global* list of recent actions. A journal test that left its reminders
    behind would fail test_undo.py rather than itself, which is the worst kind of
    failure to be handed.
    """
    email = f"journal-{uuid.uuid4().hex[:8]}@example.com"
    dbfixture.create_account(email)
    with dbfixture.session() as db:
        account = db.execute(select(Account).where(Account.email == email)).scalars().one()
        for name, role in (("INBOX", "inbox"), ("Archive", "archive")):
            db.add(Mailbox(account_id=account.id, imap_name=name, role=role, uidvalidity=1))
        _clear_journal_state(db)
        db.flush()
        account_id = account.id

    yield {"id": account_id, "email": email}

    with dbfixture.session() as db:
        _clear_journal_state(db)
        if (account := db.get(Account, account_id)) is not None:
            # Cascades to its mailboxes, messages, reminders and pending actions.
            db.delete(account)


def add_message(email: str, subject: str, uid: int = 1) -> str:
    """Ingest one message; return the key a record would name it by.

    Bare, without the angle brackets the header carries — that is what
    ``dedup_key`` holds (core/mail/parse.py strips them), and the whole point of
    keying records on it is that every install derives the same string from the
    same message.
    """
    bare = f"{uuid.uuid4().hex}@journal.test"
    raw = make_message(f"<{bare}>", subject, "someone@ex.com", email, "body", SENT)
    dbfixture.ingest_raw_message(email, raw, uid=uid)
    return bare


def message_for(db, account_id: int, mid: str) -> Message:
    return db.execute(
        select(Message).where(Message.account_id == account_id, Message.dedup_key == mid)
    ).scalars().one()


def pending(db, account_id: int) -> list[Reminder]:
    return list(db.execute(
        select(Reminder).where(Reminder.account_id == account_id,
                               Reminder.state == "pending")
    ).scalars().all())


# --- Publishing and applying ------------------------------------------------


def test_a_reminder_set_here_is_published_and_applies_there(log, acct, monkeypatch):
    """The end-to-end shape: park on one install, and the promise exists on the
    other without the other touching any mail."""
    mid = add_message(acct["email"], "park me")
    with dbfixture.session() as db:
        msg = message_for(db, acct["id"], mid)
        due = utcnow() + timedelta(days=1)
        reminder, _ = reminders_core.set_reminder(db, msg, due)
        db.flush()
        client.publish_reminder_set(db, reminder, msg)

    with dbfixture.session() as db:
        assert client.drain(db) == 1

    # The record is on the log, sealed — the server holds no plaintext.
    assert len(log.records) == 1
    assert "park me" not in log.records[0][1]
    assert acct["email"] not in log.records[0][1]

    # Now the other install reads it. Wipe this one's copy first so what is
    # asserted is what the *record* produced, not what set_reminder left behind.
    with dbfixture.session() as db:
        for row in pending(db, acct["id"]):
            db.delete(row)
        client.set_cursor(db, 0)

    as_other_machine(monkeypatch)
    with dbfixture.session() as db:
        assert client.pull(db) == 1
        rows = pending(db, acct["id"])
        assert len(rows) == 1
        assert abs((rows[0].due_at - due).total_seconds()) < 1
        assert rows[0].message_pk == message_for(db, acct["id"], mid).id


def test_applying_a_record_moves_no_mail(log, acct, monkeypatch):
    """The install that reads the record must not archive anything.

    The machine that set the reminder already moved the conversation, and that
    move reaches every other install through the mail server. An apply path that
    also moved it would file it twice — and the second move would be against a
    message that is no longer where it thinks.
    """
    mid = add_message(acct["email"], "leave me alone")
    with dbfixture.session() as db:
        msg = message_for(db, acct["id"], mid)
        before = {loc.mailbox_id for loc in msg.locations}
        record = wire.envelope(
            client.REMINDER,
            {"op": "set", "due_at": (utcnow() + timedelta(days=1)).isoformat(),
             "park": "Archive", "parked": [{"message": mid, "from": ["INBOX"]}]},
            instance="machine-b", account=acct["email"], key=mid)
        client._apply(db, record, seq=1)

    with dbfixture.session() as db:
        msg = message_for(db, acct["id"], mid)
        assert {loc.mailbox_id for loc in msg.locations} == before
        assert len(pending(db, acct["id"])) == 1


def test_a_second_set_only_moves_the_deadline(log, acct):
    """Same rule the local path follows, and for the same reason: re-parking
    would overwrite the record of where the mail came from with where it now is."""
    mid = add_message(acct["email"], "twice")
    first = utcnow() + timedelta(days=1)
    second = utcnow() + timedelta(days=3)
    for due in (first, second):
        with dbfixture.session() as db:
            record = wire.envelope(
                client.REMINDER,
                {"op": "set", "due_at": due.isoformat(), "park": "Archive",
                 "parked": [{"message": mid, "from": ["INBOX"]}]},
                instance="machine-b", account=acct["email"], key=mid)
            client._apply(db, record, seq=1)

    with dbfixture.session() as db:
        rows = pending(db, acct["id"])
        assert len(rows) == 1
        assert abs((rows[0].due_at - second).total_seconds()) < 1


def test_cancel_and_fired_retire_the_promise(log, acct):
    mid = add_message(acct["email"], "retire me")
    for op, state in (("cancel", "cancelled"), ("fired", "done")):
        with dbfixture.session() as db:
            for row in db.execute(select(Reminder)).scalars().all():
                db.delete(row)
        with dbfixture.session() as db:
            client._apply(db, wire.envelope(
                client.REMINDER,
                {"op": "set", "due_at": (utcnow() + timedelta(days=1)).isoformat(),
                 "park": "Archive", "parked": [{"message": mid, "from": ["INBOX"]}]},
                instance="machine-b", account=acct["email"], key=mid), seq=1)
        with dbfixture.session() as db:
            client._apply(db, wire.envelope(
                client.REMINDER, {"op": op},
                instance="machine-b", account=acct["email"], key=mid), seq=2)
        with dbfixture.session() as db:
            row = db.execute(select(Reminder)).scalars().one()
            assert row.state == state


# --- Being behind -----------------------------------------------------------


def test_a_record_for_mail_that_has_not_synced_waits(log, acct, monkeypatch):
    """The ordinary case, not an error: one machine finished syncing first.

    The cursor must move past it (or one message that never arrives freezes the
    whole log) while the record itself is kept and retried.
    """
    absent = "not-synced-here@journal.test"
    as_other_machine(monkeypatch)
    with dbfixture.session() as db:
        client.publish(db, client.REMINDER, {
            "op": "set", "due_at": (utcnow() + timedelta(days=1)).isoformat(),
            "park": "Archive", "parked": []}, account=acct["email"], key=absent)
    with dbfixture.session() as db:
        client.drain(db)

    with dbfixture.session() as db:
        client.set_cursor(db, 0)
    with dbfixture.session() as db:
        assert client.pull(db) == 0                 # nothing applied...
        assert client.cursor(db) == 1               # ...but the cursor moved on
        from core.models import JournalDeferred
        assert db.execute(select(JournalDeferred)).scalars().one().seq == 1

    # The mail arrives, and the next pass picks the record up without it being
    # sent again.
    raw = make_message(f"<{absent}>", "late arrival", "s@ex.com", acct["email"], "body", SENT)
    dbfixture.ingest_raw_message(acct["email"], raw, uid=99)
    with dbfixture.session() as db:
        assert client.pull(db) == 1
        assert len(pending(db, acct["id"])) == 1
        from core.models import JournalDeferred
        assert db.execute(select(JournalDeferred)).scalars().all() == []


def test_a_cancel_cannot_overtake_the_set_it_cancels(log, acct):
    """Both waiting on the same absent message, and order still has to hold.

    Applied out of order the cancel finds nothing to cancel and does nothing,
    and the set then lands behind it — resurrecting a reminder the user had
    already taken back.
    """
    from core.models import JournalDeferred

    absent = "still-elsewhere@journal.test"
    for op_body in ({"op": "set", "due_at": (utcnow() + timedelta(days=1)).isoformat(),
                     "park": "Archive", "parked": []},
                    {"op": "cancel"}):
        with dbfixture.session() as db:
            client.publish(db, client.REMINDER, op_body,
                           account=acct["email"], key=absent)
    with dbfixture.session() as db:
        client.drain(db)
    with dbfixture.session() as db:
        client.pull(db)
        assert len(db.execute(select(JournalDeferred)).scalars().all()) == 2

    # The message arrives. Both records apply this pass, in order, and the
    # conversation ends up cancelled rather than pending.
    raw = make_message(f"<{absent}>", "at last", "s@ex.com", acct["email"], "body", SENT)
    dbfixture.ingest_raw_message(acct["email"], raw, uid=77)
    with dbfixture.session() as db:
        client.pull(db)
        assert pending(db, acct["id"]) == []
        assert db.execute(select(Reminder)).scalars().one().state == "cancelled"


def test_a_reminder_waits_for_the_folder_its_mail_is_parked_in(log, acct):
    """Without the park folder there is nothing for fire() to take the mail out
    of, so a reminder recorded now would look healthy and then move nothing."""
    mid = add_message(acct["email"], "no such folder")
    with dbfixture.session() as db:
        with pytest.raises(client.Defer):
            client._apply(db, wire.envelope(
                client.REMINDER,
                {"op": "set", "due_at": (utcnow() + timedelta(days=1)).isoformat(),
                 "park": "Some/Folder/This/Install/Has/Not/Listed", "parked": []},
                instance="machine-b", account=acct["email"], key=mid), seq=1)


def test_sent_records_do_not_pile_up_forever(log, acct):
    from core.models import JournalOutbox

    with dbfixture.session() as db:
        client.publish(db, client.ACCOUNT_PREFS, {"label": "x"}, account=acct["email"])
    with dbfixture.session() as db:
        client.drain(db)
        row = db.execute(select(JournalOutbox)).scalars().one()
        row.sent_at = utcnow() - client.KEEP_SENT - timedelta(days=1)
    with dbfixture.session() as db:
        client.drain(db)
        assert db.execute(select(JournalOutbox)).scalars().all() == []


def test_a_record_for_an_unknown_account_waits_rather_than_failing(log, acct):
    """A machine that syncs two of three accounts is an ordinary setup."""
    with dbfixture.session() as db:
        with pytest.raises(client.Defer):
            client._apply(db, wire.envelope(
                client.REMINDER, {"op": "cancel"},
                instance="machine-b", account="nobody@example.com", key="<x@y>"), seq=1)


def test_a_record_sealed_with_another_passphrase_is_stepped_over(log, acct):
    """Not an error and not retryable: somebody pointed a machine at the wrong
    journal, and the records that *are* ours are behind it."""
    other = wire.derive("a-completely-different-passphrase")
    log.records.append((1, wire.seal(other, wire.envelope(
        client.REMINDER, {"op": "cancel"}, instance="stranger"))))
    with dbfixture.session() as db:
        assert client.pull(db) == 0
        assert client.cursor(db) == 1


# --- Claiming ---------------------------------------------------------------


def test_only_one_install_fires_a_due_reminder(log, acct, monkeypatch):
    """The race this whole mechanism exists for.

    Both machines hold the reminder, both reach the deadline, both claim. The
    lower sequence number wins and the other stands down — otherwise the
    conversation is moved back into the inbox twice and marked unread twice.
    """
    mid = add_message(acct["email"], "who fires me")
    with dbfixture.session() as db:
        msg = message_for(db, acct["id"], mid)
        reminder, _ = reminders_core.set_reminder(db, msg, utcnow() + timedelta(seconds=60))
        db.flush()
        reminder_id = reminder.id

    # Machine B claims first — it gets sequence 1.
    as_other_machine(monkeypatch)
    with dbfixture.session() as db:
        first = db.get(Reminder, reminder_id)
        assert client.claim(db, first) is True

    # Machine A claims second, reads the log back, and finds B ahead of it.
    as_other_machine(monkeypatch, "machine-a")
    with dbfixture.session() as db:
        second = db.get(Reminder, reminder_id)
        assert client.claim(db, second) is False
        assert second.claim_by == "machine-b"


def test_a_claim_that_went_stale_can_be_taken_over(log, acct, monkeypatch):
    """A machine that claimed and was then shut must not park mail forever."""
    mid = add_message(acct["email"], "abandoned")
    with dbfixture.session() as db:
        msg = message_for(db, acct["id"], mid)
        reminder, _ = reminders_core.set_reminder(db, msg, utcnow() + timedelta(seconds=60))
        db.flush()
        reminder.claim_seq, reminder.claim_by = 1, "machine-b"
        reminder.claim_at = utcnow() - client.CLAIM_TTL - timedelta(minutes=1)
        reminder_id = reminder.id

    with dbfixture.session() as db:
        assert client.claim(db, db.get(Reminder, reminder_id)) is True


def test_an_unreachable_journal_fires_rather_than_stalling(log, acct, monkeypatch):
    """A rented box nobody is paying for must not silently turn every reminder
    into a conversation that never comes back."""
    mid = add_message(acct["email"], "fire anyway")
    with dbfixture.session() as db:
        msg = message_for(db, acct["id"], mid)
        reminder, _ = reminders_core.set_reminder(db, msg, utcnow() + timedelta(seconds=60))
        db.flush()
        reminder_id = reminder.id

    def dead(*_args, **_kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(client, "_post", dead)
    with dbfixture.session() as db:
        assert client.claim(db, db.get(Reminder, reminder_id)) is True


# --- The second kind --------------------------------------------------------


def test_account_prefs_travel(log, acct, monkeypatch):
    """Footers and account names, which is the other thing IMAP cannot hold."""
    as_other_machine(monkeypatch)
    with dbfixture.session() as db:
        client._apply(db, wire.envelope(
            client.ACCOUNT_PREFS, {"footer": "-- \nfrom the laptop", "label": "Personal"},
            instance="machine-b", account=acct["email"], key=acct["email"]), seq=1)

    with dbfixture.session() as db:
        account = db.get(Account, acct["id"])
        assert account.footer == "-- \nfrom the laptop"
        assert account.label == "Personal"
        # Saving a footer from anywhere opts the account out of the default
        # backfill, exactly as saving one in Settings does.
        assert account.footer_customized is True


def test_a_field_pinned_in_the_config_file_is_not_overwritten(log, acct, monkeypatch):
    """meerail.toml is the more specific instruction, and it is local on purpose.

    Applying the record would start a fight the file wins a minute later, when
    the agent rewrites the value on its next pass.
    """
    with dbfixture.session() as db:
        account = db.get(Account, acct["id"])
        account.config_fields = ["label"]
        account.label = "From the file"

    with dbfixture.session() as db:
        client._apply(db, wire.envelope(
            client.ACCOUNT_PREFS, {"label": "From the journal", "color": "#ff0000"},
            instance="machine-b", account=acct["email"], key=acct["email"]), seq=1)

    with dbfixture.session() as db:
        account = db.get(Account, acct["id"])
        assert account.label == "From the file"      # pinned, left alone
        assert account.color == "#ff0000"            # not pinned, applied


# --- The two halves against each other --------------------------------------


def test_the_client_and_the_real_server_agree(acct, tmp_path, monkeypatch):
    """Everything above stubs the transport; this one does not.

    The stub is the right tool for the logic — but it is also the place a wire
    mismatch hides, because a fake that both sides of the test agree about
    proves nothing about the JSON the server actually returns. So this runs the
    real ``_post`` and ``_fetch`` bodies against the real journal server, in
    process, over Starlette's TestClient (which is an httpx client, which is
    what app/journal.py speaks).
    """
    testclient = pytest.importorskip("fastapi.testclient")
    import importlib
    import sys

    monkeypatch.setattr(client.settings, "journal_url", "http://testserver")
    monkeypatch.setattr(client.settings, "journal_passphrase", PASSPHRASE)
    monkeypatch.setattr(client.settings, "journal_instance", "machine-a")
    monkeypatch.setattr(client, "_keys", None)

    monkeypatch.setenv("JOURNAL_DATABASE_URL", f"sqlite:///{tmp_path/'j.db'}")
    monkeypatch.setenv("JOURNAL_SPACES", client.keys().space)
    sys.modules.pop("journal.server", None)
    server = importlib.import_module("journal.server")

    with testclient.TestClient(server.app) as http:
        http.headers["Authorization"] = f"Bearer {client.keys().token}"
        monkeypatch.setattr(client, "_client", lambda: http)

        mid = add_message(acct["email"], "over the wire")
        with dbfixture.session() as db:
            msg = message_for(db, acct["id"], mid)
            due = utcnow() + timedelta(days=2)
            reminder, _ = reminders_core.set_reminder(db, msg, due)
            db.flush()
            client.publish_reminder_set(db, reminder, msg)
        with dbfixture.session() as db:
            assert client.drain(db) == 1

        # The server holds it, and holds it sealed.
        with server.SessionLocal() as sdb:
            assert "over the wire" not in sdb.query(server.Record).one().blob

        # And reading it back through the real endpoint reconstructs the promise.
        with dbfixture.session() as db:
            for row in pending(db, acct["id"]):
                db.delete(row)
            client.set_cursor(db, 0)
        with dbfixture.session() as db:
            assert client.pull(db) == 1
            rows = pending(db, acct["id"])
            assert len(rows) == 1
            assert abs((rows[0].due_at - due).total_seconds()) < 1

    sys.modules.pop("journal.server", None)
    client._keys = None


# --- Off by default ---------------------------------------------------------


def test_nothing_is_published_when_no_journal_is_configured(acct):
    """An install with no journal is the install that existed before this did."""
    from core.models import JournalOutbox
    assert client.enabled() is False
    with dbfixture.session() as db:
        client.publish(db, client.ACCOUNT_PREFS, {"label": "x"}, account=acct["email"])
    with dbfixture.session() as db:
        assert db.execute(select(JournalOutbox)).scalars().all() == []
