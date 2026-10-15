"""Filing calendar invitations the old parser missed (tools/reparse_forwards.py --calendar).

Database-only, like the other tool tests. An Outlook or Exchange invitation
carries its event only as a ``text/calendar`` alternative of the body, and the
parser used to store no attachment for it. ``--calendar`` finds those messages
by their stored raw bytes and re-reads them. What these pin is that the byte
search really runs on Postgres (raw_mime is bytea, which has no ILIKE), that it
picks out invitations in any spelling of the type and nothing else, and that a
re-read files the missing invitation once without touching a Gmail one that was
stored correctly all along.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime

import pytest
from sqlalchemy import delete, select

import dbfixture
import reparse_forwards
from core.database import SessionLocal
from core.models import Account, Attachment, Message
from helpers import make_message

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)

_VCALENDAR = (
    b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\n"
    b"UID:reparse-calendar-test\r\nDTSTART:20260310T090000Z\r\n"
    b"DTEND:20260310T093000Z\r\nSUMMARY:Planning\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)


@pytest.fixture
def email() -> str:
    return f"calendar-{uuid.uuid4().hex[:10]}@example.com"


def _invite(mid: str, subject: str) -> EmailMessage:
    """The Outlook and Exchange shape: the event is a third alternative."""
    m = EmailMessage()
    m["Message-ID"] = mid
    m["Subject"] = subject
    m["From"] = "Organizer <org@example.com>"
    m["To"] = "me@proton.me"
    m["Date"] = format_datetime(T0)
    m.set_content("you are invited")
    m.add_alternative("<p>you are invited</p>", subtype="html")
    m.add_alternative(_VCALENDAR, maintype="text", subtype="calendar",
                      params={"method": "REQUEST", "charset": "utf-8"})
    return m


def _pk(email: str, subject: str) -> int:
    with SessionLocal() as db:
        return db.execute(select(Message.id).join(Account).where(
            Account.email == email, Message.subject == subject)).scalar_one()


def _files(message_pk: int) -> list[tuple[str, str, bool]]:
    with SessionLocal() as db:
        return sorted(db.execute(
            select(Attachment.filename, Attachment.content_type, Attachment.is_inline)
            .where(Attachment.message_pk == message_pk)).tuples().all())


def _as_the_old_parser_left_it(message_pk: int) -> None:
    """Drop the invitation the new parser filed, which is the state every
    Outlook invitation stored before the fix is in."""
    with dbfixture.session() as db:
        db.execute(delete(Attachment).where(Attachment.message_pk == message_pk))
        db.get(Message, message_pk).has_attachments = False


@pytest.fixture
def seeded(email) -> dict[str, int]:
    outlook = _invite("<outlook-invite@x>", "outlook invite").as_bytes()
    # MIME types are case-insensitive, and a byte match would miss this one.
    shouting = _invite("<shouting-invite@x>", "shouting invite").as_bytes().replace(
        b"text/calendar", b"Text/Calendar")
    assert b"text/calendar" not in shouting
    gmail = _invite("<gmail-invite@x>", "gmail invite")
    gmail.add_attachment(_VCALENDAR, maintype="application", subtype="ics",
                         filename="invite.ics")
    plain = make_message("<plain@x>", "plain mail", "a@b.com", "me@proton.me",
                         "nothing to see", T0, text_attachment="notes\n")
    for uid, raw in enumerate([outlook, shouting, gmail.as_bytes(), plain], 1):
        dbfixture.ingest_raw_message(email, raw, uid=uid)
    pks = {"outlook": _pk(email, "outlook invite"),
           "shouting": _pk(email, "shouting invite"),
           "gmail": _pk(email, "gmail invite"),
           "plain": _pk(email, "plain mail")}
    _as_the_old_parser_left_it(pks["outlook"])
    _as_the_old_parser_left_it(pks["shouting"])
    return pks


def _run(monkeypatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["reparse_forwards.py", *args])
    return reparse_forwards.main()


def test_calendar_candidates_are_found_in_the_raw_bytes(seeded):
    with SessionLocal() as db:
        ids = set(reparse_forwards._candidates(db, every=False, calendar=True))
    assert {seeded["outlook"], seeded["shouting"], seeded["gmail"]} <= ids
    assert seeded["plain"] not in ids


def test_calendar_dry_run_reports_without_writing(seeded, monkeypatch, capsys):
    assert _run(monkeypatch, "--calendar") == 0
    out = capsys.readouterr().out
    assert f"#{seeded['outlook']} " in out
    assert f"#{seeded['shouting']} " in out
    assert f"#{seeded['gmail']} " not in out          # already correct
    assert "Dry run" in out
    assert _files(seeded["outlook"]) == []


def test_calendar_apply_files_the_missing_invitation_once(seeded, monkeypatch):
    gmail_before = _files(seeded["gmail"])
    assert _run(monkeypatch, "--calendar", "--apply") == 0
    for name in ("outlook", "shouting"):
        assert _files(seeded[name]) == [("invite.ics", "text/calendar", False)]
        with SessionLocal() as db:
            msg = db.get(Message, seeded[name])
            assert msg.has_attachments
            assert msg.raw_mime            # the bytes the re-read came from are kept
    assert _files(seeded["gmail"]) == gmail_before
    assert [f for f in gmail_before if f[1] in ("text/calendar", "application/ics")] == [
        ("invite.ics", "application/ics", False)]
    assert _files(seeded["plain"]) == [("notes.txt", "text/plain", False)]


def test_calendar_and_all_cannot_be_combined(monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "--calendar", "--all")
    assert exc.value.code == 2
