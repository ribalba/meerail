"""Deleting mail in an account nothing syncs.

Everywhere else deleting is two halves: file the message in Trash now, and let
Empty Trash tell the server to expunge it later. An imported account has no
later and no server — the rows in this database *are* the mail — so both halves
have to mean something different here, and this is the file that says what.

Two things are being pinned down. Delete has to work at all: with no \\Trash on
the account the keypress used to be refused with a sentence about creating one
on the server and syncing again, which for mail that came off disk is advice
nobody can take. And permanent has to mean permanent: the queue path emptied a
Trash by dropping the placements and leaving every message row, raw MIME and
attachment byte behind, so a mailbox that reported itself emptied still cost
what it did before.
"""

import uuid

import dbfixture
from conftest import T0, ingest_one
from helpers import api, make_message


def _import_one(email: str, token: str, folder: str) -> None:
    raw = make_message(f"<m-{uuid.uuid4().hex}@t>", f"Subj {token}", "s@ex.com",
                       email, f"{token} body", T0)
    dbfixture.import_raw_message(email, raw, folder=folder)


def _mailboxes(account_id: int) -> list[dict]:
    _, sidebar = api("GET", "/api/mailboxes")
    return next(a for a in sidebar["accounts"] if a["id"] == account_id)["mailboxes"]


def _folder_id(account_id: int, imap_name: str) -> int:
    return next(m["id"] for m in _mailboxes(account_id) if m["imap_name"] == imap_name)


def _row(account_id: int, token: str) -> dict:
    _, r = api("GET", f"/api/search?q={token}&account_id={account_id}")
    return r["rows"][0]


def _in_folder(mailbox_id: int) -> list[int]:
    _, r = api("GET", f"/api/messages?mailbox_id={mailbox_id}&limit=200")
    return [row["id"] for row in r["rows"]]


# --- Delete files into a Trash this app makes for itself ----------------------


def test_local_trash_creates_the_folder_it_needs(local_account):
    """The refusal this replaces was Delete not working, on every message.

    An imported account starts with exactly the folders the mbox had in it, and
    "create one on the server and sync again" is not something the owner of one
    can do. The folder is this app's to make here — the same thing the + button
    does — so it makes it.
    """
    email, aid = local_account["email"], local_account["id"]
    token = "LOCTRASH" + uuid.uuid4().hex[:6]
    home = "Wrong" + uuid.uuid4().hex[:6]
    _import_one(email, token, home)
    mid = _row(aid, token)["id"]
    assert not [m for m in _mailboxes(aid) if m["role"] == "trash"]

    code, body = api("POST", "/api/messages/bulk/trash",
                     {"items": [{"account_id": aid, "message_id": mid}]})
    assert code == 200, body
    assert body["moved"] == 1

    trash = next(m for m in _mailboxes(aid) if m["role"] == "trash")
    assert trash["imap_name"] == "Trash"
    assert [p["imap_name"] for p in dbfixture.placements(email, mid)] == ["Trash"]
    # Made here, not asked for: nothing is left waiting on an agent that is
    # never coming, and the placement carries a real UID rather than the
    # negative "we wrote this ahead of the server" one.
    assert not dbfixture.pending_actions(email, "move", status="pending")
    assert dbfixture.placements(email, mid)[0]["imap_uid"] > 0


def test_local_trash_reuses_an_imported_trash(local_account):
    """An mbox that already had one keeps it — the folder is only made when the
    account genuinely has nowhere to file to."""
    email, aid = local_account["email"], local_account["id"]
    token = "LOCREUSE" + uuid.uuid4().hex[:6]
    _import_one(email, "SEED" + uuid.uuid4().hex[:6], "Trash")
    _import_one(email, token, "Wrong" + uuid.uuid4().hex[:6])
    existing = _folder_id(aid, "Trash")
    mid = _row(aid, token)["id"]

    assert api("POST", "/api/messages/bulk/trash",
               {"items": [{"account_id": aid, "message_id": mid}]})[0] == 200
    assert _folder_id(aid, "Trash") == existing
    assert len([m for m in _mailboxes(aid) if m["role"] == "trash"]) == 1


# --- Emptying it destroys the rows, not just the placements -------------------


def test_local_empty_trash_deletes_the_message_itself(local_account):
    """Emptied has to mean gone, because here there is nothing else holding it.

    On an account with a server the placement going is the whole of what this
    app can do — the expunge is the agent's job and the content row is collected
    later. Here the queue nobody drains meant the message simply stayed: filed
    in no folder, invisible in every list, still holding its raw MIME.
    """
    email, aid = local_account["email"], local_account["id"]
    token = "LOCEMPTY" + uuid.uuid4().hex[:6]
    _import_one(email, token, "Wrong" + uuid.uuid4().hex[:6])
    mid = _row(aid, token)["id"]
    api("POST", "/api/messages/bulk/trash",
        {"items": [{"account_id": aid, "message_id": mid}]})
    trash = _folder_id(aid, "Trash")

    code, body = api("POST", "/api/messages/bulk/empty-trash",
                     {"mailbox_id": trash, "confirm": True})
    assert code == 200, body
    assert body["done"] is True
    assert _in_folder(trash) == []
    assert dbfixture.message_survives(mid) == {
        "message": False, "attachments": 0, "locations": 0}
    assert api("GET", f"/api/messages/{mid}")[0] == 404


# --- Delete permanently, without the Trash in between -------------------------


def test_purge_destroys_a_selection_outright(local_account):
    """The thing the Trash is in the way of: "I do not want a Trash, I want this
    gone." One request, no folder in between, nothing left in the database."""
    email, aid = local_account["email"], local_account["id"]
    token = "LOCPURGE" + uuid.uuid4().hex[:6]
    home = "Wrong" + uuid.uuid4().hex[:6]
    _import_one(email, token, home)
    mid = _row(aid, token)["id"]
    source = _folder_id(aid, home)

    code, body = api("POST", "/api/messages/bulk/purge", {
        "items": [{"account_id": aid, "message_id": mid}], "confirm": True,
    })
    assert code == 200, body
    assert body["deleted"] == 1
    assert _in_folder(source) == []
    assert dbfixture.message_survives(mid)["message"] is False
    # No Trash was invented on the way past. This route files nothing.
    assert not [m for m in _mailboxes(aid) if m["role"] == "trash"]


def test_purge_takes_every_placement_of_a_message(local_account):
    """Ticking a conversation says nothing about which folder you were standing
    in, so a copy left behind under another folder's name is the delete not
    having worked. The same mbox imported twice is one message wearing two."""
    email, aid = local_account["email"], local_account["id"]
    token = "LOCBOTH" + uuid.uuid4().hex[:6]
    a = "First" + uuid.uuid4().hex[:6]
    b = "Second" + uuid.uuid4().hex[:6]
    raw = make_message(f"<m-{uuid.uuid4().hex}@t>", f"Subj {token}", "s@ex.com",
                       email, f"{token} body", T0)
    dbfixture.import_raw_message(email, raw, folder=a)
    dbfixture.import_raw_message(email, raw, folder=b)
    mid = _row(aid, token)["id"]
    assert len(dbfixture.placements(email, mid)) == 2

    code, body = api("POST", "/api/messages/bulk/purge", {
        "items": [{"account_id": aid, "message_id": mid}], "confirm": True,
    })
    assert code == 200, body
    assert body["deleted"] == 1
    assert _in_folder(_folder_id(aid, a)) == []
    assert _in_folder(_folder_id(aid, b)) == []
    assert dbfixture.message_survives(mid)["message"] is False


def test_emptying_one_folder_leaves_the_copy_in_another(local_account):
    """The other side of the same fact. Emptying one folder's Trash is not
    permission to destroy a copy filed somewhere else, so the placement goes and
    the message stays — it is only destroyed once nothing reaches it."""
    email, aid = local_account["email"], local_account["id"]
    token = "LOCKEEP" + uuid.uuid4().hex[:6]
    keep = "Keep" + uuid.uuid4().hex[:6]
    raw = make_message(f"<m-{uuid.uuid4().hex}@t>", f"Subj {token}", "s@ex.com",
                       email, f"{token} body", T0)
    dbfixture.import_raw_message(email, raw, folder=keep)
    dbfixture.import_raw_message(email, raw, folder="Trash")
    mid = _row(aid, token)["id"]

    code, body = api("POST", "/api/messages/bulk/empty-trash",
                     {"mailbox_id": _folder_id(aid, "Trash"), "confirm": True})
    assert code == 200, body
    assert [p["imap_name"] for p in dbfixture.placements(email, mid)] == [keep]
    assert dbfixture.message_survives(mid)["message"] is True


def test_purge_all_empties_a_folder_a_chunk_at_a_time(local_account):
    """The whole-folder version — the import that went in the wrong place, taken
    back out. `removed` is what the client loops on, because it counts
    placements and `deleted` counts messages that stopped existing."""
    email, aid = local_account["email"], local_account["id"]
    home = "Wrong" + uuid.uuid4().hex[:6]
    tokens = ["LOCALL%s%d" % (uuid.uuid4().hex[:5], n) for n in range(3)]
    for token in tokens:
        _import_one(email, token, home)
    ids = [_row(aid, t)["id"] for t in tokens]
    source = _folder_id(aid, home)

    code, body = api("POST", "/api/messages/bulk/purge-all",
                     {"mailbox_id": source, "confirm": True})
    assert code == 200, body
    assert body["deleted"] == 3
    assert body["removed"] == 3
    assert body["done"] is True
    assert _in_folder(source) == []
    for mid in ids:
        assert dbfixture.message_survives(mid) == {
            "message": False, "attachments": 0, "locations": 0}


# --- What it refuses ----------------------------------------------------------


def test_purge_without_confirm_deletes_nothing(local_account):
    """`confirm` defaults to false, so a client that forgets it gets an error
    rather than a deletion — the same contract Empty Trash has."""
    email, aid = local_account["email"], local_account["id"]
    token = "LOCNOCONF" + uuid.uuid4().hex[:6]
    home = "Wrong" + uuid.uuid4().hex[:6]
    _import_one(email, token, home)
    mid = _row(aid, token)["id"]

    assert api("POST", "/api/messages/bulk/purge",
               {"items": [{"account_id": aid, "message_id": mid}]})[0] == 400
    assert api("POST", "/api/messages/bulk/purge-all",
               {"mailbox_id": _folder_id(aid, home)})[0] == 400
    assert dbfixture.message_survives(mid)["message"] is True


def test_purge_refuses_an_account_with_a_server_behind_it(account):
    """Deleting rows there would delete a copy: the message is still on the
    server, so it would vanish until the next pass fetched it again — a delete
    that silently undoes itself hours later, with no button to blame."""
    email, aid = account["email"], account["id"]
    token = "SRVPURGE" + uuid.uuid4().hex[:6]
    mid, _ = ingest_one(email, aid, token)

    code, body = api("POST", "/api/messages/bulk/purge", {
        "items": [{"account_id": aid, "message_id": mid}], "confirm": True,
    })
    assert code == 400
    assert "imported" in body["detail"]
    assert dbfixture.message_survives(mid)["message"] is True
    assert api("GET", f"/api/messages/{mid}")[0] == 200


def test_purge_all_refuses_a_selector_that_is_not_a_folder(local_account):
    """`flagged` spans every folder and every account, which is how a flagged
    message sitting in someone's Trash was once destroyed by a button that never
    said so. A permanent delete is aimed at a place, not at a filter."""
    email, aid = local_account["email"], local_account["id"]
    token = "LOCFLAG" + uuid.uuid4().hex[:6]
    _import_one(email, token, "Wrong" + uuid.uuid4().hex[:6])
    mid = _row(aid, token)["id"]
    api("POST", f"/api/messages/{mid}/flag?flagged=1")

    code, _ = api("POST", "/api/messages/bulk/purge-all",
                  {"scope": "flagged", "confirm": True})
    assert code == 400
    assert dbfixture.message_survives(mid)["message"] is True
