"""Integration tests for /api/search (regex + keyword, case sensitivity, scope).

Requires the running server; uses a throwaway account so results are isolated.
"""

import base64
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode

from helpers import api, make_message

T0 = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)


def _ingest(email, messages):
    """messages: list of (uid, raw_bytes). Registers INBOX and uploads them."""
    api("POST", "/api/agent/folders",
        {"account": email, "folders": [{"imap_name": "INBOX", "uidvalidity": 1}]})
    api("POST", "/api/agent/messages", {
        "account": email, "folder": "INBOX", "uidvalidity": 1,
        "items": [{"uid": uid, "raw_b64": base64.b64encode(raw).decode()} for uid, raw in messages]})
    api("POST", "/api/agent/cursor",
        {"account": email, "folder": "INBOX", "last_uid": max(u for u, _ in messages)})


def _search(account_id, q, **kw):
    params = {"q": q, "account_id": account_id, **kw}
    return api("GET", "/api/search?" + urlencode(params))


def test_regex_keyword_and_case(account):
    email, aid = account["email"], account["id"]
    z = f"z-{uuid.uuid4().hex}@t"
    g = f"g-{uuid.uuid4().hex}@t"
    _ingest(email, [
        (1, make_message(f"<{z}>", "Zoo update", "x@y.com", email, "the ZEBRAWORD escaped today", T0)),
        (2, make_message(f"<{g}>", "Safari log", "x@y.com", email, "a GIRAFFE appeared", T0)),
    ])

    # keyword substring
    code, r = _search(aid, "ZEBRAWORD")
    assert code == 200 and r["total"] == 1

    # regex alternation across both messages
    code, r = _search(aid, r"ZEBRA\w+|GIRAFFE", mode="regex")
    assert code == 200 and r["total"] == 2

    # case sensitivity (~ vs ~*)
    _, r = _search(aid, "zebraword", mode="regex", case_sensitive="true")
    assert r["total"] == 0
    _, r = _search(aid, "ZEBRAWORD", mode="regex", case_sensitive="true")
    assert r["total"] == 1

    # keyword AND semantics: both terms must appear (they don't in one message)
    _, r = _search(aid, "ZEBRAWORD GIRAFFE")
    assert r["total"] == 0

    # invalid regex -> 400 with a helpful message
    code, r = _search(aid, "(", mode="regex")
    assert code == 400
    assert "regex" in (r.get("detail", "") if isinstance(r, dict) else "").lower()


def test_time_window_excludes_old(account):
    email, aid = account["email"], account["id"]
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    mid = f"old-{uuid.uuid4().hex}@t"
    _ingest(email, [(1, make_message(f"<{mid}>", "Ancient", "x@y.com", email, "PALEOTOKEN here", old))])

    _, r = _search(aid, "PALEOTOKEN")
    assert r["total"] == 1                          # all-time finds it
    _, r = _search(aid, "PALEOTOKEN", years=2)
    assert r["total"] == 0                          # last 2 years excludes a 2000 message
