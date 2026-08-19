"""Deleting a folder, and what goes with it.

The other half of the + button. An mbox that went in under the wrong name is
twenty folders somebody never meant to have, several of them empty, and until
this there was no button anywhere in meerail that removed one: emptying them
left the folders themselves standing, and the only way out was a re-import.

Two things are pinned down here. What the delete takes — the children under it,
and the mail that this folder was the last home of, which has to *stop existing*
rather than have its placement dropped out from under it (the state
mailops.purge was written for; the Mailbox row's own cascade arrives at it by a
different door). And what it asks first: nothing at all for an empty folder,
because a dialog about nothing is a dialog people learn to click through, and
the counts otherwise.
"""

import uuid

import dbfixture
from conftest import T0
from helpers import api, make_message


def _mailboxes(account_id: int) -> list[dict]:
    _, sidebar = api("GET", "/api/mailboxes")
    return next(a for a in sidebar["accounts"] if a["id"] == account_id)["mailboxes"]


def _folder(account_id: int, imap_name: str) -> dict | None:
    return next((m for m in _mailboxes(account_id) if m["imap_name"] == imap_name), None)


def _make(account_id: int, name: str) -> int:
    code, body = api("POST", "/api/mailboxes", {"account_id": account_id, "name": name})
    assert code == 201, body        # imported accounts get the row now, not a queue slot
    return body["mailbox_id"]


def _import_one(email: str, token: str, folder: str) -> None:
    raw = make_message(f"<m-{uuid.uuid4().hex}@t>", f"Subj {token}", "s@ex.com",
                       email, f"{token} body", T0)
    dbfixture.import_raw_message(email, raw, folder=folder)


def _pk(account_id: int, token: str) -> int:
    _, r = api("GET", f"/api/search?q={token}&account_id={account_id}")
    return r["rows"][0]["id"]


# --- The empty ones, which is what the question was about ---------------------


def test_an_empty_folder_goes_on_the_first_request(local_account):
    """"Ein paar sind leer und sinnlos" — one click, no dialog. Confirmation is
    asked for when there is something to lose, and an empty folder with nothing
    under it has nothing."""
    aid = local_account["id"]
    name = "Empty" + uuid.uuid4().hex[:6]
    mailbox_id = _make(aid, name)

    code, body = api("DELETE", f"/api/mailboxes/{mailbox_id}")
    assert code == 200, body
    assert body == {"ok": True, "folders": 1, "deleted": 0, "held": 0}
    assert _folder(aid, name) is None


def test_deleting_a_folder_that_is_already_gone_is_a_404(local_account):
    aid = local_account["id"]
    mailbox_id = _make(aid, "Twice" + uuid.uuid4().hex[:6])
    assert api("DELETE", f"/api/mailboxes/{mailbox_id}")[0] == 200
    assert api("DELETE", f"/api/mailboxes/{mailbox_id}")[0] == 404


# --- The ones holding something ------------------------------------------------


def test_a_folder_holding_mail_has_to_be_confirmed(local_account):
    """The refusal names what is at stake, because only this side knows it: the
    sidebar has a count per folder and no idea which others hang off this one."""
    email, aid = local_account["email"], local_account["id"]
    token = "FOLDMAIL" + uuid.uuid4().hex[:6]
    name = "Holding" + uuid.uuid4().hex[:6]
    _import_one(email, token, name)
    mid = _pk(aid, token)
    mailbox_id = _folder(aid, name)["id"]

    code, body = api("DELETE", f"/api/mailboxes/{mailbox_id}")
    assert code == 409, body
    assert "1 message" in body["detail"]
    # Refused means nothing happened — not "deleted the mail and kept the folder".
    assert _folder(aid, name) is not None
    assert dbfixture.message_survives(mid)["message"] is True

    code, body = api("DELETE", f"/api/mailboxes/{mailbox_id}?confirm=true")
    assert code == 200, body
    assert body["deleted"] == 1 and body["held"] == 1
    assert _folder(aid, name) is None
    # Gone, rather than filed in no folder at all: that state is invisible in
    # every list and still holds the raw MIME and every attachment byte.
    assert dbfixture.message_survives(mid) == {
        "message": False, "attachments": 0, "locations": 0}
    assert api("GET", f"/api/messages/{mid}")[0] == 404


def test_deleting_a_parent_takes_the_folders_under_it(local_account):
    """"Ich habe alles in old importiert und darin nun 20 Ordner." Deleting the
    parent is deleting them: a child left behind is a row whose parent is gone,
    which the sidebar then draws at the top level under a name that no longer
    says where it came from."""
    email, aid = local_account["email"], local_account["id"]
    parent = "Old" + uuid.uuid4().hex[:6]
    token = "FOLDKID" + uuid.uuid4().hex[:6]
    _make(aid, parent)
    _make(aid, f"{parent}/Empty")
    _import_one(email, token, f"{parent}/Full")
    mid = _pk(aid, token)
    mailbox_id = _folder(aid, parent)["id"]

    code, body = api("DELETE", f"/api/mailboxes/{mailbox_id}")
    assert code == 409, body
    assert "1 message" in body["detail"] and "2 folders inside it" in body["detail"]

    code, body = api("DELETE", f"/api/mailboxes/{mailbox_id}?confirm=true")
    assert code == 200, body
    assert body["folders"] == 3 and body["deleted"] == 1
    names = [m["imap_name"] for m in _mailboxes(aid)]
    assert not [n for n in names if n == parent or n.startswith(parent + "/")]
    assert dbfixture.message_survives(mid)["message"] is False


def test_mail_filed_somewhere_else_as_well_survives(local_account):
    """An mbox imported twice into two folders is one message wearing two
    placements, and deleting one of those folders is not permission to destroy
    the copy in the other. The placement goes; the message stays."""
    email, aid = local_account["email"], local_account["id"]
    token = "FOLDKEEP" + uuid.uuid4().hex[:6]
    keep = "Keep" + uuid.uuid4().hex[:6]
    drop = "Drop" + uuid.uuid4().hex[:6]
    raw = make_message(f"<m-{uuid.uuid4().hex}@t>", f"Subj {token}", "s@ex.com",
                       email, f"{token} body", T0)
    dbfixture.import_raw_message(email, raw, folder=keep)
    dbfixture.import_raw_message(email, raw, folder=drop)
    mid = _pk(aid, token)
    assert len(dbfixture.placements(email, mid)) == 2

    code, body = api("DELETE", f"/api/mailboxes/{_folder(aid, drop)['id']}?confirm=true")
    assert code == 200, body
    assert body["deleted"] == 0 and body["held"] == 1      # a placement went, no message did
    assert [p["imap_name"] for p in dbfixture.placements(email, mid)] == [keep]
    assert dbfixture.message_survives(mid)["message"] is True


# --- What it refuses -----------------------------------------------------------


def test_a_folder_a_mail_server_owns_is_refused(account):
    """Dropping the row would delete this app's copy of everything in it and
    then watch the next LIST put the folder straight back, empty. A folder on a
    server is deleted on the server."""
    email, aid = account["email"], account["id"]
    name = "Server" + uuid.uuid4().hex[:6]
    mailbox_id = dbfixture.create_folder(email, name)

    code, body = api("DELETE", f"/api/mailboxes/{mailbox_id}?confirm=true")
    assert code == 400, body
    assert "mail server" in body["detail"]
    assert _folder(aid, name) is not None


# --- Mail the user cannot see -------------------------------------------------


def test_mail_the_user_cannot_see_does_not_make_a_folder_look_full(local_account):
    """"Der Ordner ist leer... dann sagt er mir das dort zwei Mails drinne sind."

    An mbox export carries its Status letters, "D" among them, and the importer
    stores that flag as it finds it (tools/import_mbox.py). A placement wearing
    \\Deleted is invisible in the list, in search and in the sidebar's count, so
    the folder is empty to everyone looking at it — and the delete used to count
    those placements anyway and refuse with a number nothing on screen agreed
    with. It goes on the first request, like any other empty folder, and the
    placements go with it.
    """
    email, aid = local_account["email"], local_account["id"]
    token = "FOLDGHOST" + uuid.uuid4().hex[:6]
    name = "Ghosts" + uuid.uuid4().hex[:6]
    raw = make_message(f"<m-{uuid.uuid4().hex}@t>", f"Subj {token}", "s@ex.com",
                       email, f"{token} body", T0)
    dbfixture.import_raw_message(email, raw, folder=name, flags={"deleted": True})
    mailbox_id = _folder(aid, name)["id"]
    assert _folder(aid, name)["total"] == 0          # what the sidebar drew

    code, body = api("DELETE", f"/api/mailboxes/{mailbox_id}")
    assert code == 200, body
    assert body["held"] == 0 and body["folders"] == 1
    assert body["deleted"] == 1                      # the placement's mail still went
    assert _folder(aid, name) is None


def test_visible_mail_beside_hidden_mail_is_still_confirmed(local_account):
    """The other half of it: \\Deleted placements are not counted, and that is
    not the same as counting nothing. One readable message in the folder and the
    refusal is the refusal, naming the one."""
    email, aid = local_account["email"], local_account["id"]
    name = "Mixed" + uuid.uuid4().hex[:6]
    for token, flags in ((f"FOLDSEEN{uuid.uuid4().hex[:6]}", None),
                         (f"FOLDGONE{uuid.uuid4().hex[:6]}", {"deleted": True})):
        raw = make_message(f"<m-{uuid.uuid4().hex}@t>", f"Subj {token}", "s@ex.com",
                           email, f"{token} body", T0)
        dbfixture.import_raw_message(email, raw, folder=name, flags=flags)
    mailbox_id = _folder(aid, name)["id"]

    code, body = api("DELETE", f"/api/mailboxes/{mailbox_id}")
    assert code == 409, body
    assert "1 message" in body["detail"]

    code, body = api("DELETE", f"/api/mailboxes/{mailbox_id}?confirm=true")
    assert code == 200, body
    assert body["held"] == 1 and body["deleted"] == 2   # both placements' mail goes
