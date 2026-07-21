"""Integration tests for the /api/agent/* protocol (black-box, over HTTP).

Requires the meerail server running (docker compose up -d). Each test uses a
throwaway account, so it is isolated from any real data.
"""

import base64
import uuid
from datetime import datetime, timedelta, timezone

from conftest import status_for
from helpers import api, make_message

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _mb(email: str, imap_name: str) -> dict:
    st = status_for(email)
    return next(m for m in st["mailboxes"] if m["imap_name"] == imap_name)


def test_ingest_thread_dedup_flags_and_prune(account):
    email = account["email"]
    a, b, c = (f"{p}-{uuid.uuid4().hex}@t" for p in ("a", "b", "c"))
    A = make_message(f"<{a}>", "Subject ALPHA", "x@y.com", email, "the body", T0)
    B = make_message(f"<{b}>", "Re: Subject ALPHA", "z@y.com", email, "a reply",
                     T0 + timedelta(hours=1), in_reply_to=f"<{a}>", refs=[f"<{a}>"])
    C = make_message(f"<{c}>", "Unrelated BETA", "q@y.com", email, "other", T0 + timedelta(days=2))

    # Register INBOX -> fresh cursor.
    code, cur = api("POST", "/api/agent/folders",
                    {"account": email, "folders": [{"imap_name": "INBOX", "uidvalidity": 1, "uidnext": 4}]})
    assert code == 200 and cur[0]["last_uid"] == 0 and cur[0]["role"] == "inbox"

    # Scan: server has nothing yet -> all need raw.
    code, scan = api("POST", "/api/agent/scan", {
        "account": email, "folder": "INBOX", "uidvalidity": 1,
        "items": [{"uid": 1, "message_id": a}, {"uid": 2, "message_id": b}, {"uid": 3, "message_id": c}]})
    assert code == 200 and scan["matched"] == 0 and scan["need_raw"] == [1, 2, 3]

    # Upload raw; uid 1 already read.
    code, msgs = api("POST", "/api/agent/messages", {
        "account": email, "folder": "INBOX", "uidvalidity": 1,
        "items": [{"uid": 1, "flags": {"seen": True}, "raw_b64": b64(A)},
                  {"uid": 2, "raw_b64": b64(B)},
                  {"uid": 3, "raw_b64": b64(C)}]})
    assert code == 200 and msgs["stored"] == 3 and msgs["created"] == 3
    api("POST", "/api/agent/cursor", {"account": email, "folder": "INBOX", "last_uid": 3})

    inbox = _mb(email, "INBOX")
    assert inbox["total"] == 3 and inbox["unread"] == 2  # one seen

    # Dedup: the same messages under a second folder must NOT re-upload raw.
    api("POST", "/api/agent/folders",
        {"account": email, "folders": [{"imap_name": "Archive2", "uidvalidity": 1, "uidnext": 13}]})
    code, scan2 = api("POST", "/api/agent/scan", {
        "account": email, "folder": "Archive2", "uidvalidity": 1,
        "items": [{"uid": 10, "message_id": a}, {"uid": 11, "message_id": b}, {"uid": 12, "message_id": c}]})
    assert code == 200 and scan2["matched"] == 3 and scan2["need_raw"] == []
    api("POST", "/api/agent/cursor", {"account": email, "folder": "Archive2", "last_uid": 12})
    assert _mb(email, "Archive2")["total"] == 3

    # Flag change: mark uid 2 read -> unread drops.
    api("POST", "/api/agent/flags",
        {"account": email, "folder": "INBOX", "items": [{"uid": 2, "flags": {"seen": True}}]})
    assert _mb(email, "INBOX")["unread"] == 1

    # Vanished: uid 3 gone from INBOX -> its INBOX placement is pruned...
    api("POST", "/api/agent/present", {"account": email, "folder": "INBOX", "uidvalidity": 1, "uids": [1, 2]})
    assert _mb(email, "INBOX")["total"] == 2
    # ...but the message still lives in Archive2, so that folder is untouched.
    assert _mb(email, "Archive2")["total"] == 3


def test_rescan_is_idempotent(account):
    email = account["email"]
    mid = f"solo-{uuid.uuid4().hex}@t"
    raw = make_message(f"<{mid}>", "Solo", "x@y.com", email, "hi", T0)
    api("POST", "/api/agent/folders",
        {"account": email, "folders": [{"imap_name": "INBOX", "uidvalidity": 1}]})
    api("POST", "/api/agent/messages", {
        "account": email, "folder": "INBOX", "uidvalidity": 1,
        "items": [{"uid": 1, "raw_b64": b64(raw)}]})
    api("POST", "/api/agent/cursor", {"account": email, "folder": "INBOX", "last_uid": 1})

    # A repeat scan of the same UID/Message-ID recognizes existing content.
    code, rescan = api("POST", "/api/agent/scan", {
        "account": email, "folder": "INBOX", "uidvalidity": 1,
        "items": [{"uid": 1, "message_id": mid}]})
    assert code == 200 and rescan["matched"] == 1 and rescan["need_raw"] == []
    assert _mb(email, "INBOX")["total"] == 1  # no duplicate row


def test_unknown_account_is_rejected(require_server):
    code, _ = api("POST", "/api/agent/folders",
                  {"account": "nobody-here@example.com", "folders": [{"imap_name": "INBOX"}]})
    assert code == 404
