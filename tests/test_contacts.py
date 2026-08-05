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


def test_an_alias_of_your_own_is_not_a_contact(account):
    """One account owns several addresses. All of them are you.

    Only the primary used to be excluded, so mail addressed to an alias put the
    user in their own address book — offered back to them in the composer — and
    mail *sent from* one read as mail received, which is a different weight in
    the co-recipient ranking (see the `sent` half of contact_pairs).
    """
    email = account["email"]
    tag = uuid.uuid4().hex[:8]
    alias = f"alias-{tag}@ex.test"
    dbfixture.report_sync(email, addresses=[email, alias])
    other = f"other-{tag}@ex.test"
    _ingest(email, 1, _rich(f"<a1-{tag}@t>", f"<{other}>", alias, None, None, T0))
    _ingest(email, 2, _rich(f"<a2-{tag}@t>", f"<{alias}>", other, None, None, T0))

    api("POST", "/api/contacts/refresh")
    _, rows = api("GET", f"/api/contacts?q={tag}")
    addrs = {c["address"] for c in rows}
    assert other in addrs                # the person written to is a contact
    assert alias not in addrs            # the address written from is not


def _related(*addresses):
    p = "&".join(f"address={a}" for a in addresses)
    code, rows = api("GET", f"/api/contacts/related?{p}")
    assert code == 200
    return {c["address"] for c in rows}


def test_related_suggests_people_addressed_together(account):
    """Two mails to the same trio make each member suggest the other two."""
    email = account["email"]
    tag = uuid.uuid4().hex[:8]
    alice, bob, carol, dave = (f"{k}-{tag}@ex.test" for k in ("alice", "bob", "carol", "dave"))
    for i, uid in enumerate((1, 2)):
        _ingest(email, uid, _rich(f"<g{i}-{tag}@t>", email, f"{alice}, {bob}", carol, None, T0))
    # Dave shares exactly one message with Alice, and one that only arrived —
    # not a habit, and below the weight floor.
    _ingest(email, 3, _rich(f"<d-{tag}@t>", alice, email, dave, None, T0))

    api("POST", "/api/contacts/refresh")
    hits = _related(alice)
    assert {bob, carol} <= hits          # the group Alice is usually written to with
    assert dave not in hits              # met once, by accident
    assert alice not in hits             # never suggest who is already there


def test_related_counts_mail_that_only_arrived(account):
    """Received mail is history too: a group that only ever writes to you counts."""
    email = account["email"]
    tag = uuid.uuid4().hex[:8]
    sender, other = f"sender-{tag}@ex.test", f"other-{tag}@ex.test"
    for i, uid in enumerate((1, 2)):
        _ingest(email, uid, _rich(f"<in{i}-{tag}@t>", sender, email, other, None, T0))

    api("POST", "/api/contacts/refresh")
    assert other in _related(sender)


def test_related_ignores_broadcasts(account):
    """A message to a crowd is not a group — it must not pair everyone up."""
    email = account["email"]
    tag = uuid.uuid4().hex[:8]
    crowd = [f"crowd{i}-{tag}@ex.test" for i in range(14)]
    for i, uid in enumerate((1, 2)):    # twice, so the weight floor is not what filters it
        _ingest(email, uid, _rich(f"<b{i}-{tag}@t>", email, ", ".join(crowd), None, None, T0))

    api("POST", "/api/contacts/refresh")
    assert _related(crowd[0]) == set()


def test_related_takes_every_recipient_into_account(account):
    """With two people in the fields, the answer is who fits both."""
    email = account["email"]
    tag = uuid.uuid4().hex[:8]
    alice, bob, carol = (f"{k}-{tag}@ex.test" for k in ("alice", "bob", "carol"))
    for i, uid in enumerate((1, 2)):
        _ingest(email, uid, _rich(f"<t{i}-{tag}@t>", email, f"{alice}, {bob}", carol, None, T0))

    api("POST", "/api/contacts/refresh")
    hits = _related(alice, bob)
    assert hits == {carol}               # the seeds themselves drop out


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
