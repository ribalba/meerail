"""Unit coverage for the meercal client (app/meercal.py).

Pure unit test: no server, no database, no meercal. The requests are built by a
real httpx client and answered by a MockTransport, so what is checked is what
would actually cross the wire. That matters most for the import, whose
multipart field names have to be the ones meercal's FastAPI route reads
(`file` and `calendar_id`) or the event silently goes nowhere.
"""

import email
import email.policy

import httpx
import pytest

import app.meercal as meercal
from app.meercal import (
    BlockedHost, MeercalError, NoAnswer, Refused, fetch_calendars, import_ics,
    parse_base, preview, writable_calendars,
)

BASE = "http://meercal.example:8010"
ICS = (b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:abc@example\r\n"
       b"DTSTART:20261016T120000Z\r\nDTEND:20261016T130000Z\r\nSUMMARY:Standup\r\n"
       b"END:VEVENT\r\nEND:VCALENDAR\r\n")

STATE = {
    "accounts": [
        {"id": 1, "label": "Imported", "kind": "local"},
        {"id": 2, "label": "Fastmail", "kind": "fastmail"},
    ],
    "calendars": [
        {"id": 10, "account_id": 2, "name": "Work", "color": "#3b82f6", "read_only": False},
        {"id": 11, "account_id": 2, "name": "Holidays", "color": "#ef4444", "read_only": True},
        {"id": 12, "account_id": 1, "name": "Trips", "color": "red;background:url(x)",
         "read_only": False},
    ],
}


@pytest.fixture
def wire(monkeypatch):
    """Route every httpx.Client this module makes through a handler, and skip
    the host check, which has tests of its own below."""
    seen = []
    answers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = answers.get((request.method, request.url.path))
        if answer is None:
            return httpx.Response(404, json={"detail": "Not found"})
        if isinstance(answer, Exception):
            raise answer
        return answer

    real = httpx.Client

    class Client(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", Client)
    monkeypatch.setattr(meercal, "pinned", lambda base: (base, {}))
    return seen, answers


def _form(request: httpx.Request) -> dict:
    """The multipart body as {field: (filename, bytes)}."""
    head = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    msg = email.message_from_bytes(head + request.content, policy=email.policy.HTTP)
    out = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        out[name] = (part.get_filename(), part.get_payload(decode=True))
    return out


# --- The address -----------------------------------------------------------


def test_the_address_is_reduced_to_its_base():
    assert parse_base("http://meercal.example:8010/") == BASE
    # Pasted from the address bar of an open meercal, view and all.
    assert parse_base("  http://meercal.example:8010/#/week/2026-10-15 ") == BASE
    assert parse_base("https://host.example/cal/?x=1") == "https://host.example/cal"


@pytest.mark.parametrize("raw", ["", "meercal.example:8010", "ftp://host", "http://"])
def test_an_address_that_is_not_a_url_is_refused(raw):
    with pytest.raises(ValueError):
        parse_base(raw)


def test_a_password_in_the_address_is_refused():
    """It would end up in a row stored in the clear and shown on the page."""
    with pytest.raises(ValueError, match="password field"):
        parse_base("http://user:secret@meercal.example:8010")


# --- Calendars -------------------------------------------------------------


def test_only_writable_calendars_are_offered_each_with_its_account():
    cals = writable_calendars(STATE)
    assert [c["id"] for c in cals] == [10, 12]
    assert cals[0] == {"id": 10, "name": "Work", "color": "#3b82f6", "account": "Fastmail"}
    assert cals[1]["account"] == "Imported"


def test_a_colour_that_is_not_a_colour_is_dropped():
    """It goes into the page's markup, and it came from another program."""
    assert writable_calendars(STATE)[1]["color"] == ""


def test_odd_state_does_not_break_the_list():
    state = {"accounts": None, "calendars": [None, {"id": "x"}, {"id": "7"}]}
    assert writable_calendars(state) == [
        {"id": 7, "name": "Calendar 7", "color": "", "account": ""}]


def test_the_password_goes_as_a_bearer_and_only_when_there_is_one(wire):
    seen, answers = wire
    answers[("GET", "/api/state")] = httpx.Response(200, json=STATE)

    fetch_calendars(BASE, "pw")
    fetch_calendars(BASE, "")

    assert seen[0].headers["authorization"] == "Bearer pw"
    assert "authorization" not in seen[1].headers


def test_the_pins_host_header_survives_the_password(monkeypatch, wire):
    """The pin rewrites the URL to an address and carries the real name in a
    Host header. Adding the password must not throw that header away."""
    seen, answers = wire
    answers[("GET", "/api/state")] = httpx.Response(200, json=STATE)
    monkeypatch.setattr(meercal, "pinned", lambda base: (
        "http://203.0.113.5:8010", {"headers": {"Host": "meercal.example:8010"}}))

    fetch_calendars(BASE, "pw")

    assert seen[0].headers["host"] == "meercal.example:8010"
    assert seen[0].headers["authorization"] == "Bearer pw"


def test_an_address_without_meercals_api_says_so(wire):
    with pytest.raises(MeercalError, match="no meercal API"):
        fetch_calendars(BASE, "")


def test_something_that_is_not_meercal_says_so(wire):
    _, answers = wire
    answers[("GET", "/api/state")] = httpx.Response(200, text="<html>hello</html>")
    with pytest.raises(MeercalError, match="not the way meercal does"):
        fetch_calendars(BASE, "")
    answers[("GET", "/api/state")] = httpx.Response(200, json={"ok": True})
    with pytest.raises(MeercalError, match="not the way meercal does"):
        fetch_calendars(BASE, "")


# --- Failures --------------------------------------------------------------


def test_a_wrong_password_and_a_missing_one_are_told_apart(wire):
    _, answers = wire
    answers[("GET", "/api/state")] = httpx.Response(401, json={"detail": "Not signed in"})
    with pytest.raises(MeercalError, match="did not accept the password"):
        fetch_calendars(BASE, "wrong")
    with pytest.raises(MeercalError, match="is password-protected"):
        fetch_calendars(BASE, "")


def test_meercals_own_refusal_is_passed_on(wire):
    """A read-only calendar, or meercal refusing a password over plain HTTP.
    Its own sentence already says what to do."""
    _, answers = wire
    answers[("POST", "/api/import")] = httpx.Response(403, json={"detail": "Holidays is read-only"})
    with pytest.raises(Refused, match="Holidays is read-only") as caught:
        import_ics(BASE, "", 11, "invite.ics", ICS)
    assert caught.value.status == 403


def test_a_redirect_is_named_rather_than_followed(wire):
    _, answers = wire
    answers[("GET", "/api/state")] = httpx.Response(
        301, headers={"location": "https://meercal.example/api/state"})
    with pytest.raises(MeercalError, match="redirect to https://meercal.example/api/state"):
        fetch_calendars(BASE, "")


def test_a_timeout_on_import_says_trying_again_is_safe(wire):
    _, answers = wire
    answers[("POST", "/api/import")] = httpx.ReadTimeout("slow")
    with pytest.raises(NoAnswer, match="Adding it again is safe"):
        import_ics(BASE, "", 10, "invite.ics", ICS)


def test_an_unreachable_meercal_names_the_address(wire):
    _, answers = wire
    answers[("GET", "/api/state")] = httpx.ConnectError("refused")
    with pytest.raises(MeercalError, match="Could not reach meercal at http://meercal.example:8010"):
        fetch_calendars(BASE, "")


# --- Preview and import ----------------------------------------------------


def test_the_import_sends_the_file_and_the_calendar_meercal_reads(wire):
    seen, answers = wire
    answers[("POST", "/api/import")] = httpx.Response(201, json={
        "calendar": {"id": 10, "name": "Work"}, "created": 1, "updated": 0, "queued": 1})

    res = import_ics(BASE, "pw", 10, "invite.ics", ICS)

    assert res["created"] == 1
    form = _form(seen[0])
    assert form["file"] == ("invite.ics", ICS)
    assert form["calendar_id"][1] == b"10"


def test_the_preview_passes_on_only_the_fields_the_dialog_uses(wire):
    seen, answers = wire
    answers[("POST", "/api/import/preview")] = httpx.Response(200, json={
        "filename": "invite.ics", "name": "invite", "events": 1, "recurring": 0, "all_day": 0,
        "first": "2026-10-16", "last": "2026-10-16", "titles": ["Standup"],
        "outside_horizon": 0, "horizon": {"from": "2026-01-01", "to": "2027-12-31"},
        "something_new": "x"})

    got = preview(BASE, "", "invite.ics", ICS)

    assert got["titles"] == ["Standup"]
    assert "something_new" not in got and "name" not in got
    assert _form(seen[0])["file"] == ("invite.ics", ICS)


# --- Where the server may go -----------------------------------------------


def test_a_private_address_is_refused_by_default(monkeypatch):
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "meercal_allow_private_hosts", False)
    with pytest.raises(BlockedHost, match="meercal_allow_private_hosts"):
        meercal.pinned("http://127.0.0.1:8010")
    with pytest.raises(BlockedHost):
        meercal.pinned("http://10.0.0.5:8010")


def test_the_restriction_can_be_lifted_for_a_meercal_next_door(monkeypatch):
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "meercal_allow_private_hosts", True)
    assert meercal.pinned("http://127.0.0.1:8010") == ("http://127.0.0.1:8010", {})


def test_a_public_address_is_pinned_to_what_it_resolved_to(monkeypatch):
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "meercal_allow_private_hosts", False)
    # An IP literal, which getaddrinfo answers without asking a resolver.
    target, pin = meercal.pinned("https://93.184.216.34:8443")
    assert target == "https://93.184.216.34:8443"
    assert pin["headers"]["Host"] == "93.184.216.34:8443"


def test_a_calendar_that_went_missing_is_named_as_such(wire):
    """meercal answers a bare "Not found" for a calendar deleted since the
    dialog listed it."""
    _, answers = wire
    answers[("POST", "/api/import")] = httpx.Response(404, json={"detail": "Not found"})
    with pytest.raises(Refused, match="not in meercal any more"):
        import_ics(BASE, "", 99, "invite.ics", ICS)
