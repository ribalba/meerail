"""Integration tests for filing a selection, and a whole folder, somewhere else.

Two tiers, like the bulk delete beside them: the ticked rows, and everything a
list selector matches. And two kinds of account, because the second half of a
move is different in each — an agent account queues the instruction and files
the message optimistically, an imported one has nobody to queue it for and the
filing here is the whole of it.
"""

import uuid

import dbfixture
from conftest import T0, ingest_one, mailbox
from helpers import api, make_message


def _folder_id(account_id: int, imap_name: str) -> int:
    _, sidebar = api("GET", "/api/mailboxes")
    acc = next(a for a in sidebar["accounts"] if a["id"] == account_id)
    return next(m["id"] for m in acc["mailboxes"] if m["imap_name"] == imap_name)


def _row(account_id: int, token: str) -> dict:
    _, r = api("GET", f"/api/search?q={token}&account_id={account_id}")
    return r["rows"][0]


def _in_folder(account_id: int, mailbox_id: int) -> list[int]:
    _, r = api("GET", f"/api/messages?mailbox_id={mailbox_id}&limit=200")
    return [row["id"] for row in r["rows"]]


# --- An account an agent syncs -----------------------------------------------


def _kept(email: str) -> int:
    """A fresh custom folder for this account, registered as a sync pass would."""
    return dbfixture.create_folder(email, "Kept" + uuid.uuid4().hex[:6])


def test_bulk_move_files_the_selection_and_queues_it(account):
    email, aid = account["email"], account["id"]
    token = "BULKMV" + uuid.uuid4().hex[:6]
    mid, _ = ingest_one(email, aid, token)
    target = _kept(email)

    code, body = api("POST", "/api/messages/bulk/move", {
        "items": [{"account_id": aid, "message_id": mid}],
        "target_mailbox_id": target,
    })
    assert code == 200
    assert body["moved"] == 1

    # Filed here straight away — the message is in the target and out of the
    # inbox before the agent has been anywhere near it.
    assert mid in _in_folder(aid, target)
    assert mid not in _in_folder(aid, mailbox(email, "inbox")["id"])

    # ...and the mail server is going to be told, once.
    moves = dbfixture.pending_actions(email, "move")
    assert [a["message_pk"] for a in moves] == [mid]
    assert moves[0]["payload"]["op_id"] == body["op_id"]


def test_bulk_move_takes_the_whole_conversation(account):
    """A row in the list is a conversation, so a ticked row moves all of it —
    including a reply ingested since the page was drawn."""
    email, aid = account["email"], account["id"]
    token = "BULKTH" + uuid.uuid4().hex[:6]
    mid, rfc = ingest_one(email, aid, token)
    thread_id = _row(aid, token)["thread_id"]
    assert thread_id

    reply = make_message(f"<r-{uuid.uuid4().hex}@t>", f"Re: Subj {token}", "b@ex.com",
                         email, f"{token} reply", T0, in_reply_to=f"<{rfc}>")
    dbfixture.ingest_raw_message(email, reply, uid=2)

    target = _kept(email)
    code, body = api("POST", "/api/messages/bulk/move", {
        "items": [{"account_id": aid, "thread_id": thread_id}],
        "target_mailbox_id": target,
    })
    assert code == 200
    assert body["moved"] == 2
    assert len(_in_folder(aid, target)) == 1        # one conversation, two messages
    assert mid not in _in_folder(aid, mailbox(email, "inbox")["id"])


def test_bulk_move_refuses_a_selection_from_another_account(account):
    email, aid = account["email"], account["id"]
    token = "BULKXA" + uuid.uuid4().hex[:6]
    mid, _ = ingest_one(email, aid, token)
    target = _kept(email)

    code, body = api("POST", "/api/messages/bulk/move", {
        "items": [{"account_id": aid, "message_id": mid},
                  {"account_id": aid + 9999, "message_id": mid}],
        "target_mailbox_id": target,
    })
    assert code == 400
    assert "same account" in body["detail"]
    # And nothing was half-applied to the rows that did match.
    assert mid in _in_folder(aid, mailbox(email, "inbox")["id"])


def test_bulk_move_rejects_an_unknown_target(account):
    email, aid = account["email"], account["id"]
    mid, _ = ingest_one(email, aid, "BULKNT" + uuid.uuid4().hex[:6])

    code, _ = api("POST", "/api/messages/bulk/move", {
        "items": [{"account_id": aid, "message_id": mid}],
        "target_mailbox_id": 99999999,
    })
    assert code == 400


def test_bulk_move_all_empties_the_folder(account):
    email, aid = account["email"], account["id"]
    for i in range(3):
        ingest_one(email, aid, "BULKALL" + uuid.uuid4().hex[:6], uid=10 + i)
    inbox = mailbox(email, "inbox")["id"]
    target = _kept(email)

    code, body = api("POST", "/api/messages/bulk/move-all", {
        "mailbox_id": inbox, "target_mailbox_id": target,
    })
    assert code == 200
    assert body["moved"] == 3
    assert body["done"] is True
    assert not _in_folder(aid, inbox)
    assert len(_in_folder(aid, target)) == 3


def test_bulk_move_all_into_the_folder_it_is_reading(account):
    """The one selector that can never move anything, said rather than looped on."""
    email, aid = account["email"], account["id"]
    ingest_one(email, aid, "BULKSELF" + uuid.uuid4().hex[:6])
    inbox = mailbox(email, "inbox")["id"]

    code, _ = api("POST", "/api/messages/bulk/move-all", {
        "mailbox_id": inbox, "target_mailbox_id": inbox,
    })
    assert code == 400


# --- An account nothing syncs ------------------------------------------------


def _import_one(email: str, token: str, folder: str) -> None:
    raw = make_message(f"<m-{uuid.uuid4().hex}@t>", f"Subj {token}", "s@ex.com",
                       email, f"{token} body", T0)
    dbfixture.import_raw_message(email, raw, folder=folder)


def test_local_bulk_move_files_without_queueing_anything(local_account):
    """The imported-into-the-wrong-folder case, which is what this is for."""
    email, aid = local_account["email"], local_account["id"]
    wrong = "Wrong" + uuid.uuid4().hex[:6]
    token = "LOCMV" + uuid.uuid4().hex[:6]
    _import_one(email, token, wrong)

    assert api("POST", "/api/mailboxes",
               {"account_id": aid, "name": "Right/2024"})[0] == 201
    source = _folder_id(aid, wrong)
    target = _folder_id(aid, "Right/2024")

    code, body = api("POST", "/api/messages/bulk/move-all", {
        "mailbox_id": source, "target_mailbox_id": target,
    })
    assert code == 200
    assert body["moved"] == 1
    assert body["done"] is True
    assert not _in_folder(aid, source)
    assert len(_in_folder(aid, target)) == 1

    # Nothing was left waiting for an agent that is never coming.
    assert not dbfixture.pending_actions(email, "move")


def test_local_move_placement_carries_a_real_uid(local_account):
    """Not the negative "we wrote this ourselves" UID.

    That sign means "a move is queued and the server has not confirmed it yet",
    and here nothing was queued and nothing is going to confirm anything — so
    the placement would have read as permanently in flight, and every later
    keypress on the message would have been answered with "still being moved".
    """
    email, aid = local_account["email"], local_account["id"]
    token = "LOCUID" + uuid.uuid4().hex[:6]
    _import_one(email, token, "Wrong" + uuid.uuid4().hex[:6])
    mid = _row(aid, token)["id"]

    assert api("POST", "/api/mailboxes", {"account_id": aid, "name": "Right"})[0] == 201
    target = _folder_id(aid, "Right")
    source = next(p for p in dbfixture.placements(email, mid) if not p["deleted"])

    code, _ = api("POST", "/api/messages/bulk/move", {
        "items": [{"account_id": aid, "message_id": mid}],
        "target_mailbox_id": target,
    })
    assert code == 200

    placed = dbfixture.placements(email, mid)
    assert [p["imap_name"] for p in placed] == ["Right"]
    assert placed[0]["imap_uid"] > 0
    assert source["imap_name"] != "Right"


def test_local_move_is_undoable(local_account):
    """No queue row means no Undo, unless one is written as a record — which is
    the one place a bulk move most needs it, since no mail server is holding a
    second copy of where things were."""
    email, aid = local_account["email"], local_account["id"]
    token = "LOCUNDO" + uuid.uuid4().hex[:6]
    home = "Wrong" + uuid.uuid4().hex[:6]
    _import_one(email, token, home)
    mid = _row(aid, token)["id"]

    assert api("POST", "/api/mailboxes", {"account_id": aid, "name": "Right"})[0] == 201
    target = _folder_id(aid, "Right")
    _, body = api("POST", "/api/messages/bulk/move", {
        "items": [{"account_id": aid, "message_id": mid}],
        "target_mailbox_id": target,
    })
    op_id = body["op_id"]

    _, recent = api("GET", "/api/actions/recent?limit=5")
    entry = next(i for i in recent["items"] if i["op_id"] == op_id)
    assert entry["undoable"] is True

    assert api("POST", f"/api/actions/{op_id}/undo")[0] == 200
    assert [p["imap_name"] for p in dbfixture.placements(email, mid)] == [home]
