"""Filing mail sent before the agent did it (tools/file_sent.py).

Database-only, like the importer's tests: the tool writes queue rows and never
talks to a mail server, so these run without one. What they pin is which sent
messages get a ``save_sent`` row and which are left alone, because the one
failure that matters here is a second copy of a message in someone's Sent
folder, and the rows this tool writes are what the agent acts on.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import file_sent
import pytest
from sqlalchemy import select

import dbfixture
from core.config import AccountConfig, get_settings
from core.database import SessionLocal
from core.models import Outbound, PendingAction
from helpers import make_message

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def email() -> str:
    return f"sent-{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture
def account_id(email) -> int:
    return dbfixture.create_account(email)["id"]


@pytest.fixture
def configured(email, monkeypatch):
    """The account as meerail.toml would list it, so main() walks it."""
    monkeypatch.setattr(get_settings(), "accounts", [AccountConfig(email=email)])


def sent_mail(account_id: int, email: str, subject: str, *, state: str = "sent",
              mid: str | None = "", crlf: bool = False, with_mime: bool = True,
              when: datetime = T0, to: str = "bob@example.org",
              cc: list[str] | None = None, bcc: list[str] | None = None) -> tuple[int, bytes]:
    """An Outbound row as a finished send leaves one. Returns (id, raw bytes).

    ``mid=""`` makes up a Message-ID; ``None`` writes the message without one.
    The MIME is stored as text, the way the composer stores it, with bare LF
    line endings unless ``crlf`` asks for the other spelling.
    """
    if mid == "":
        mid = f"<out-{uuid.uuid4().hex}@example.com>"
    raw = make_message(mid, subject, email, to, "the body", when)
    text = raw.decode()
    if crlf:
        text = text.replace("\n", "\r\n")
    with dbfixture.session() as db:
        row = Outbound(account_id=account_id, state=state, to_addrs=[to],
                       cc_addrs=cc or [], bcc_addrs=bcc or [], subject=subject,
                       raw_mime=text if with_mime else None,
                       sent_at=when.replace(tzinfo=None) if state == "sent" else None)
        db.add(row)
        db.flush()
        return row.id, raw


def queue_rows(account_id: int) -> list[dict]:
    """Every queue row on the account, whatever its type, oldest first."""
    with SessionLocal() as db:
        rows = db.execute(
            select(PendingAction).where(PendingAction.account_id == account_id)
            .order_by(PendingAction.id)
        ).scalars().all()
        return [{"type": a.type, "status": a.status, "message_pk": a.message_pk,
                 "payload": a.payload} for a in rows]


def run(email: str, account_id: int, apply: bool, save_sent: bool | None = None):
    acc = SimpleNamespace(email=email, save_sent=save_sent)
    with SessionLocal() as db:
        return file_sent._file_account(db, acc, account_id, apply)


def test_a_dry_run_queues_nothing(email, account_id, configured, capsys):
    """Without --apply the tool reports and writes nothing at all, which is
    the promise every tool in tools/ makes about being run to see what it
    would do."""
    sent_mail(account_id, email, "Quarterly report")

    assert file_sent.main(["-a", email]) == 0

    assert queue_rows(account_id) == []
    out = capsys.readouterr().out
    assert "'Quarterly report' -> would queue a copy into Sent" in out
    assert "2026-03-01" in out
    assert "Re-run with --apply" in out


def test_apply_queues_one_copy_and_a_second_run_queues_nothing(
        email, account_id, configured, capsys):
    """One save_sent row per sent message, carrying what the agent needs to
    find the MIME and the envelope it went out with, and never a second.

    A second run is the case that matters most: someone runs the tool, is not
    sure it worked, and runs it again. The row from the first run is found by
    its outbound id whatever state it is in, including "done", where the
    agent has already filed the message and another row would be another copy
    in Sent the next time the agent's own check came up empty.
    """
    ob, _ = sent_mail(account_id, email, "Quarterly report",
                      cc=["carol@example.org"], bcc=["dave@example.org"])

    assert file_sent.main(["--apply", "-a", email]) == 0

    rows = queue_rows(account_id)
    assert rows == [{
        "type": "save_sent", "status": "pending", "message_pk": None,
        "payload": {"outbound_id": ob,
                    "rcpt_to": ["bob@example.org", "carol@example.org",
                                "dave@example.org"]},
    }]
    out = capsys.readouterr().out
    assert "-> queued" in out
    assert "next pass" in out

    assert file_sent.main(["--apply", "-a", email]) == 0
    assert len(queue_rows(account_id)) == 1
    assert "already queued (pending)" in capsys.readouterr().out

    with SessionLocal() as db:
        for action in db.execute(select(PendingAction).where(
                PendingAction.account_id == account_id)).scalars():
            action.status = "done"
        db.commit()

    assert file_sent.main(["--apply", "-a", email]) == 0
    assert len(queue_rows(account_id)) == 1
    assert "already queued (done)" in capsys.readouterr().out


def test_a_message_already_in_sent_is_left_alone(email, account_id, capsys):
    """On a server that files its own copies (Proton, Gmail) the copy it made
    has been synced in like any other mail, and the database already knows
    it is in Sent. Queueing it anyway would cost an agent pass per message to
    rediscover that.

    Only a folder with role "sent" counts. The second message here was also
    sent to its author, so the same Message-ID sits in the Inbox, and that is
    no copy in Sent at all: it is queued.

    The first is stored with CRLF line endings, so the Message-ID has to be
    read out of a header block that ends in "\\r\\n\\r\\n" rather than the
    composer's "\\n\\n".
    """
    dbfixture.create_folder(email, "Sent", "\\Sent")
    filed, filed_raw = sent_mail(account_id, email, "Already filed", crlf=True)
    dbfixture.ingest_raw_message(email, filed_raw, uid=1, folder="Sent")
    to_self, to_self_raw = sent_mail(account_id, email, "Note to self", to=email,
                                     when=T0 + timedelta(hours=1))
    dbfixture.ingest_raw_message(email, to_self_raw, uid=1, folder="INBOX")

    assert run(email, account_id, apply=True) == (1, 1)

    rows = queue_rows(account_id)
    assert [r["payload"]["outbound_id"] for r in rows] == [to_self]
    assert filed not in [r["payload"]["outbound_id"] for r in rows]
    assert "'Already filed' -> already in Sent, left alone" in capsys.readouterr().out


def test_an_account_configured_not_to_file_sent_copies_is_skipped(
        email, account_id, capsys):
    """``save_sent = false`` is the operator saying copies are not to be filed
    for this account, and the agent obeys it for every send it makes. A
    backfill that went ahead regardless would put back the very copies the
    setting exists to keep out."""
    sent_mail(account_id, email, "Quarterly report")

    assert run(email, account_id, apply=True, save_sent=False) == (0, 0)

    assert queue_rows(account_id) == []
    assert "save_sent = false" in capsys.readouterr().out


def test_only_sent_mail_with_its_mime_is_a_candidate(email, account_id, capsys):
    """Mail that has not gone out yet is not sent mail: a queued or held
    message is still in the Outbox, where the agent files it after it sends
    it, and a draft was never sent at all. A sent row whose MIME is gone has
    nothing to file. None of these may produce a row."""
    sent_mail(account_id, email, "Still queued", state="queued")
    sent_mail(account_id, email, "Held back", state="held")
    sent_mail(account_id, email, "Half written", state="draft")
    sent_mail(account_id, email, "Sent, MIME gone", with_mime=False)

    assert run(email, account_id, apply=True) == (0, 0)

    assert queue_rows(account_id) == []
    assert "no sent mail with its MIME still held" in capsys.readouterr().out


def test_a_message_without_a_message_id_is_named_not_filed(email, account_id, capsys):
    """Without a Message-ID there is nothing to look the message up by, in the
    database or on the server, so nothing can tell whether a copy is already
    in Sent. The tool names it instead of filing it on a guess."""
    sent_mail(account_id, email, "Anonymous", mid=None)

    assert run(email, account_id, apply=True) == (0, 1)

    assert queue_rows(account_id) == []
    assert "'Anonymous' -> no Message-ID, cannot check Sent" in capsys.readouterr().out


def test_header_block_ends_at_the_first_blank_line_in_either_spelling():
    """A CRLF message holds no "\\n\\n" until its body, and a bare-LF one may
    carry a "\\r\\n\\r\\n" in its body; the earlier of the two is the end of
    the headers either way."""
    assert file_sent._header_block("A: 1\nB: 2\n\nbody\r\n\r\nmore") == b"A: 1\nB: 2"
    assert file_sent._header_block("A: 1\r\nB: 2\r\n\r\nbody\n\nmore") == b"A: 1\r\nB: 2"
    assert file_sent._header_block("A: 1\nB: 2") == b"A: 1\nB: 2"
