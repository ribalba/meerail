"""Integration tests for the ingest pipeline the agent owns.

These drive `core.ingest` directly against the database — the same calls the
agent's sync loop makes — and assert the results through the server's read APIs.
(Formerly test_agent_protocol.py, which drove the deleted /api/agent/* HTTP API.)
"""

import uuid
from datetime import datetime, timedelta, timezone

import dbfixture
from conftest import status_for
from helpers import api, make_message

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)


def _mb(email: str, imap_name: str) -> dict:
    st = status_for(email)
    return next(m for m in st["mailboxes"] if m["imap_name"] == imap_name)


def test_ingest_threads_dedups_flags_and_prunes(account):
    email = account["email"]
    a, b, c = (f"{p}-{uuid.uuid4().hex}@t" for p in ("a", "b", "c"))
    A = make_message(f"<{a}>", "Subject ALPHA", "x@y.com", email, "the body", T0)
    B = make_message(f"<{b}>", "Re: Subject ALPHA", "z@y.com", email, "a reply",
                     T0 + timedelta(hours=1), in_reply_to=f"<{a}>", refs=[f"<{a}>"])
    C = make_message(f"<{c}>", "Unrelated BETA", "q@y.com", email, "other", T0 + timedelta(days=2))

    for uid, raw in enumerate((A, B, C), start=1):
        dbfixture.ingest_raw_message(email, raw, uid=uid)

    inbox = _mb(email, "INBOX")
    assert inbox["total"] == 3

    # A and B are one conversation; C is its own.
    _, rows = api("GET", f"/api/messages?mailbox_id={inbox['id']}&limit=50")
    threads = {r["thread_id"] for r in rows["rows"]}
    assert len(threads) == 2, threads

    # The same Message-ID under a second Proton label is a placement, not a copy.
    assert dbfixture.record_placement(email, a, uid=101, folder="Archive2",
                                      role_hint="\\Archive") is True
    assert dbfixture.message_count(email) == 3          # no duplicate content
    assert _mb(email, "Archive2")["total"] == 1

    # Flags sync per folder.
    dbfixture.set_flags(email, "INBOX", [{"uid": 1, "flags": {"seen": True}}])
    assert _mb(email, "INBOX")["unread"] == 2

    # uid 3 vanished from INBOX -> its placement goes...
    dbfixture.set_present(email, "INBOX", [1, 2])
    assert _mb(email, "INBOX")["total"] == 2
    # ...but the copy in Archive2 keeps that message alive.
    assert _mb(email, "Archive2")["total"] == 1
    assert dbfixture.message_count(email) == 2


def test_rescan_is_idempotent(account):
    email = account["email"]
    mid = f"solo-{uuid.uuid4().hex}@t"
    raw = make_message(f"<{mid}>", "Solo", "x@y.com", email, "hi", T0)
    dbfixture.ingest_raw_message(email, raw, uid=1)

    # Seeing the same UID/Message-ID again recognizes existing content.
    assert dbfixture.record_placement(email, mid, uid=1, folder="INBOX") is True
    assert _mb(email, "INBOX")["total"] == 1  # no duplicate row
    assert dbfixture.message_count(email) == 1


def test_unknown_account_is_autoregistered(require_server):
    """First contact from an agent creates the account, so it appears in the UI."""
    email = f"auto-{uuid.uuid4().hex[:10]}@example.com"
    raw = make_message(f"<auto-{uuid.uuid4().hex}@t>", "Hello", "x@y.com", email, "body", T0)
    dbfixture.ingest_raw_message(email, raw, uid=1)

    code, accounts = api("GET", "/api/accounts")
    assert code == 200
    acc = next((a for a in accounts if a["email"] == email), None)
    assert acc is not None, "ingest should have created the account"
    assert acc["label"] == email.split("@")[0]
    api("DELETE", f"/api/accounts/{acc['id']}")


def test_attachment_text_is_extracted_and_searchable(account):
    """Tika extraction runs in the agent and feeds the search index."""
    email, aid = account["email"], account["id"]
    token = "TIKATOKEN" + uuid.uuid4().hex[:6]
    mid = f"att-{uuid.uuid4().hex}@t"
    raw = make_message(f"<{mid}>", "Has attachment", "x@y.com", email, "see attached", T0,
                       text_attachment=f"{token} lives inside the attachment".encode())
    dbfixture.ingest_raw_message(email, raw, uid=1)

    assert dbfixture.extract_all() >= 1

    _, sr = api("GET", f"/api/search?q={token}&account_id={aid}")
    assert sr["total"] == 1, sr
    assert sr["rows"][0]["subject"] == "Has attachment"


def test_sync_marks_backfill_complete(account):
    """The agent's end-of-pass report lands on the account row the UI reads."""
    email, aid = account["email"], account["id"]
    dbfixture.report_sync(email, backfill_complete=True)

    _, accounts = api("GET", "/api/accounts")
    acc = next(a for a in accounts if a["id"] == aid)
    assert acc["backfill_complete"] is True
