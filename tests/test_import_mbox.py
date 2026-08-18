"""The mbox importer (tools/import_mbox.py).

Database-only: the importer is agent-side work — it writes through core.ingest
and never touches the web app — so these run without a server, unlike most of
the integration suite. Tika is used when it is up (attachment text) and the one
case that needs it skips when it is not.
"""

from __future__ import annotations

import mailbox
import plistlib
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


def write_emlx(path: Path, raw: bytes, flags: int = 0) -> Path:
    """One Apple Mail message file: byte count, message, plist of Mail's state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    trailer = plistlib.dumps({"flags": flags, "date-sent": 0})
    path.write_bytes(f"{len(raw)}\n".encode() + raw + trailer)
    return path


def apple_mailbox(root: Path, messages: list[bytes], flags: list[int] | None = None,
                  name: str = "Verteiler.mbox") -> Path:
    """Apple Mail's on-disk layout: <Folder>.mbox/<UUID>/Data/<n>/Messages/*.emlx."""
    box = root / name
    data = box / "5B1F0C22-0000-4000-8000-000000000001" / "Data" / "3"
    for n, raw in enumerate(messages):
        write_emlx(data / "Messages" / f"{n + 1}.emlx", raw,
                   flags[n] if flags else 0)
    return box


def split_attachments(raw: bytes, emlx: Path) -> None:
    """Write a message the way Mail stores one that has attachments.

    Every attachment part is emptied and marked X-Apple-Content-Length, its
    bytes going to Attachments/<n>/<part>/<filename> beside the Messages dir —
    which is what makes the file .partial.emlx.
    """
    msg = message_from_bytes(raw)
    attachments = [p for p in msg.walk() if p.get_filename()]
    for n, part in enumerate(attachments, start=2):
        blob = part.get_payload(decode=True)
        target = (emlx.parent.parent / "Attachments" / emlx.name.split(".")[0]
                  / str(n) / part.get_filename())
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
        part.set_payload("")
        part["X-Apple-Content-Length"] = str(len(blob))
    write_emlx(emlx, msg.as_bytes())


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


def test_a_drain_that_stops_early_says_what_is_still_queued(
        tmp_path, email, monkeypatch, capsys):
    """extract_pending returns 0 both for "queue empty" and for "Tika stopped
    answering", so a run that gave up half way through a backlog printed the
    same "Indexed N attachment(s)" as one that finished. The leftovers then came
    out during the *next* import — which is why importing a single message
    looked like it was re-indexing an archive imported an hour earlier."""
    box = write_mbox(tmp_path / "withpdf.mbox", [
        make_message(f"<{uuid.uuid4().hex}@t>", "Has a PDF", "x@y.com", email,
                     "see attached", T0, pdf_text="STOPPEDEARLY"),
    ])

    from core import ingest

    # Tika is up as far as the tool can tell, and the queue still does not move:
    # exactly what a container that starts refusing connections mid-drain looks
    # like from here.
    monkeypatch.setattr(tika, "health", lambda: True)
    monkeypatch.setattr(ingest, "extract_pending", lambda db, *a, **k: 0)

    assert import_mbox.main([str(box), "--account", email]) == 0

    out = capsys.readouterr().out
    assert "Indexed 0 attachment(s)" in out
    assert "still queued for text extraction" in out


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


def test_apple_mail_directory_imports_its_emlx_files(tmp_path, email):
    """~/Library/Mail/V10/<account>/<Folder>.mbox is a directory, and there is no
    mbox file anywhere inside it — every message is its own .emlx. Pointing the
    tool at one used to reach mailbox.mbox() and die on IsADirectoryError."""
    a, b = (f"{p}-{uuid.uuid4().hex}@t" for p in ("a", "b"))
    box = apple_mailbox(tmp_path, [
        make_message(f"<{a}>", "Subject GAMMA", "x@y.com", email, "the body", T0),
        make_message(f"<{b}>", "Re: Subject GAMMA", "z@y.com", email, "a reply",
                     T0 + timedelta(hours=1), in_reply_to=f"<{a}>", refs=[f"<{a}>"]),
    ])

    assert import_mbox.main([str(box), "--account", email, "--folder", "Verteiler",
                             "--no-index"]) == 0

    with SessionLocal() as db:
        account = account_row(db, email)
        folder = folder_row(db, account, "Verteiler")
        assert folder.total_count == 2
        msgs = db.execute(
            select(Message).where(Message.account_id == account.id)
        ).scalars().all()
        assert {m.message_id for m in msgs} == {a, b}
        assert len({m.thread_id for m in msgs}) == 1
        assert all(m.content_status == "full" for m in msgs)

    # And again: the same directory imports nothing twice.
    assert import_mbox.main([str(box), "--account", email, "--folder", "Verteiler",
                             "--no-index"]) == 0
    with SessionLocal() as db:
        account = account_row(db, email)
        assert folder_row(db, account, "Verteiler").total_count == 2


def test_keep_unread_honours_the_emlx_plist_flags(tmp_path, email):
    ids = [f"{p}-{uuid.uuid4().hex}@t" for p in ("read", "unread", "flagged")]
    box = apple_mailbox(
        tmp_path,
        [make_message(f"<{mid}>", f"Subject {mid[:4]}", "x@y.com", email, "body", T0)
         for mid in ids],
        flags=[0b1, 0, 0b10001],       # read; nothing; read + flagged
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


def test_partial_emlx_gets_its_attachment_back(tmp_path, email):
    """Mail keeps attachments beside the message, not in it. Without splicing
    them back an imported archive is a table of contents: right subjects, right
    threads, and every PDF in it zero bytes long."""
    mid = f"p-{uuid.uuid4().hex}@t"
    token = f"ZEPHYR{uuid.uuid4().hex[:6].upper()}"
    raw = make_message(f"<{mid}>", "Has a PDF", "x@y.com", email, "see attached", T0,
                       pdf_text=token)
    pdf = message_from_bytes(raw).get_payload(1).get_payload(decode=True)

    box = tmp_path / "WithPDF.mbox"
    split_attachments(raw, box / "UUID" / "Data" / "1" / "Messages" / "9.partial.emlx")

    assert import_mbox.main([str(box), "--account", email, "--no-index"]) == 0

    with SessionLocal() as db:
        account = account_row(db, email)
        msg = db.execute(
            select(Message).where(Message.account_id == account.id)
        ).scalars().one()
        att = db.execute(
            select(Attachment).where(Attachment.message_pk == msg.id)
        ).scalars().one()
        assert att.filename == "report.pdf"
        assert att.size_bytes == len(pdf)


def test_sub_mailboxes_come_in_as_folders_of_their_own(tmp_path, email):
    """A mailbox with children keeps them inside itself, nested as deep as they
    were filed, and one command brings the lot in — but each into its own folder.
    Sweeping every .emlx under the directory into one would file a parent's and a
    child's mail together, where nothing could ever separate it again."""
    parent, child, deep = (f"{p}-{uuid.uuid4().hex}@t"
                           for p in ("parent", "child", "deep"))
    box = apple_mailbox(tmp_path, [
        make_message(f"<{parent}>", "Parent mail", "x@y.com", email, "body", T0),
    ], name="Lists.mbox")
    announce = apple_mailbox(box, [
        make_message(f"<{child}>", "Child mail", "x@y.com", email, "body", T0),
    ], name="Announce.mbox")
    apple_mailbox(announce, [
        make_message(f"<{deep}>", "Deep mail", "x@y.com", email, "body", T0),
    ], name="2019.mbox")

    assert import_mbox.main([str(box), "--account", email, "--folder", "Lists",
                             "--no-index"]) == 0

    with SessionLocal() as db:
        account = account_row(db, email)
        assert folder_row(db, account, "Lists").total_count == 1
        assert folder_row(db, account, "Lists/Announce").total_count == 1
        assert folder_row(db, account, "Lists/Announce/2019").total_count == 1
        placed = db.execute(
            select(Message.message_id, Mailbox.imap_name)
            .join(MessageLocation, MessageLocation.message_pk == Message.id)
            .join(Mailbox, Mailbox.id == MessageLocation.mailbox_id)
            .where(Message.account_id == account.id)
        ).all()
        assert dict(placed) == {parent: "Lists", child: "Lists/Announce",
                                deep: "Lists/Announce/2019"}

    # And again: a tree re-imports nothing twice either.
    assert import_mbox.main([str(box), "--account", email, "--folder", "Lists",
                             "--no-index"]) == 0
    with SessionLocal() as db:
        account = account_row(db, email)
        assert db.scalar(
            select(func.count()).select_from(Message).where(Message.account_id == account.id)
        ) == 3


def test_no_recurse_imports_only_the_mailbox_named(tmp_path, email):
    parent, child = (f"{p}-{uuid.uuid4().hex}@t" for p in ("parent", "child"))
    box = apple_mailbox(tmp_path, [
        make_message(f"<{parent}>", "Parent mail", "x@y.com", email, "body", T0),
    ], name="Lists.mbox")
    apple_mailbox(box, [
        make_message(f"<{child}>", "Child mail", "x@y.com", email, "body", T0),
    ], name="Announce.mbox")

    assert import_mbox.main([str(box), "--account", email, "--folder", "Lists",
                             "--no-recurse", "--no-index"]) == 0

    with SessionLocal() as db:
        account = account_row(db, email)
        assert db.execute(
            select(Message.message_id).where(Message.account_id == account.id)
        ).scalars().all() == [parent]
        assert db.execute(
            select(Mailbox).where(Mailbox.account_id == account.id,
                                  Mailbox.imap_name == "Lists/Announce")
        ).scalar_one_or_none() is None


def test_a_mailbox_holding_only_sub_mailboxes_names_the_folder_they_land_under(
        tmp_path, email):
    """01-GCS.mbox holds nothing but mailboxes — the shape most of an Apple Mail
    account's top level has. It contributes no mail of its own, and with no
    --folder to hang them off its own name is the one that makes sense: INBOX/API
    Errors is not where anybody filed anything.

    The container does get a folder row, holding nothing. It used not to, back
    when the sidebar drew a flat list and an empty row was pure furniture — but
    the sidebar nests now, and the parent is what the nesting is drawn from
    (app/routers/mailboxes.py::_chains). Without it the two children come back
    as two unrelated top-level folders called "API Errors" and "DNS Errors",
    which is precisely the grouping this import was preserving."""
    one, two = (f"{p}-{uuid.uuid4().hex}@t" for p in ("one", "two"))
    box = tmp_path / "01-GCS.mbox"
    box.mkdir()
    (box / "Info.plist").write_bytes(b"<plist/>")
    apple_mailbox(box, [
        make_message(f"<{one}>", "API mail", "x@y.com", email, "body", T0),
    ], name="API Errors.mbox")
    apple_mailbox(box, [
        make_message(f"<{two}>", "DNS mail", "x@y.com", email, "body", T0),
    ], name="DNS Errors.mbox")

    assert import_mbox.main([str(box), "--account", email, "--no-index"]) == 0

    with SessionLocal() as db:
        account = account_row(db, email)
        assert folder_row(db, account, "01-GCS/API Errors").total_count == 1
        assert folder_row(db, account, "01-GCS/DNS Errors").total_count == 1
        # The container itself: present so the two hang off something, empty
        # because nothing was filed in it.
        container = db.execute(
            select(Mailbox).where(Mailbox.account_id == account.id,
                                  Mailbox.imap_name == "01-GCS")
        ).scalar_one()
        assert container.total_count == 0
        assert container.local is True


def test_a_lone_sub_mailbox_still_hangs_off_its_parents_name(tmp_path):
    """One child rather than six: the folder still comes from the mailbox named,
    not from INBOX, because what was pointed at holds no mail of its own."""
    box = tmp_path / "01-GCS.mbox"
    box.mkdir()
    apple_mailbox(box, [
        make_message("<one@t>", "API mail", "x@y.com", "a@b.c", "body", T0),
    ], name="API Errors.mbox")

    targets = import_mbox._targets(box, None, recurse=True)
    assert [name for name, _ in targets] == ["01-GCS/API Errors"]


def test_a_quoted_path_keeps_its_tilde_and_is_expanded_here(tmp_path, email, monkeypatch):
    """A mailbox name with a space in it invites quotes, and a quoted ~ is one
    the shell never expands — so 'x/My Mail.mbox' arrived as a literal ~ and the
    import died on a path that does not exist."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    mid = f"t-{uuid.uuid4().hex}@t"
    apple_mailbox(home, [
        make_message(f"<{mid}>", "Tilde mail", "x@y.com", email, "body", T0),
    ], name="My Mail.mbox")

    assert import_mbox.main(["~/My Mail.mbox", "--account", email,
                             "--folder", "My Mail", "--no-index"]) == 0

    with SessionLocal() as db:
        account = account_row(db, email)
        assert folder_row(db, account, "My Mail").total_count == 1


def test_a_path_that_is_not_there_says_so(tmp_path):
    with pytest.raises(SystemExit) as exc:
        import_mbox.main([str(tmp_path / "nope.mbox"), "--no-index"])
    assert "no such file or directory" in str(exc.value)


def test_exported_mailbox_folder_reads_the_mbox_inside(tmp_path, email):
    """Mail.app's Mailbox > Export Mailbox writes a .mbox *folder* with the real
    mbox in it under the name "mbox"."""
    mid = f"e-{uuid.uuid4().hex}@t"
    box = tmp_path / "Exported.mbox"
    box.mkdir()
    (box / "table_of_contents").write_bytes(b"\x00\x01")
    write_mbox(box / "mbox", [
        make_message(f"<{mid}>", "Exported one", "x@y.com", email, "body", T0),
    ])

    assert import_mbox.main([str(box), "--account", email, "--no-index"]) == 0

    with SessionLocal() as db:
        account = account_row(db, email)
        assert folder_row(db, account, "INBOX").total_count == 1


def test_an_account_directory_names_the_mailboxes_it_holds(tmp_path, capsys):
    """~/Library/Mail/V10/<account-id> is one directory up from anything
    importable, and is what someone lands on first."""
    account_dir = tmp_path / "F34D123C-84B6-44E7-B833-2F2A7CBFE702"
    apple_mailbox(account_dir, [
        make_message(f"<{uuid.uuid4().hex}@t>", "s", "x@y.com", "a@b.c", "b", T0),
    ], name="Verteiler.mbox")

    with pytest.raises(SystemExit) as exc:
        import_mbox.main([str(account_dir), "--no-index"])
    assert "Verteiler.mbox" in str(exc.value)

    empty = tmp_path / "nothing-here"
    empty.mkdir()
    with pytest.raises(SystemExit) as exc:
        import_mbox.main([str(empty), "--no-index"])
    assert "no .emlx messages" in str(exc.value)


def test_emlx_without_a_length_header_is_read_as_a_plain_message(tmp_path):
    raw = make_message("<x@t>", "Subject", "x@y.com", "a@b.c", "body", T0)
    path = tmp_path / "1.emlx"
    path.write_bytes(raw)
    assert import_mbox.read_emlx(path) == (raw, {})


def test_emlx_flags_come_from_the_plist_bitfield():
    assert import_mbox.emlx_flags({"flags": 0b10111}) == {
        "seen": True, "deleted": True, "answered": True, "flagged": True,
    }
    assert import_mbox.emlx_flags({}) == {
        "seen": False, "deleted": False, "answered": False, "flagged": False,
    }


def test_mbox_flags_are_read_from_both_status_headers():
    raw = b"Status: RO\r\nX-Status: AF\r\nSubject: x\r\n\r\nbody Status: D\r\n"
    flags = import_mbox.mbox_flags(raw)
    assert flags == {"seen": True, "deleted": False, "flagged": True, "answered": True}
