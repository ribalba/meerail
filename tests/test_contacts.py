"""Integration tests for compose address autocomplete (materialized contacts)."""

import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime

import dbfixture
from helpers import api

T0 = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)


def _rich(mid, frm, to, cc, bcc, when):
    m = EmailMessage()
    m["Message-ID"] = mid
    m["Subject"] = "rich"
    m["From"] = frm
    m["To"] = to
    if cc:
        m["Cc"] = cc
    if bcc:
        m["Bcc"] = bcc
    m["Date"] = format_datetime(when)
    m.set_content("hello")
    return m.as_bytes()


def _ingest(email, uid, raw):
    dbfixture.ingest_raw_message(email, raw, uid=uid)


def test_autocomplete_covers_from_to_cc_bcc(account):
    email = account["email"]
    tag = uuid.uuid4().hex[:8]
    frm, to, cc, bcc = (f"{k}-{tag}@ex.test" for k in ("from", "to", "cc", "bcc"))
    _ingest(email, 1, _rich(f"<r-{tag}@t>", f"Sender <{frm}>", to, cc, bcc, T0))

    api("POST", "/api/contacts/refresh")
    _, rows = api("GET", f"/api/contacts?q={tag}")
    addrs = {c["address"] for c in rows}
    assert {frm, to, cc, bcc} <= addrs                 # every field contributes contacts


def test_autocomplete_matches_name_and_excludes_self(account):
    email = account["email"]
    tag = uuid.uuid4().hex[:8]
    _ingest(email, 1, _rich(f"<n-{tag}@t>", f"Zaphod{tag} <zap-{tag}@ex.test>", email, None, None, T0))

    api("POST", "/api/contacts/refresh")
    _, by_name = api("GET", f"/api/contacts?q=Zaphod{tag}")
    assert any(c["address"] == f"zap-{tag}@ex.test" for c in by_name)   # matched by display name
    # the account's own address (in To) is not offered as a contact
    _, self_hits = api("GET", f"/api/contacts?q={email.split('@')[0]}")
    assert all(c["address"] != email for c in self_hits)


def test_scan_window_is_configurable(account):
    email = account["email"]
    tag = uuid.uuid4().hex[:8]
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    _ingest(email, 1, _rich(f"<o-{tag}@t>", f"<old-{tag}@ex.test>", email, None, None, old))

    api("POST", "/api/contacts/refresh?years=1")       # 1-year window
    _, rows = api("GET", f"/api/contacts?q=old-{tag}")
    assert not any(c["address"] == f"old-{tag}@ex.test" for c in rows)

    api("POST", "/api/contacts/refresh?years=0")       # all time
    _, rows = api("GET", f"/api/contacts?q=old-{tag}")
    assert any(c["address"] == f"old-{tag}@ex.test" for c in rows)

    api("POST", "/api/contacts/refresh")               # restore default window
