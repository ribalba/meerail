"""Pytest fixtures for meerail.

Unit tests (test_parse.py) need only the server's Python deps.
Integration tests use fixtures here and are skipped when their backing service
(the meerail server, or GreenMail) isn't reachable.
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timezone

import pytest

from helpers import api, make_message, server_up

T0 = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def require_server():
    if not server_up():
        pytest.skip("meerail server not reachable at MEERAIL_URL (run: docker compose up -d)")


@pytest.fixture
def account(require_server):
    """Create a throwaway account; delete it (cascades) on teardown."""
    email = f"pytest-{uuid.uuid4().hex[:10]}@example.com"
    code, acc = api("POST", "/api/accounts", {"email": email, "label": "pytest"})
    assert code == 201, acc
    yield acc
    api("DELETE", f"/api/accounts/{acc['id']}")


def status_for(email: str) -> dict | None:
    code, rows = api("GET", "/api/sync/status")
    assert code == 200
    return next((r for r in rows if r["email"] == email), None)


def mailbox(email: str, role: str) -> dict | None:
    st = status_for(email)
    if not st:
        return None
    return next((m for m in st["mailboxes"] if m["role"] == role), None)


def ingest_one(email: str, account_id: int, token: str, frm: str = "sender@ex.com",
               uid: int = 1) -> tuple[int, str]:
    """Ingest one message (body contains `token`) into INBOX; return (message_id, rfc_message_id)."""
    mid = f"m-{uuid.uuid4().hex}@t"
    raw = make_message(f"<{mid}>", f"Subj {token}", frm, email, f"{token} body text", T0)
    api("POST", "/api/agent/folders",
        {"account": email, "folders": [{"imap_name": "INBOX", "uidvalidity": 1}]})
    api("POST", "/api/agent/messages", {
        "account": email, "folder": "INBOX", "uidvalidity": 1,
        "items": [{"uid": uid, "raw_b64": base64.b64encode(raw).decode()}]})
    api("POST", "/api/agent/cursor", {"account": email, "folder": "INBOX", "last_uid": uid})
    _, r = api("GET", f"/api/search?q={token}&account_id={account_id}")
    return r["rows"][0]["id"], mid
