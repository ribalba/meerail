"""meercal's settings and the import routes, through the live server.

The client in app/meercal.py is unit-tested against a mock transport in
test_meercal_unit.py, which covers what goes over the wire and how meercal's
failures are reported. What is left for this file is what only the running
server shows. That is what reaches the page, which attachments the reader is
told are calendar files, and the gates in front of the import (configuration,
deleted mail, files that are not calendars).

The test stack has no meercal in it and runs without
server.meercal_allow_private_hosts. So every address here is either refused
before anything is sent or refused by the host check, which is itself one of
the things worth seeing from the outside.
"""

import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime

import pytest

import dbfixture
from helpers import api

URL_KEY = "meercal_url"
PASSWORD_KEY = "meercal_password"
MASK = "•" * 8
PASSWORD = "pw-" + uuid.uuid4().hex

ICS = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\nMETHOD:REQUEST\r\n"
       "BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20261015T090000Z\r\n"
       "DTSTART:20261016T120000Z\r\nDTEND:20261016T130000Z\r\nSUMMARY:Planning\r\n"
       "END:VEVENT\r\nEND:VCALENDAR\r\n")


def _write(key, value):
    from core.models import Setting

    with dbfixture.session() as db:
        row = db.get(Setting, key)
        if value is None:
            if row:
                db.delete(row)
        elif row:
            row.value = value
        else:
            db.add(Setting(key=key, value=value))


def _row(key):
    from core.models import Setting

    with dbfixture.session() as db:
        row = db.get(Setting, key)
        return row.value if row else None


@pytest.fixture
def unconfigured(require_server):
    _write(URL_KEY, None)
    _write(PASSWORD_KEY, None)
    yield
    _write(URL_KEY, None)
    _write(PASSWORD_KEY, None)


@pytest.fixture
def configured(unconfigured):
    """Saved as PUT /config would have saved it. Loopback, so the host check
    refuses it the moment anything tries to reach it."""
    from app.security import encrypt_secret

    _write(URL_KEY, "http://127.0.0.1:9")
    _write(PASSWORD_KEY, encrypt_secret(PASSWORD))
    yield


def _base(subject: str) -> EmailMessage:
    m = EmailMessage()
    m["Message-ID"] = f"<cal-{uuid.uuid4().hex}@t>"
    m["Subject"] = subject
    m["From"] = "organiser@example.com"
    m["To"] = "me@example.com"
    # Recent, so no content window can have pruned it.
    m["Date"] = format_datetime(datetime.now(timezone.utc) - timedelta(hours=1))
    return m


def outlook_invite() -> bytes:
    """The calendar part inside multipart/alternative, attached to nothing."""
    m = _base("Planning")
    m.set_content("You are invited to Planning.")
    m.add_alternative("<p>You are invited to <b>Planning</b>.</p>", subtype="html")
    m.add_alternative(ICS.format(uid=uuid.uuid4().hex), subtype="calendar",
                      params={"method": "REQUEST"})
    return m.as_bytes()


def gmail_invite() -> bytes:
    """The calendar part in the body *and* attached as invite.ics."""
    ics = ICS.format(uid=uuid.uuid4().hex)
    m = _base("Planning")
    m.set_content("You are invited to Planning.")
    m.add_alternative("<p>You are invited.</p>", subtype="html")
    m.add_alternative(ics, subtype="calendar", params={"method": "REQUEST"})
    m.add_attachment(ics.encode(), maintype="application", subtype="ics",
                     filename="invite.ics")
    return m.as_bytes()


def with_notes() -> bytes:
    m = _base("Notes")
    m.set_content("See the notes.")
    m.add_attachment(b"just text", maintype="text", subtype="plain", filename="notes.txt")
    return m.as_bytes()


def _ingest(account, raw: bytes, uid: int) -> dict:
    """Store it as the agent would, and read it back as the reader does."""
    from core.mail.parse import parse_email
    from core.models import Message

    dbfixture.ingest_raw_message(account["email"], raw, uid=uid)
    mid = parse_email(raw).message_id
    with dbfixture.session() as db:
        pk = db.query(Message.id).filter(Message.message_id == mid).scalar()
    code, detail = api("GET", f"/api/messages/{pk}")
    assert code == 200
    return detail


# --- Settings --------------------------------------------------------------


def test_nothing_is_configured_to_start_with(unconfigured):
    code, cfg = api("GET", "/api/calendar/config")
    assert code == 200
    assert cfg == {"configured": False, "url": "", "password": ""}


def test_the_password_does_not_come_back_to_the_page(configured):
    code, cfg = api("GET", "/api/calendar/config")

    assert code == 200
    assert cfg["configured"] is True
    # The address comes back whole, so it can be corrected in place. The
    # password comes back as the mask and nothing else.
    assert cfg["url"] == "http://127.0.0.1:9"
    assert cfg["password"] == MASK
    assert PASSWORD not in str(cfg)
    assert PASSWORD not in _row(PASSWORD_KEY)


def test_a_private_address_is_refused_where_it_was_typed(unconfigured):
    code, body = api("PUT", "/api/calendar/config",
                     {"url": "http://127.0.0.1:8010", "password": ""})

    assert code == 400
    assert "meercal_allow_private_hosts" in body["detail"]
    assert _row(URL_KEY) is None


def test_an_address_that_is_not_a_url_is_refused(unconfigured):
    code, body = api("PUT", "/api/calendar/config", {"url": "meercal", "password": ""})
    assert code == 400
    assert "http" in body["detail"]


def test_keeping_a_password_that_was_never_saved_is_refused(unconfigured):
    """Checked before anything is sent, so no meercal is needed to see it."""
    code, body = api("PUT", "/api/calendar/config",
                     {"url": "http://93.184.216.34:9", "password": MASK})
    assert code == 400
    assert "no saved password" in body["detail"]


def test_clearing_the_address_removes_the_password_too(configured):
    code, cfg = api("PUT", "/api/calendar/config", {"url": "", "password": MASK})

    assert code == 200
    assert cfg["configured"] is False
    assert _row(URL_KEY) is None
    assert _row(PASSWORD_KEY) is None


# --- Which attachments are calendars ---------------------------------------


def test_an_outlook_invitation_is_offered_as_a_calendar_file(account):
    m = _ingest(account, outlook_invite(), uid=1)

    cals = [a for a in m["attachments"] if a["calendar"]]
    assert len(cals) == 1
    assert cals[0]["filename"] == "invite.ics"


def test_a_gmail_invitation_is_offered_once(account):
    """Gmail sends the event twice, in the body and attached. One button."""
    m = _ingest(account, gmail_invite(), uid=2)
    assert len([a for a in m["attachments"] if a["calendar"]]) == 1


def test_an_ordinary_attachment_is_not_a_calendar(account):
    m = _ingest(account, with_notes(), uid=3)
    assert [a["calendar"] for a in m["attachments"]] == [False]


# --- The gates in front of the import --------------------------------------


def test_nothing_is_imported_while_meercal_is_not_set_up(unconfigured, account):
    att = _ingest(account, outlook_invite(), uid=4)["attachments"][0]

    code, body = api("GET", f"/api/calendar/attachments/{att['id']}")
    assert code == 409
    code, body = api("POST", "/api/calendar/import",
                     {"attachment_id": att["id"], "calendar_id": 1})
    assert code == 409
    assert "Settings" in body["detail"]


def test_a_file_that_is_not_a_calendar_is_refused(configured, account):
    att = _ingest(account, with_notes(), uid=5)["attachments"][0]

    code, body = api("GET", f"/api/calendar/attachments/{att['id']}")
    assert code == 400
    code, body = api("POST", "/api/calendar/import",
                     {"attachment_id": att["id"], "calendar_id": 1})
    assert code == 400


def test_an_unknown_attachment_is_not_found(configured):
    code, _ = api("GET", "/api/calendar/attachments/987654321")
    assert code == 404


def test_deleted_mail_cannot_be_sent_to_meercal(configured, account):
    """The same gate as reading it. Deleted mail must not leave this server."""
    from core.models import Attachment, MessageLocation

    att = _ingest(account, outlook_invite(), uid=6)["attachments"][0]
    with dbfixture.session() as db:
        pk = db.get(Attachment, att["id"]).message_pk
        for loc in db.query(MessageLocation).filter(MessageLocation.message_pk == pk):
            loc.deleted = True

    code, _ = api("GET", f"/api/calendar/attachments/{att['id']}")
    assert code == 404
    code, _ = api("POST", "/api/calendar/import", {"attachment_id": att["id"], "calendar_id": 1})
    assert code == 404


def test_a_calendar_file_gets_as_far_as_the_host_check(configured, account):
    """Every gate of ours passed, and the one left is the address. The answer
    is a 502 that names the setting, not a 500."""
    att = _ingest(account, outlook_invite(), uid=7)["attachments"][0]

    code, body = api("GET", f"/api/calendar/attachments/{att['id']}")
    assert code == 502
    assert "meercal_allow_private_hosts" in body["detail"]


def test_a_calendar_id_must_be_a_real_one(configured):
    code, _ = api("POST", "/api/calendar/import", {"attachment_id": 1, "calendar_id": 0})
    assert code == 422
