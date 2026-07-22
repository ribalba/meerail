"""Integration tests for folder creation (queued to the agent, not applied here)."""

import uuid

import dbfixture
from helpers import api


def _creates(email):
    return dbfixture.pending_actions(email, "create_folder")


def test_create_folder_enqueues_action(account):
    email, aid = account["email"], account["id"]
    name = "Pytest" + uuid.uuid4().hex[:6]

    code, body = api("POST", "/api/mailboxes", {"account_id": aid, "name": name})
    assert code == 202
    assert body["name"] == name

    assert any(a["payload"]["name"] == name for a in _creates(email))


def test_create_folder_does_not_write_a_mailbox_row(account):
    """The row must come from the agent's LIST pass — one written here would be
    deleted by prune_mailboxes on that very pass."""
    email, aid = account["email"], account["id"]
    name = "Pytest" + uuid.uuid4().hex[:6]

    api("POST", "/api/mailboxes", {"account_id": aid, "name": name})

    _, sidebar = api("GET", "/api/mailboxes")
    acc = next(a for a in sidebar["accounts"] if a["id"] == aid)
    assert not any(m["imap_name"] == name for m in acc["mailboxes"])


def test_create_folder_trims_whitespace(account):
    aid = account["id"]
    leaf = "Pytest" + uuid.uuid4().hex[:6]

    code, body = api("POST", "/api/mailboxes", {"account_id": aid, "name": f"   {leaf}   "})
    assert code == 202
    assert body["name"] == leaf


def test_create_folder_clashes_with_a_namespaced_folder(account):
    """Bridge stores user folders as "Folders/<leaf>", so a bare leaf that
    matches an existing folder's display name is still a duplicate."""
    email, aid = account["email"], account["id"]
    leaf = "Pytest" + uuid.uuid4().hex[:6]
    dbfixture.create_folder(email, f"Folders/{leaf}")

    code, _ = api("POST", "/api/mailboxes", {"account_id": aid, "name": leaf})
    assert code == 409
    assert not _creates(email)


def test_create_folder_rejects_duplicate_of_existing_mailbox(account):
    email, aid = account["email"], account["id"]
    name = "Pytest" + uuid.uuid4().hex[:6]
    dbfixture.create_folder(email, name)

    code, _ = api("POST", "/api/mailboxes", {"account_id": aid, "name": name})
    assert code == 409
    assert not _creates(email)


def test_create_folder_rejects_duplicate_pending_request(account):
    email, aid = account["email"], account["id"]
    name = "Pytest" + uuid.uuid4().hex[:6]

    assert api("POST", "/api/mailboxes", {"account_id": aid, "name": name})[0] == 202
    assert api("POST", "/api/mailboxes", {"account_id": aid, "name": name})[0] == 409
    assert len(_creates(email)) == 1


def test_create_folder_rejects_bad_names(account):
    aid = account["id"]
    for bad in ["", "   ", "/", "Parent/Child", 'quo"te', "star*", "per%cent",
                "back\\slash", "bell\x07"]:
        code, _ = api("POST", "/api/mailboxes", {"account_id": aid, "name": bad})
        assert code == 400, f"expected 400 for {bad!r}, got {code}"


def test_create_folder_unknown_account(require_server):
    code, _ = api("POST", "/api/mailboxes", {"account_id": 99999999, "name": "Nope"})
    assert code == 404
