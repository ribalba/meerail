"""The mbox importer (tools/import_mbox.py).

Database-only: the importer is agent-side work — it writes through core.ingest
and never touches the web app — so these run without a server, unlike most of
the integration suite. Tika is used when it is up (attachment text) and the one
case that needs it skips when it is not.
"""

from __future__ import annotations

import mailbox
import uuid
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from pathlib import Path

import import_mbox
import pytest
from sqlalchemy import func, select

from core.database import SessionLocal
from core.mail import tika
from core.models import Account, Attachment, Mailbox, Message, MessageLocation
from helpers import make_message

T0 = datetime(2026, 2, 1, 9, 0, tzinfo=timezone.utc)


def write_mbox(path: Path, messages: list[bytes], flags: list[str] | None = None) -> Path:
    """An mbox holding these raw messages, with optional mbox status letters."""
    box = mailbox.mbox(str(path))
    for n, raw in enumerate(messages):
        item = mailbox.mboxMessage(message_from_bytes(raw))
        if flags and flags[n]:
            item.set_flags(flags[n])
        box.add(item)
    box.flush()
    box.close()
    return path


def account_row(db, email: str) -> Account:
    return db.execute(select(Account).where(Account.email == email)).scalar_one()


def folder_row(db, account: Account, name: str) -> Mailbox:
    return db.execute(
        select(Mailbox).where(Mailbox.account_id == account.id, Mailbox.imap_name == name)
    ).scalar_one()


@pytest.fixture
def email() -> str:
    return f"mbox-{uuid.uuid4().hex[:10]}@imported.local"


def test_import_creates_account_threads_and_marks_read(tmp_path, email):
    a, b, c = (f"{p}-{uuid.uuid4().hex}@t" for p in ("a", "b", "c"))
    box = write_mbox(tmp_path / "archive.mbox", [
        make_message(f"<{a}>", "Subject GAMMA", "x@y.com", email, "the body", T0),
        make_message(f"<{b}>", "Re: Subject GAMMA", "z@y.com", email, "a reply",
                     T0 + timedelta(hours=1), in_reply_to=f"<{a}>", refs=[f"<{a}>"]),
        make_message(f"<{c}>", "Unrelated DELTA", "q@y.com", email, "other",
                     T0 + timedelta(days=2)),
    ])

    assert import_mbox.main([str(box), "--account", email, "--no-index"]) == 0

    with SessionLocal() as db:
        account = account_row(db, email)
        # Nothing will ever backfill this account, so it must not claim to be
        # mid-first-sync in the status panel.
        assert account.backfill_complete is True
        assert account.last_agent_seen is None

        inbox = folder_row(db, account, "INBOX")
        assert inbox.role == "inbox"
        assert inbox.total_count == 3
        assert inbox.unread_count == 0          # imported mail is not new mail

        msgs = db.execute(
            select(Message).where(Message.account_id == account.id)
        ).scalars().all()
        assert len(msgs) == 3
        assert len({m.thread_id for m in msgs}) == 2    # the reply threads
        assert all(m.content_status == "full" for m in msgs)

        uids = db.execute(
            select(MessageLocation.imap_uid).where(MessageLocation.mailbox_id == inbox.id)
        ).scalars().all()
        assert sorted(uids) == [1, 2, 3]
        assert inbox.last_uid == 3


def test_reimport_is_idempotent_and_resumes(tmp_path, email):
    ids = [f"{p}-{uuid.uuid4().hex}@t" for p in ("a", "b")]
    raws = [make_message(f"<{mid}>", f"Subject {n}", "x@y.com", email, "body", T0)
            for n, mid in enumerate(ids)]
    first = write_mbox(tmp_path / "one.mbox", raws)

    assert import_mbox.main([str(first), "--account", email, "--no-index"]) == 0
    # The same file again, plus one new message: the old two are already placed
    # in the folder, so only the new one lands.
    third = f"c-{uuid.uuid4().hex}@t"
    grown = write_mbox(tmp_path / "two.mbox", [
        *raws,
        make_message(f"<{third}>", "Subject 2", "x@y.com", email, "body", T0),
    ])
    assert import_mbox.main([str(grown), "--account", email, "--no-index"]) == 0

    with SessionLocal() as db:
        account = account_row(db, email)
        inbox = folder_row(db, account, "INBOX")
        assert db.scalar(
            select(func.count()).select_from(Message).where(Message.account_id == account.id)
        ) == 3
        assert db.scalar(
            select(func.count()).select_from(MessageLocation)
            .where(MessageLocation.mailbox_id == inbox.id)
        ) == 3
        assert inbox.total_count == 3


def test_two_messages_in_one_mbox_sharing_a_message_id_both_land(tmp_path, email):
    """An archive is exactly where a Message-ID collision turns up: years of
    mail, and somewhere in it a mailer with a broken generator or a list that
    re-sent under the old id.

    The importer asked "do I already hold this Message-ID in this folder?" before
    handing anything to the store, so the second message was skipped — counted as
    "already here", never stored, and never mentioned again. The question is what
    it is now: do I already hold *this message*.
    """
    mid = f"clash-{uuid.uuid4().hex}@t"
    box = write_mbox(tmp_path / "clash.mbox", [
        make_message(f"<{mid}>", "Invoice for March", "billing@vendor.example", email,
                     "Amount due: 100", T0),
        make_message(f"<{mid}>", "Invoice for April", "billing@vendor.example", email,
                     "Amount due: 999999", T0 + timedelta(days=30)),
    ])

    assert import_mbox.main([str(box), "--account", email, "--no-index"]) == 0

    with SessionLocal() as db:
        account = account_row(db, email)
        subjects = set(db.execute(
            select(Message.subject).where(Message.account_id == account.id)
        ).scalars().all())
        assert subjects == {"Invoice for March", "Invoice for April"}

    # And running it again still imports nothing: both are held now, and both
    # are recognised — the second by the key a collision is filed under.
    assert import_mbox.main([str(box), "--account", email, "--no-index"]) == 0
    with SessionLocal() as db:
        account = account_row(db, email)
        assert db.scalar(
            select(func.count()).select_from(Message).where(Message.account_id == account.id)
        ) == 2


def test_keep_unread_honours_mbox_status_flags(tmp_path, email):
    ids = [f"{p}-{uuid.uuid4().hex}@t" for p in ("read", "unread", "flagged")]
    box = write_mbox(
        tmp_path / "flags.mbox",
        [make_message(f"<{mid}>", f"Subject {mid[:4]}", "x@y.com", email, "body", T0)
         for mid in ids],
        flags=["RO", "", "RF"],
    )

    assert import_mbox.main([str(box), "--account", email, "--keep-unread",
                             "--no-index"]) == 0

    with SessionLocal() as db:
        account = account_row(db, email)
        inbox = folder_row(db, account, "INBOX")
        rows = db.execute(
            select(Message.message_id, MessageLocation.seen, MessageLocation.flagged)
            .join(MessageLocation, MessageLocation.message_pk == Message.id)
            .where(MessageLocation.mailbox_id == inbox.id)
        ).all()
        state = {mid: (seen, flagged) for mid, seen, flagged in rows}
        assert state[ids[0]] == (True, False)
        assert state[ids[1]] == (False, False)
        assert state[ids[2]] == (True, True)
        assert inbox.unread_count == 1


def test_folder_name_takes_its_role(tmp_path, email):
    mid = f"s-{uuid.uuid4().hex}@t"
    box = write_mbox(tmp_path / "sent.mbox", [
        make_message(f"<{mid}>", "Sent one", email, "x@y.com", "body", T0),
    ])

    assert import_mbox.main([str(box), "--account", email, "--folder", "Sent",
                             "--no-index"]) == 0

    with SessionLocal() as db:
        account = account_row(db, email)
        assert folder_row(db, account, "Sent").role == "sent"


def test_import_indexes_attachment_text_into_search(tmp_path, email):
    if not tika.health():
        pytest.skip("Tika not reachable (run: make test-up)")

    mid = f"p-{uuid.uuid4().hex}@t"
    token = f"ZEPHYR{uuid.uuid4().hex[:6].upper()}"
    box = write_mbox(tmp_path / "withpdf.mbox", [
        make_message(f"<{mid}>", "Has a PDF", "x@y.com", email, "see attached", T0,
                     pdf_text=token),
    ])

    assert import_mbox.main([str(box), "--account", email]) == 0

    with SessionLocal() as db:
        account = account_row(db, email)
        msg = db.execute(
            select(Message).where(Message.account_id == account.id)
        ).scalars().one()
        att = db.execute(
            select(Attachment).where(Attachment.message_pk == msg.id)
        ).scalars().one()
        assert att.extract_status == "done"
        assert token in (att.extracted_text or "")
        # The whole point of indexing: the PDF's text is searchable on the message.
        search_text = db.scalar(select(Message.search_text).where(Message.id == msg.id))
        assert token in search_text


def test_refuses_an_account_the_agent_syncs(tmp_path, email, monkeypatch, capsys):
    box = write_mbox(tmp_path / "x.mbox", [
        make_message(f"<{uuid.uuid4().hex}@t>", "Subject", "x@y.com", email, "b", T0),
    ])

    from core.config import AccountConfig, get_settings

    cfg = get_settings()
    monkeypatch.setattr(cfg, "accounts", [AccountConfig(email=email)])

    assert import_mbox.main([str(box), "--account", email]) == 1
    assert "Refusing to import" in capsys.readouterr().err

    with SessionLocal() as db:
        assert db.execute(
            select(Account).where(Account.email == email)
        ).scalar_one_or_none() is None


def test_default_account_is_derived_from_the_filename(tmp_path):
    assert import_mbox.default_email(Path("/tmp/Work Archive 2019.mbox")) \
        == "work-archive-2019@imported.local"
    assert import_mbox.default_email(Path("/tmp/.mbox")) == "mbox@imported.local"


def test_mbox_flags_are_read_from_both_status_headers():
    raw = b"Status: RO\r\nX-Status: AF\r\nSubject: x\r\n\r\nbody Status: D\r\n"
    flags = import_mbox.mbox_flags(raw)
    assert flags == {"seen": True, "deleted": False, "flagged": True, "answered": True}
