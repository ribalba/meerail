"""Integration tests for message actions (read/flag/trash) + agent-action queue."""

import uuid
from datetime import timedelta

import dbfixture
from conftest import T0, ingest_one
from helpers import api, make_message


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


def test_archive_thread_clears_every_message_and_every_folder(account):
    """Send & Archive files the conversation, not the message that was replied to.

    The two things that used to leave the row sitting in the list are both here:
    a second message in the thread, and a message filed under a label as well as
    the inbox — archiving one placement left the other holding the row.
    """
    email, aid = account["email"], account["id"]
    a, b = (f"{p}-{uuid.uuid4().hex}@t" for p in ("a", "b"))
    tok = "ARCHTOK" + uuid.uuid4().hex[:6]
    A = make_message(f"<{a}>", f"Subject {tok}", "x@y.com", email, f"{tok} body", T0)
    B = make_message(f"<{b}>", f"Re: Subject {tok}", "z@y.com", email, f"{tok} reply",
                     T0 + timedelta(hours=1), in_reply_to=f"<{a}>", refs=[f"<{a}>"])
    for uid, raw in enumerate((A, B), start=1):
        dbfixture.ingest_raw_message(email, raw, uid=uid)
    # The same content under a second Proton label, as a real mailbox has.
    dbfixture.record_placement(email, a, uid=101, folder="Labels/Work")
    # An Archive folder to file into.
    dbfixture.ingest_raw_message(email, make_message(
        f"<seed-{uuid.uuid4().hex}@t>", "Seed", "x@y.com", email, "seed", T0),
        uid=1, folder="Archive", role_hint="\\Archive")

    _, boxes = api("GET", "/api/mailboxes")
    mine = next(a_ for a_ in boxes["accounts"] if a_["email"] == email)["mailboxes"]
    inbox = next(m for m in mine if m["role"] == "inbox")
    label = next(m for m in mine if m["imap_name"] == "Labels/Work")

    _, rows = api("GET", f"/api/messages?mailbox_id={inbox['id']}&limit=50")
    row = next(r for r in rows["rows"] if tok in r["subject"])
    assert row["thread_count"] == 2

    code, body = api("POST", f"/api/messages/threads/{row['thread_id']}/archive"
                             f"?account_id={aid}")
    assert code == 200, body
    assert body["moved"] == 3          # two inbox placements + the label

    # Gone from the folder that was on screen — and from the label, which is
    # what a "vanish from the list" that only half happened looked like.
    _, rows = api("GET", f"/api/messages?mailbox_id={inbox['id']}&limit=50")
    assert not [r for r in rows["rows"] if tok in r["subject"]]
    _, rows = api("GET", f"/api/messages?mailbox_id={label['id']}&limit=50")
    assert not [r for r in rows["rows"] if tok in r["subject"]]

    # Three moves queued for the agent, so IMAP ends up agreeing.
    moves = [x for x in _actions(email) if x["type"] == "move"]
    assert len([m for m in moves if m["payload"]["to_folder"] == "Archive"]) == 3


def test_archive_thread_is_idempotent(account):
    """A second press must not 400 on messages already sitting in Archive."""
    email, aid = account["email"], account["id"]
    tok = "IDEMTOK" + uuid.uuid4().hex[:6]
    mid_rfc = f"i-{uuid.uuid4().hex}@t"
    dbfixture.ingest_raw_message(email, make_message(
        f"<{mid_rfc}>", f"Subject {tok}", "x@y.com", email, f"{tok} body", T0), uid=1)
    dbfixture.ingest_raw_message(email, make_message(
        f"<seed-{uuid.uuid4().hex}@t>", "Seed", "x@y.com", email, "seed", T0),
        uid=1, folder="Archive", role_hint="\\Archive")

    _, r = api("GET", f"/api/search?q={tok}&account_id={aid}")
    thread_id = r["rows"][0]["thread_id"]

    code, first = api("POST", f"/api/messages/threads/{thread_id}/archive?account_id={aid}")
    assert code == 200 and first["moved"] == 1
    code, again = api("POST", f"/api/messages/threads/{thread_id}/archive?account_id={aid}")
    assert code == 200 and again["moved"] == 0


def test_bulk_trash_clears_every_selected_row(account):
    """Ctrl-A then Delete: the whole ticked set goes in one request.

    Selections are conversations, so a two-message thread has to leave whole —
    the same reason the reader trashes by thread rather than by message.
    """
    email, aid = account["email"], account["id"]
    tok = "BULKTOK" + uuid.uuid4().hex[:6]
    a = f"a-{uuid.uuid4().hex}@t"
    # One standalone message, and one thread of two.
    dbfixture.ingest_raw_message(email, make_message(
        f"<s-{uuid.uuid4().hex}@t>", f"Solo {tok}", "x@y.com", email, f"{tok} body", T0), uid=1)
    dbfixture.ingest_raw_message(email, make_message(
        f"<{a}>", f"Thread {tok}", "x@y.com", email, f"{tok} body", T0), uid=2)
    dbfixture.ingest_raw_message(email, make_message(
        f"<b-{uuid.uuid4().hex}@t>", f"Re: Thread {tok}", "z@y.com", email, f"{tok} reply",
        T0 + timedelta(hours=1), in_reply_to=f"<{a}>", refs=[f"<{a}>"]), uid=3)

    _, boxes = api("GET", "/api/mailboxes")
    inbox_id = next(m["id"] for a_ in boxes["accounts"] for m in a_["mailboxes"]
                    if a_["email"] == email and m["role"] == "inbox")
    _, rows = api("GET", f"/api/messages?mailbox_id={inbox_id}&limit=50")
    mine = [r for r in rows["rows"] if tok in r["subject"]]
    assert len(mine) == 2                       # two conversations, three messages

    items = [{"account_id": aid, "thread_id": r["thread_id"],
              "message_id": None if r["thread_id"] else r["id"]} for r in mine]
    code, body = api("POST", "/api/messages/bulk/trash", {"items": items})
    assert code == 200, body
    assert body["moved"] == 3                   # the reply went too

    _, rows = api("GET", f"/api/messages?mailbox_id={inbox_id}&limit=50")
    assert not [r for r in rows["rows"] if tok in r["subject"]]


def test_bulk_trash_skips_rows_that_already_went(account):
    """A row trashed in another window must not fail the rest of the batch."""
    email, aid = account["email"], account["id"]
    tok = "GONETOK" + uuid.uuid4().hex[:6]
    mid, _ = ingest_one(email, aid, tok)
    _, r = api("GET", f"/api/search?q={tok}&account_id={aid}")
    thread_id = r["rows"][0]["thread_id"]

    items = [{"account_id": aid, "thread_id": thread_id, "message_id": None},
             {"account_id": aid, "thread_id": f"no-such-{uuid.uuid4().hex}", "message_id": None}]
    code, body = api("POST", "/api/messages/bulk/trash", {"items": items})
    assert code == 200, body
    assert body["moved"] == 1


def test_bulk_trash_all_empties_the_selected_mailbox(account):
    """The escalated "select all N in this folder" path deletes past the page."""
    email = account["email"]
    tok = "ALLTOK" + uuid.uuid4().hex[:6]
    for uid in range(1, 6):
        dbfixture.ingest_raw_message(email, make_message(
            f"<x{uid}-{uuid.uuid4().hex}@t>", f"Bulk {tok} {uid}", "x@y.com", email,
            f"{tok} body", T0), uid=uid)

    _, boxes = api("GET", "/api/mailboxes")
    inbox_id = next(m["id"] for a_ in boxes["accounts"] for m in a_["mailboxes"]
                    if a_["email"] == email and m["role"] == "inbox")

    code, body = api("POST", "/api/messages/bulk/trash-all", {"mailbox_id": inbox_id})
    assert code == 200, body
    assert body["done"] is True and body["moved"] >= 5

    _, rows = api("GET", f"/api/messages?mailbox_id={inbox_id}&limit=50")
    assert rows["rows"] == [] and rows["total"] == 0
    assert mailbox_total(email) == 0


def mailbox_total(email):
    _, body = api("GET", "/api/sync/status")
    st = next(r for r in body["accounts"] if r["email"] == email)
    return next(m["total"] for m in st["mailboxes"] if m["role"] == "inbox")
