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
    """"Parent/Child" is here because this account's server has not said it can
    nest — see the pair of tests below, where one that has said so takes it."""
    aid = account["id"]
    for bad in ["", "   ", "/", "Parent/Child", 'quo"te', "star*", "per%cent",
                "back\\slash", "bell\x07"]:
        code, _ = api("POST", "/api/mailboxes", {"account_id": aid, "name": bad})
        assert code == 400, f"expected 400 for {bad!r}, got {code}"


# --- Nesting, where the mail server allows it --------------------------------
#
# Refusing "/" everywhere was Proton Bridge's answer applied to every account.
# The agent now reads the real one off IMAP's LIST once a pass; these are what
# the app does with it.


def test_a_server_that_cannot_nest_says_so(account):
    email, aid = account["email"], account["id"]
    dbfixture.set_folder_capabilities(email, "/", False)

    code, body = api("POST", "/api/mailboxes", {"account_id": aid, "name": "Archive/2024"})
    assert code == 400
    assert "does not allow folders inside folders" in body["detail"]


def test_a_server_that_can_nest_takes_the_path(account):
    email, aid = account["email"], account["id"]
    dbfixture.set_folder_capabilities(email, "/", True)
    parent = "Pytest" + uuid.uuid4().hex[:6]

    code, body = api("POST", "/api/mailboxes",
                     {"account_id": aid, "name": f"{parent}/2024"})
    assert code == 202
    assert body["name"] == f"{parent}/2024"

    # Segments, not a joined string: "/" is what a person types, and the agent
    # is the only side that knows what the server puts between a parent and a
    # child. `name` rides along for the log and for an older agent.
    queued = next(a for a in _creates(email) if a["payload"]["name"] == f"{parent}/2024")
    assert queued["payload"]["segments"] == [parent, "2024"]


def test_nesting_still_refuses_a_broken_path(account):
    email, aid = account["email"], account["id"]
    dbfixture.set_folder_capabilities(email, "/", True)

    for bad in ["//", "a//b", "a/ /b", 'a/"b', "1/2/3/4/5/6/7/8/9"]:
        code, _ = api("POST", "/api/mailboxes", {"account_id": aid, "name": bad})
        assert code == 400, f"expected 400 for {bad!r}, got {code}"


def test_a_dovecot_style_delimiter_nests_the_sidebar(account):
    """The folder names a "."-delimited server hands back are still a tree, and
    the sidebar has to draw it as one — splitting on "/" would see one flat
    folder called "INBOX.Archive"."""
    email, aid = account["email"], account["id"]
    dbfixture.set_folder_capabilities(email, ".", True)
    dbfixture.create_folder(email, "INBOX", role_hint="\\inbox")
    dbfixture.create_folder(email, "INBOX.Archive")
    dbfixture.create_folder(email, "INBOX.Archive.2024")

    folders = _folders(aid)
    assert folders["INBOX.Archive"]["depth"] == 1        # under INBOX
    assert folders["INBOX.Archive.2024"]["depth"] == 2
    assert folders["INBOX.Archive.2024"]["path"] == "INBOX/Archive/2024"

    # ...and the name a person types is compared against that path, not against
    # the server's spelling of it.
    code, _ = api("POST", "/api/mailboxes",
                  {"account_id": aid, "name": "INBOX/Archive/2024"})
    assert code == 409


def test_create_folder_unknown_account(require_server):
    code, _ = api("POST", "/api/mailboxes", {"account_id": 99999999, "name": "Nope"})
    assert code == 404


# --- Accounts with no agent (imported mail) ----------------------------------
#
# The + button used to answer 202 "queued" here and nothing ever happened: the
# queue it wrote to is drained by an agent, and an imported account has none.


def _folders(account_id):
    _, sidebar = api("GET", "/api/mailboxes")
    acc = next(a for a in sidebar["accounts"] if a["id"] == account_id)
    return {m["imap_name"]: m for m in acc["mailboxes"]}


def test_local_create_folder_makes_it_now(local_account):
    email, aid = local_account["email"], local_account["id"]
    name = "Pytest" + uuid.uuid4().hex[:6]

    code, body = api("POST", "/api/mailboxes", {"account_id": aid, "name": name})
    assert code == 201
    assert body["status"] == "created"

    assert name in _folders(aid)
    # Nothing queued: there is nobody to drain it, and a row left on that queue
    # is what made this look broken in the first place.
    assert not _creates(email)


def test_local_create_folder_nests_and_makes_the_parent(local_account):
    aid = local_account["id"]
    parent = "Pytest" + uuid.uuid4().hex[:6]

    code, _ = api("POST", "/api/mailboxes", {"account_id": aid, "name": f"{parent}/2024/Q1"})
    assert code == 201

    folders = _folders(aid)
    assert folders[parent]["depth"] == 0
    assert folders[f"{parent}/2024"]["depth"] == 1
    assert folders[f"{parent}/2024/Q1"]["depth"] == 2
    # The leaf is what the sidebar prints; the path is what the move menu and
    # the list header need, since "Q1" alone names nothing.
    assert folders[f"{parent}/2024/Q1"]["display_name"] == "Q1"
    assert folders[f"{parent}/2024/Q1"]["path"] == f"{parent}/2024/Q1"


def test_local_create_folder_sorts_children_under_their_parent(local_account):
    aid = local_account["id"]
    parent = "Pytest" + uuid.uuid4().hex[:6]
    for name in (f"{parent}/Zebra", f"{parent}/Ant", parent + "zzz"):
        assert api("POST", "/api/mailboxes", {"account_id": aid, "name": name})[0] == 201

    _, sidebar = api("GET", "/api/mailboxes")
    acc = next(a for a in sidebar["accounts"] if a["id"] == aid)
    order = [m["imap_name"] for m in acc["mailboxes"]]
    # Alphabetically "<parent>zzz" sorts between the parent and its children;
    # the tree has to keep the children with the parent regardless.
    assert order.index(f"{parent}/Ant") == order.index(parent) + 1
    assert order.index(f"{parent}/Zebra") == order.index(parent) + 2
    assert order.index(parent + "zzz") > order.index(f"{parent}/Zebra")


def test_local_create_folder_same_leaf_under_two_parents(local_account):
    """"2024" under Archive and "2024" under Receipts are two folders."""
    aid = local_account["id"]
    stem = "Pytest" + uuid.uuid4().hex[:6]

    assert api("POST", "/api/mailboxes", {"account_id": aid, "name": f"{stem}A/2024"})[0] == 201
    assert api("POST", "/api/mailboxes", {"account_id": aid, "name": f"{stem}B/2024"})[0] == 201

    folders = _folders(aid)
    assert f"{stem}A/2024" in folders and f"{stem}B/2024" in folders
    # ...and a bare "2024" at the top level is a third, not a duplicate of
    # either: on a local account the path is the whole name.
    assert api("POST", "/api/mailboxes", {"account_id": aid, "name": "2024"})[0] == 201


def test_local_create_folder_rejects_duplicate_path(local_account):
    aid = local_account["id"]
    name = "Pytest" + uuid.uuid4().hex[:6] + "/2024"

    assert api("POST", "/api/mailboxes", {"account_id": aid, "name": name})[0] == 201
    assert api("POST", "/api/mailboxes", {"account_id": aid, "name": name})[0] == 409


def test_local_create_folder_rejects_bad_paths(local_account):
    aid = local_account["id"]
    # "a/b " is not here: the whole name is trimmed before it is split, the same
    # way a bare leaf is (see test_create_folder_trims_whitespace). What is
    # refused is a space that is *inside* the path, where trimming it would be
    # silently filing under a name nobody typed.
    for bad in ["", "   ", "/", "//", "a//b", "a/ /b", "a /b", 'a/"b', "a/b*",
                "1/2/3/4/5/6/7/8/9"]:
        code, _ = api("POST", "/api/mailboxes", {"account_id": aid, "name": bad})
        assert code == 400, f"expected 400 for {bad!r}, got {code}"


def test_bridge_namespaced_folders_do_not_indent(account):
    """"Folders/Receipts" is Bridge's namespace, not a folder called "Folders" —
    so the row stays at the top level. Nesting on the separator alone would
    indent an entire Proton account under a heading that is not in the list."""
    email, aid = account["email"], account["id"]
    leaf = "Pytest" + uuid.uuid4().hex[:6]
    dbfixture.create_folder(email, f"Folders/{leaf}")

    folders = _folders(aid)
    assert folders[f"Folders/{leaf}"]["depth"] == 0
    assert folders[f"Folders/{leaf}"]["path"] == leaf
