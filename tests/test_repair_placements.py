"""Merging mail stored twice and clearing dead placeholders (tools/repair_placements.py).

Database-only, like the other tool tests, plus a stand-in for the mail server
where the tool asks one. What these pin is what the tool deletes, because that is
the one way it can do harm. A merge must never fold two different messages into
one, and a message that exists only here must never be forgotten on anything
short of the server saying it does not have it either.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
from sqlalchemy import select

import dbfixture
import repair_placements as rp
from core.config import AccountConfig
from core.database import SessionLocal
from core.mail.parse import content_key
from core.models import (Account, Mailbox, Message, MessageLocation, PendingAction,
                         Reminder, utcnow)
from helpers import make_message

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
LONG_AGO = timedelta(days=2)


@pytest.fixture
def email() -> str:
    return f"repair-{uuid.uuid4().hex[:10]}@example.com"


def run(email: str, apply: bool, forget: bool = False, config=None, backup=None):
    with SessionLocal() as db:
        account = db.execute(select(Account).where(Account.email == email)).scalar_one()
        return rp.repair_account(db, account, apply=apply, forget_unheld=forget,
                                 config=config, backup=backup)


def message_pks(email: str) -> list[int]:
    with SessionLocal() as db:
        account = db.execute(select(Account).where(Account.email == email)).scalar_one()
        return sorted(db.execute(select(Message.id).where(
            Message.account_id == account.id)).scalars().all())


def pk_of(email: str, subject: str) -> int:
    with SessionLocal() as db:
        return db.execute(select(Message.id).join(Account).where(
            Account.email == email, Message.subject == subject)).scalar_one()


def places(email: str, message_pk: int) -> list[tuple[str, int]]:
    return sorted((p["imap_name"], p["imap_uid"]) for p in dbfixture.placements(email, message_pk))


def mailbox_id(email: str, name: str) -> int:
    with SessionLocal() as db:
        return db.execute(select(Mailbox.id).join(Account).where(
            Account.email == email, Mailbox.imap_name == name)).scalar_one()


def strand(email: str, message_pk: int, folder: str, *, keep_real: bool = False,
           seen: bool = False, ago: timedelta = LONG_AGO, status: str = "done",
           from_folder: str = "All Mail") -> None:
    """Leave a message with the placeholder a finished move wrote and nothing
    ever replaced: the state the tool exists to find."""
    with dbfixture.session() as db:
        msg = db.get(Message, message_pk)
        if not keep_real:
            for loc in list(msg.locations):
                db.delete(loc)
        target = db.execute(select(Mailbox.id).where(
            Mailbox.account_id == msg.account_id, Mailbox.imap_name == folder)).scalar_one()
        db.add(MessageLocation(message_pk=message_pk, mailbox_id=target,
                               imap_uid=-message_pk, seen=seen,
                               updated_at=utcnow() - ago))
        db.add(PendingAction(account_id=msg.account_id, message_pk=message_pk, type="move",
                             status=status, attempts=1, updated_at=utcnow() - ago,
                             payload={"from_folder": from_folder, "to_folder": folder,
                                      "uid": 1, "uidvalidity": 1}))


def twin_of(email: str, message_pk: int, read_at=None, raw: bytes | None = None) -> int:
    """A second row holding the same bytes under their hash. What ingest left
    behind before it looked a message up by its content as well. ``raw`` also
    stores the bytes themselves, which is what a comparison by bytes reads."""
    with dbfixture.session() as db:
        src = db.get(Message, message_pk)
        key = content_key(raw) if raw is not None else src.content_hash
        twin = Message(account_id=src.account_id, message_id=src.message_id,
                       dedup_key=key, content_hash=key, raw_mime=raw,
                       thread_id=key, subject=src.subject,
                       subject_norm=src.subject_norm, from_name=src.from_name,
                       from_addr=src.from_addr, date_sent=src.date_sent,
                       size_bytes=src.size_bytes, body_text=src.body_text,
                       body_html=src.body_html, body_sig="", read_at=read_at)
        db.add(twin)
        db.flush()
        return twin.id


def test_the_same_bytes_twice_are_merged_onto_the_row_the_id_names(email):
    """The bug report: one mail twice in Trash, once for real and once as the
    placeholder a trash keypress wrote on the other row. The merge keeps what
    either row carried, which is the read time, the reminder and the history."""
    tok = "DUPTOK" + uuid.uuid4().hex[:6]
    raw = make_message(f"<dup-{uuid.uuid4().hex}@t>", f"Report {tok}", "x@y.com", email,
                       "body", T0)
    dbfixture.ingest_raw_message(email, raw, uid=2, folder="Trash", role_hint="\\Trash")
    dbfixture.ingest_raw_message(email, raw, uid=9, folder="All Mail", role_hint="\\All")
    kept = pk_of(email, f"Report {tok}")
    read = datetime(2026, 3, 2, 8, 0)
    twin = twin_of(email, kept, read_at=read)
    strand(email, twin, "Trash", seen=True, from_folder="INBOX")
    with dbfixture.session() as db:
        # One that already fired. A pending one holds the merge back (see the
        # next test but one), a finished one is history and moves with the row.
        db.add(Reminder(account_id=db.get(Message, twin).account_id, message_pk=twin,
                        due_at=utcnow() - timedelta(days=3), state="done"))

    # A dry run reports and touches nothing.
    totals = run(email, apply=False)
    assert totals.merged == 1
    assert message_pks(email) == [kept, twin]
    assert places(email, twin) == [("Trash", -twin)]

    totals = run(email, apply=True)

    assert totals.merged == 1
    assert message_pks(email) == [kept]
    assert places(email, kept) == [("All Mail", 9), ("Trash", 2)]
    with SessionLocal() as db:
        assert db.get(Message, kept).read_at == read
        assert db.execute(select(Reminder.message_pk).where(
            Reminder.message_pk.in_((kept, twin)))).scalars().all() == [kept]
        moved = db.execute(select(PendingAction).where(
            PendingAction.message_pk == kept, PendingAction.type == "move")).scalars().all()
        assert [m.status for m in moved] == ["done"]
        # Read on the placeholder, and so read on the copy it was waiting for,
        # with the server told so.
        trash = db.execute(select(MessageLocation).where(
            MessageLocation.message_pk == kept, MessageLocation.imap_uid == 2)).scalar_one()
        assert trash.seen
        assert [a.payload["add"] for a in db.execute(select(PendingAction).where(
            PendingAction.message_pk == kept, PendingAction.type == "setflags")).scalars()] \
            == [["\\Seen"]]

    # Nothing left to find the second time round.
    assert run(email, apply=True).merged == 0


def test_a_merge_waits_while_a_change_to_the_message_is_queued(email):
    raw = make_message(f"<busy-{uuid.uuid4().hex}@t>", "Busy", "x@y.com", email, "body", T0)
    dbfixture.ingest_raw_message(email, raw, uid=2, folder="Trash", role_hint="\\Trash")
    kept = pk_of(email, "Busy")
    twin = twin_of(email, kept)
    strand(email, twin, "Trash", status="pending")

    totals = run(email, apply=True)

    assert (totals.merged, totals.blocked) == (0, 1)
    assert message_pks(email) == [kept, twin]



def test_a_row_from_before_content_hashes_is_matched_by_its_bytes(email):
    """Rows stored before content_hash existed have none, so a comparison of
    hashes cannot see them. Four of the five pairs on a real account were that,
    byte for byte the same message, and a dry run first missed them."""
    raw = make_message(f"<legacy-{uuid.uuid4().hex}@t>", "Legacy", "x@y.com", email,
                       "body", T0)
    dbfixture.ingest_raw_message(email, raw, uid=1, folder="INBOX")
    old = pk_of(email, "Legacy")
    with dbfixture.session() as db:
        db.get(Message, old).content_hash = None
    new = twin_of(email, old, raw=raw)
    all_mail = dbfixture.create_folder(email, "All Mail", "\\All")
    with dbfixture.session() as db:
        db.add(MessageLocation(message_pk=new, imap_uid=5, mailbox_id=all_mail))

    totals = run(email, apply=True)

    assert totals.merged == 1
    assert message_pks(email) == [old]
    assert places(email, old) == [("All Mail", 5), ("INBOX", 1)]

def bridge_copy(email: str, mid: str, body: str = "the body") -> bytes:
    """One message as Proton Bridge hands it over: a Gluon id and a boundary of
    its own making, both new each time Bridge builds the message again."""
    boundary = uuid.uuid4().hex * 2
    return (f"X-Pm-Gluon-Id: {uuid.uuid4()}\r\nMessage-ID: <{mid}>\r\n"
            f"Subject: Bridge GLUONTOK\r\nFrom: x@y.com\r\nTo: {email}\r\n"
            f"Date: {format_datetime(T0)}\r\nMIME-Version: 1.0\r\n"
            f"Content-Type: multipart/alternative;\r\n boundary={boundary}\r\n\r\n"
            f"--{boundary}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n"
            f"--{boundary}--\r\n").encode()


def test_one_message_bridge_wrote_out_twice_is_merged_but_a_collision_is_not(email):
    """Two serializations of one message differ in bytes and in nothing else
    that matters, so they are one message. A different mail under the same id
    and the same size is still a different mail, and stays one."""
    mid = f"gluon-{uuid.uuid4().hex}@t"
    first, second = bridge_copy(email, mid), bridge_copy(email, mid)
    other = bridge_copy(email, mid, body="not body")          # same length on purpose
    assert first != second and len(first) == len(second) == len(other)
    dbfixture.ingest_raw_message(email, first, uid=1, folder="INBOX")
    dbfixture.ingest_raw_message(email, second, uid=5, folder="All Mail", role_hint="\\All")
    dbfixture.ingest_raw_message(email, other, uid=3, folder="Archive",
                                 role_hint="\\Archive")
    before = message_pks(email)
    assert len(before) == 3

    totals = run(email, apply=True)

    assert totals.merged == 1
    keeper, collision = message_pks(email)
    assert places(email, keeper) == [("All Mail", 5), ("INBOX", 1)]
    assert places(email, collision) == [("Archive", 3)]


def test_placeholders_the_server_already_answers_for_are_dropped(email):
    """Beside a real copy in the same folder (Gmail's archive), or of mail the
    server has filed somewhere else. Either way the server's placements are the
    truth, and the placeholder was only ever showing the mail twice, or in a
    folder the server never put it in."""
    beside = make_message(f"<b-{uuid.uuid4().hex}@t>", "Beside", "x@y.com", email, "b", T0)
    away = make_message(f"<e-{uuid.uuid4().hex}@t>", "Elsewhere", "x@y.com", email, "e", T0)
    recent = make_message(f"<r-{uuid.uuid4().hex}@t>", "Recent", "x@y.com", email, "r", T0)
    dbfixture.ingest_raw_message(email, beside, uid=7, folder="All Mail", role_hint="\\All")
    dbfixture.ingest_raw_message(email, away, uid=4, folder="Archive", role_hint="\\Archive")
    dbfixture.ingest_raw_message(email, recent, uid=5, folder="Archive")
    dbfixture.create_folder(email, "Trash", "\\Trash")
    b, e, r = (pk_of(email, s) for s in ("Beside", "Elsewhere", "Recent"))
    strand(email, b, "All Mail", keep_real=True, seen=True, from_folder="INBOX")
    strand(email, e, "Trash", keep_real=True)
    strand(email, r, "Trash", keep_real=True, ago=timedelta(hours=1))

    totals = run(email, apply=True)

    assert totals.dropped == 2
    assert places(email, b) == [("All Mail", 7)]
    assert places(email, e) == [("Archive", 4)]
    # Too recent to judge: the copy may still be on its way.
    assert places(email, r) == [("Archive", 5), ("Trash", -r)]
    with SessionLocal() as db:
        assert db.execute(select(MessageLocation.seen).where(
            MessageLocation.message_pk == b)).scalar_one() is True


class _Server:
    """Enough of an IMAP session to be asked whether a message is in a folder."""

    def __init__(self, folders: dict[str, list[bytes]]):
        self.folders = {name: dict(enumerate(msgs, start=1)) for name, msgs in folders.items()}
        self.selected = None
        self.asked = []

    def ops(self):
        return self

    def logout(self):
        pass

    def select_folder(self, name, readonly=False):
        assert readonly, "the tool only ever reads the server"
        self.selected = name

    def search(self, criteria):
        self.asked.append((self.selected, criteria[2]))
        held = self.folders.get(self.selected, {})
        return [uid for uid, raw in held.items() if criteria[2].encode() in raw]

    def fetch(self, uids, _what):
        return {uid: {b"BODY[]": self.folders[self.selected][uid]} for uid in uids}


def test_mail_no_server_holds_is_forgotten_only_when_asked_and_the_server_agrees(
        email, monkeypatch, tmp_path):
    """The one destructive thing the tool does. Each of these is a message the
    database holds in no real folder, and only the first one is gone from the
    server for certain."""
    dbfixture.create_folder(email, "All Mail", "\\All")
    msgs = {}
    for name, mid in (("Gone", f"gone-{uuid.uuid4().hex}@t"),
                      ("Still there", f"there-{uuid.uuid4().hex}@t"),
                      ("No id", None),
                      ("Unmatched", f"odd-{uuid.uuid4().hex}@t")):
        raw = make_message(f"<{mid}>" if mid else None, name, "x@y.com", email, name, T0)
        dbfixture.ingest_raw_message(email, raw, uid=len(msgs) + 1, folder="Trash",
                                     role_hint="\\Trash")
        msgs[name] = (pk_of(email, name), mid, raw)
        strand(email, msgs[name][0], "Trash")
    stranger = make_message(f"<{msgs['Unmatched'][1]}>", "Unmatched", "x@y.com", email,
                            "a different body", T0)
    server = _Server({"Trash": [msgs["Still there"][2]], "All Mail": [stranger]})
    monkeypatch.setattr(rp, "_connect", lambda _acc: server)

    # --apply alone never deletes mail that exists only here.
    totals = run(email, apply=True)
    assert (totals.nowhere, totals.forgotten) == (4, 0)
    assert server.asked == []

    backup = rp.Backup(tmp_path / "forgotten.mbox")
    totals = run(email, apply=True, forget=True, config=AccountConfig(email=email),
                 backup=backup)
    backup.close()

    assert (totals.forgotten, totals.kept) == (1, 3)
    # Nothing is deleted before its bytes are on disk.
    import mailbox
    assert [m["Subject"] for m in mailbox.mbox(str(tmp_path / "forgotten.mbox"))] == ["Gone"]
    assert sorted(message_pks(email)) == sorted(
        msgs[n][0] for n in ("Still there", "No id", "Unmatched"))
    # Asked where the placeholder claims, in \All, and where the move came from.
    assert ("Trash", msgs["Gone"][1]) in server.asked
    assert ("All Mail", msgs["Gone"][1]) in server.asked



def test_a_merge_waits_while_a_reminder_names_the_message(email):
    """A reminder names its message by primary key and keeps the ids of what it
    parked. Deleting one of those rows would bring the reminder back to
    nothing, so the merge waits until the reminder is done."""
    raw = make_message(f"<rem-{uuid.uuid4().hex}@t>", "Remind me", "x@y.com", email,
                       "body", T0)
    dbfixture.ingest_raw_message(email, raw, uid=2, folder="Archive", role_hint="\\Archive")
    kept = pk_of(email, "Remind me")
    twin = twin_of(email, kept)
    with dbfixture.session() as db:
        db.add(Reminder(account_id=db.get(Message, kept).account_id, message_pk=kept,
                        due_at=utcnow() + timedelta(days=3),
                        parked=[{"message": twin, "from": []}]))

    totals = run(email, apply=True)

    assert (totals.merged, totals.blocked) == (0, 1)
    assert message_pks(email) == [kept, twin]


def test_the_row_that_still_has_its_content_is_the_one_kept(email):
    raw = make_message(f"<pruned-{uuid.uuid4().hex}@t>", "Pruned", "x@y.com", email,
                       "the body that matters", T0)
    dbfixture.ingest_raw_message(email, raw, uid=1, folder="INBOX")
    old = pk_of(email, "Pruned")
    new = twin_of(email, old)
    with dbfixture.session() as db:
        db.get(Message, old).content_status = "pruned"

    run(email, apply=True)

    assert message_pks(email) == [new]
    assert places(email, new) == [("INBOX", 1)]


def test_a_group_left_alone_keeps_its_placeholders_out_of_judgement(email, monkeypatch):
    """The keeper's placeholder may be waiting for the copy the other row holds.
    While the merge waits, so does any verdict on that placeholder, or it would
    read as mail no folder holds."""
    raw = make_message(f"<held-{uuid.uuid4().hex}@t>", "Held", "x@y.com", email, "b", T0)
    dbfixture.ingest_raw_message(email, raw, uid=3, folder="Trash", role_hint="\\Trash")
    kept = pk_of(email, "Held")
    other = twin_of(email, kept)
    with dbfixture.session() as db:
        for loc in list(db.get(Message, kept).locations):
            loc.message_pk = other            # the real copy sits on the other row
    strand(email, kept, "Trash")              # and the keeper only waits for it
    with dbfixture.session() as db:
        db.add(PendingAction(account_id=db.get(Message, other).account_id,
                             message_pk=other, type="setflags", status="pending",
                             payload={"folder": "Trash", "uid": 3, "uidvalidity": 1,
                                      "add": ["\\Seen"], "remove": []}))

    totals = run(email, apply=True)

    assert (totals.blocked, totals.nowhere) == (1, 0)


def test_two_placeholders_of_one_message_get_one_verdict(email, monkeypatch, tmp_path):
    dbfixture.create_folder(email, "Archive", "\\Archive")
    mid = f"two-{uuid.uuid4().hex}@t"
    raw = make_message(f"<{mid}>", "Twice stranded", "x@y.com", email, "b", T0)
    dbfixture.ingest_raw_message(email, raw, uid=1, folder="Trash", role_hint="\\Trash")
    pk = pk_of(email, "Twice stranded")
    strand(email, pk, "Trash")
    with dbfixture.session() as db:
        db.add(MessageLocation(message_pk=pk, imap_uid=-pk, updated_at=utcnow() - LONG_AGO,
                               mailbox_id=mailbox_id(email, "Archive")))
    server = _Server({})
    monkeypatch.setattr(rp, "_connect", lambda _acc: server)

    backup = rp.Backup(tmp_path / "f.mbox")
    totals = run(email, apply=True, forget=True, config=AccountConfig(email=email),
                 backup=backup)
    backup.close()

    assert (totals.nowhere, totals.forgotten) == (1, 1)
    assert {folder for folder, _ in server.asked} >= {"Trash", "Archive"}

def test_forget_needs_apply():
    with pytest.raises(SystemExit):
        rp.main(["--forget"])


def test_the_command_line_dry_run_says_what_it_would_do_and_does_nothing(email, capsys):
    raw = make_message(f"<cli-{uuid.uuid4().hex}@t>", "From the shell", "x@y.com", email,
                       "body", T0)
    dbfixture.ingest_raw_message(email, raw, uid=2, folder="Trash", role_hint="\\Trash")
    kept = pk_of(email, "From the shell")
    twin = twin_of(email, kept)
    strand(email, twin, "Trash")

    assert rp.main(["-a", email]) == 0

    out = capsys.readouterr().out
    assert "1 duplicate row(s) would be merged" in out
    assert message_pks(email) == [kept, twin]

    assert rp.main(["--apply", "-a", email]) == 0
    assert message_pks(email) == [kept]


def test_bridge_neutral_key_ignores_only_what_bridge_makes_up():
    mid = f"k-{uuid.uuid4().hex}@t"
    a, b = bridge_copy("me@t", mid), bridge_copy("me@t", mid)
    assert content_key(a) != content_key(b)
    assert rp.bridge_neutral_key(a) == rp.bridge_neutral_key(b)
    assert rp.bridge_neutral_key(a) != rp.bridge_neutral_key(
        bridge_copy("me@t", mid, body="not body"))
