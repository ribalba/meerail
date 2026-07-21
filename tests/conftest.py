"""Pytest fixtures for meerail.

Unit tests (test_parse.py) need only the shared `core` deps. Integration tests
read through the server's HTTP API and write through `core.ingest` (the agent's
path), so they need both the server running and the database reachable; they are
skipped when the server isn't up.

Run with an interpreter that has core's deps — `agent/.venv/bin/python -m pytest`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

import dbfixture
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
    """Ingest one message (body contains `token`) into INBOX; return (message_id, rfc_message_id).

    Goes straight to the database via core.ingest, which is what the agent does —
    there is no ingest HTTP API any more.
    """
    mid = f"m-{uuid.uuid4().hex}@t"
    raw = make_message(f"<{mid}>", f"Subj {token}", frm, email, f"{token} body text", T0)
    dbfixture.ingest_raw_message(email, raw, uid=uid)
    _, r = api("GET", f"/api/search?q={token}&account_id={account_id}")
    return r["rows"][0]["id"], mid
