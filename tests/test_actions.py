"""Integration tests for message actions (read/flag/trash) + agent-action queue."""

import uuid

import dbfixture
from conftest import ingest_one
from helpers import api


def _actions(email):
    """The queue the agent drains — read straight from the DB now."""
    return dbfixture.pending_actions(email)


def test_mark_read_updates_state_and_enqueues(account):
    email, aid = account["email"], account["id"]
    mid, _ = ingest_one(email, aid, "MARKTOK" + uuid.uuid4().hex[:6])

    code, _ = api("POST", f"/api/messages/{mid}/mark?seen=1")
    assert code == 200

    _, detail = api("GET", f"/api/messages/{mid}")
    assert detail["seen"] is True

    acts = _actions(email)
    assert any(a["type"] == "setflags" and "\\Seen" in a["payload"].get("add", []) for a in acts)


def test_flag_updates_state_and_enqueues(account):
    email, aid = account["email"], account["id"]
    mid, _ = ingest_one(email, aid, "FLAGTOK" + uuid.uuid4().hex[:6])

    api("POST", f"/api/messages/{mid}/flag?flagged=1")
    _, detail = api("GET", f"/api/messages/{mid}")
    assert detail["flagged"] is True

    acts = _actions(email)
    assert any(a["type"] == "setflags" and "\\Flagged" in a["payload"].get("add", []) for a in acts)


def test_trash_removes_from_inbox_and_enqueues(account):
    email, aid = account["email"], account["id"]
    mid, _ = ingest_one(email, aid, "TRASHTOK" + uuid.uuid4().hex[:6])

    before = mailbox_total(email)
    _, boxes = api("GET", "/api/mailboxes")
    inbox_id = next(m["id"] for a in boxes["accounts"] for m in a["mailboxes"]
                    if a["email"] == email and m["role"] == "inbox")
    code, _ = api("POST", f"/api/messages/{mid}/trash?source_mailbox_id={inbox_id}")
    assert code == 200
    assert mailbox_total(email) == before - 1  # left the inbox locally

    acts = _actions(email)
    # No Trash folder for this account -> IMAP delete (\Deleted + expunge).
    assert any(a["type"] in ("move", "delete") for a in acts)


def mailbox_total(email):
    _, rows = api("GET", "/api/sync/status")
    st = next(r for r in rows if r["email"] == email)
    return next(m["total"] for m in st["mailboxes"] if m["role"] == "inbox")
