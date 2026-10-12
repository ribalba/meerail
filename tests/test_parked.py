"""Integration tests for Parked, the local-only folder inside the inbox.

Parking never talks to the mail server. It sets a mark on a conversation's inbox
placements, so everything here is about three promises. The mail must leave the
inbox lists and counts and show up in Parked, nothing must be queued for the
agent, and a new reply must be able to bring a conversation back by itself.
"""

import uuid

import dbfixture
from conftest import T0, ingest_one, mailbox
from helpers import api, make_message


def _seed_archive(email):
    dbfixture.ingest_raw_message(email, make_message(
        f"<archive-seed-{uuid.uuid4().hex}@t>", "Seed", "x@y.com", email, "seed", T0),
        uid=1, folder="Archive", role_hint="\\Archive")


def _ids(rows):
    return [r["id"] for r in rows]


def _list(params):
    code, body = api("GET", "/api/messages?" + params + "&limit=200")
    assert code == 200, body
    return body["rows"]


def _parked_ids():
    return _ids(_list("scope=parked"))


def _inbox_ids(email):
    return _ids(_list(f"mailbox_id={mailbox(email, 'inbox')['id']}"))


def _unified_ids():
    return _ids(_list("scope=unified_inbox"))


def _sidebar(email):
    _, sidebar = api("GET", "/api/mailboxes")
    acc = next(a for a in sidebar["accounts"] if a["email"] == email)
    return {m["role"]: m for m in acc["mailboxes"]}, sidebar["smart"]


def _park(mid, parked=1):
    return api("POST", f"/api/messages/{mid}/park?parked={parked}")


def _conversation(email, aid, token):
    """Two messages in one thread, both in INBOX. Returns (root_pk, reply_pk, thread_id, root_rfc)."""
    root_pk, rfc = ingest_one(email, aid, token)
    reply_rfc = f"r-{uuid.uuid4().hex}@t"
    dbfixture.ingest_raw_message(email, make_message(
        f"<{reply_rfc}>", f"Re: Subj {token}", "b@ex.com", email, f"{token} reply", T0,
        in_reply_to=f"<{rfc}>", refs=[f"<{rfc}>"]), uid=2)
    reply_pk = dbfixture.message_pk_for(email, reply_rfc)
    _, r = api("GET", f"/api/search?q={token}&account_id={aid}")
    thread_id = r["rows"][0]["thread_id"]
    assert thread_id
    return root_pk, reply_pk, thread_id, rfc


def _recent():
    code, body = api("GET", "/api/actions/recent")
    assert code == 200, body
    return body["items"]


def test_parking_hides_the_conversation_from_the_inbox_and_lists_it_in_parked(account):
    """The point of the feature. Out of the inbox lists, and into Parked."""
    email, aid = account["email"], account["id"]
    root, reply, _, _ = _conversation(email, aid, "PRK" + uuid.uuid4().hex[:8])

    code, body = _park(root)
    assert code == 200, body
    assert body["ok"] and body["parked"] is True and body["op_kind"] == "park"
    assert body["moved"] == 2 and body["op_id"]

    assert root not in _inbox_ids(email) and reply not in _inbox_ids(email)
    assert root not in _unified_ids() and reply not in _unified_ids()
    assert {root, reply} & set(_parked_ids())


def test_parking_queues_nothing_for_the_agent(account):
    """No mail server is told, so the only rows are settled records for Recent actions."""
    email, aid = account["email"], account["id"]
    root, reply, _, _ = _conversation(email, aid, "PRKQ" + uuid.uuid4().hex[:8])

    code, body = _park(root)
    assert code == 200, body

    assert dbfixture.pending_actions(email) == []
    done = dbfixture.pending_actions(email, "park", status="done")
    assert sorted(a["message_pk"] for a in done) == sorted([root, reply])
    for a in done:
        p = a["payload"]
        assert p["op_id"] == body["op_id"]
        assert p["op_kind"] == "park" and p["parked"] is True
        assert p["mailbox_ids"]


def test_mail_stays_filed_in_the_inbox_with_its_uid(account):
    """Parking is a mark, not a move, so the server's copy is never touched."""
    email, aid = account["email"], account["id"]
    root, _ = ingest_one(email, aid, "PRKU" + uuid.uuid4().hex[:8])
    assert _park(root)[0] == 200

    locs = dbfixture.parked_placements(email, root)
    assert locs == [{"imap_name": "INBOX", "imap_uid": 1, "deleted": False, "parked": True}]


def test_unparking_puts_the_conversation_back(account):
    email, aid = account["email"], account["id"]
    root, reply, _, _ = _conversation(email, aid, "PRKB" + uuid.uuid4().hex[:8])
    _park(root)

    code, body = _park(root, 0)
    assert code == 200, body
    assert body["parked"] is False and body["op_kind"] == "unpark" and body["moved"] == 2
    assert root in _inbox_ids(email) or reply in _inbox_ids(email)
    assert root not in _parked_ids() and reply not in _parked_ids()


def test_park_refusals(account):
    """Each refusal says what is wrong, and a refused press changes nothing."""
    email, aid = account["email"], account["id"]
    _seed_archive(email)
    mid, _ = ingest_one(email, aid, "PRKR" + uuid.uuid4().hex[:8])

    code, body = _park(mid, 0)
    assert code == 409 and body["detail"] == "This is not in Parked."

    assert _park(mid)[0] == 200
    code, body = _park(mid)
    assert code == 409 and body["detail"] == "This is already in Parked."

    _, r = api("GET", f"/api/search?q=Seed&account_id={aid}")
    archived = next(x["id"] for x in r["rows"] if x["subject"] == "Seed")
    code, body = _park(archived)
    assert code == 400
    assert body["detail"] == ("Only mail in an inbox can be parked, and this "
                              "conversation is not in one.")


def test_a_new_unparked_reply_brings_the_conversation_back(account):
    """Parked must never hide fresh mail, and the old marks stay as they were."""
    email, aid = account["email"], account["id"]
    token = "PRKN" + uuid.uuid4().hex[:8]
    root, rfc = ingest_one(email, aid, token)
    assert _park(root)[0] == 200
    assert root in _parked_ids()

    reply_rfc = f"r-{uuid.uuid4().hex}@t"
    dbfixture.ingest_raw_message(email, make_message(
        f"<{reply_rfc}>", f"Re: Subj {token}", "b@ex.com", email, f"{token} again", T0,
        in_reply_to=f"<{rfc}>", refs=[f"<{rfc}>"]), uid=2)
    reply = dbfixture.message_pk_for(email, reply_rfc)

    assert {root, reply} & set(_inbox_ids(email))
    assert root not in _parked_ids() and reply not in _parked_ids()
    assert dbfixture.parked_placements(email, root)[0]["parked"] is True


def test_sidebar_counts_exclude_parked_and_parked_counts_placements(account):
    email, aid = account["email"], account["id"]
    other, _ = ingest_one(email, aid, "PRKS1" + uuid.uuid4().hex[:8], uid=5)
    root, reply, _, _ = _conversation(email, aid, "PRKS2" + uuid.uuid4().hex[:8])

    boxes, smart = _sidebar(email)
    before_total, before_unread = boxes["inbox"]["total"], boxes["inbox"]["unread"]
    before_unified = smart["unified_inbox_unread"]
    assert before_total == 3 and before_unread == 3
    assert smart["parked_total"] == 0

    assert _park(root)[0] == 200
    boxes, smart = _sidebar(email)
    assert boxes["inbox"]["total"] == before_total - 2
    assert boxes["inbox"]["unread"] == before_unread - 2
    assert smart["unified_inbox_unread"] == before_unified - 2
    assert smart["parked_total"] == 2 and smart["parked_unread"] == 2


def test_parked_lists_conversations_across_accounts(account, local_account):
    """Parked is one folder for the whole install, like the unified inbox."""
    email, aid = account["email"], account["id"]
    other_email = local_account["email"]
    a, _ = ingest_one(email, aid, "PRKX1" + uuid.uuid4().hex[:8])
    t2 = "PRKX2" + uuid.uuid4().hex[:8]
    dbfixture.ingest_raw_message(other_email, make_message(
        f"<{uuid.uuid4().hex}@t>", f"Subj {t2}", "s@ex.com", other_email, t2, T0), uid=1)
    _, r = api("GET", f"/api/search?q={t2}&account_id={local_account['id']}")
    b = r["rows"][0]["id"]

    assert _park(a)[0] == 200 and _park(b)[0] == 200
    ids = _parked_ids()
    assert a in ids and b in ids
    row = next(x for x in _list("scope=parked") if x["id"] == a)
    assert {"subject", "thread_id", "account_id"} <= set(row)


def test_flagged_still_lists_a_flagged_parked_message(account):
    email, aid = account["email"], account["id"]
    mid, _ = ingest_one(email, aid, "PRKF" + uuid.uuid4().hex[:8])
    assert api("POST", f"/api/messages/{mid}/flag?flagged=1")[0] == 200
    assert _park(mid)[0] == 200
    assert mid in _ids(_list("scope=flagged"))


def test_detail_thread_and_search_carry_the_parked_flag(account):
    email, aid = account["email"], account["id"]
    token = "PRKD" + uuid.uuid4().hex[:8]
    root, reply, thread_id, _ = _conversation(email, aid, token)
    assert _park(root)[0] == 200

    code, detail = api("GET", f"/api/messages/{root}")
    assert code == 200
    inbox = [l for l in detail["locations"] if l["role"] == "inbox"]
    assert inbox and all(l["parked"] is True for l in inbox)
    assert all(isinstance(l["parked"], bool) for l in detail["locations"])

    code, thread = api("GET", f"/api/threads/{thread_id}?account_id={aid}")
    assert code == 200
    assert len(thread["messages"]) == 2
    for m in thread["messages"]:
        assert any(l["role"] == "inbox" and l["parked"] for l in m["locations"])

    _, r = api("GET", f"/api/search?q={token}&account_id={aid}")
    assert r["rows"]
    for row in r["rows"]:
        assert any(f["role"] == "inbox" and f["parked"] is True for f in row["folders"])


def test_bulk_park_parks_and_unparks_listed_conversations(account):
    email, aid = account["email"], account["id"]
    root, reply, thread_id, _ = _conversation(email, aid, "PRKBK" + uuid.uuid4().hex[:8])
    loose, _ = ingest_one(email, aid, "PRKBL" + uuid.uuid4().hex[:8], uid=9)
    untouched, _ = ingest_one(email, aid, "PRKBU" + uuid.uuid4().hex[:8], uid=10)

    code, body = api("POST", "/api/messages/bulk/park", {"items": [
        {"account_id": aid, "thread_id": thread_id},
        {"account_id": aid, "message_id": loose}], "parked": True})
    assert code == 200, body
    assert body["moved"] == 3 and body["parked"] is True
    ids = _parked_ids()
    assert loose in ids and (root in ids or reply in ids) and untouched not in ids

    # Repeating it has nothing to change and is skipped, not refused.
    code, body = api("POST", "/api/messages/bulk/park", {"items": [
        {"account_id": aid, "thread_id": thread_id}], "parked": True})
    assert code == 200 and body["moved"] == 0

    code, body = api("POST", "/api/messages/bulk/park", {"items": [
        {"account_id": aid, "thread_id": thread_id},
        {"account_id": aid, "message_id": loose}], "parked": False})
    assert code == 200 and body["moved"] == 3
    assert loose not in _parked_ids()
    assert loose in _inbox_ids(email)


def test_park_all_parks_a_whole_inbox_and_puts_it_back(account):
    email, aid = account["email"], account["id"]
    a, _ = ingest_one(email, aid, "PRKA1" + uuid.uuid4().hex[:8], uid=1)
    b, _ = ingest_one(email, aid, "PRKA2" + uuid.uuid4().hex[:8], uid=2)
    inbox_id = mailbox(email, "inbox")["id"]

    code, body = api("POST", "/api/messages/bulk/park-all",
                     {"mailbox_id": inbox_id, "parked": True})
    assert code == 200, body
    assert body["moved"] == 2 and body["done"] is True and body["op_id"]
    assert {a, b} <= set(_parked_ids())
    assert _inbox_ids(email) == []

    code, body = api("POST", "/api/messages/bulk/park-all",
                     {"scope": "parked", "parked": False})
    assert code == 200, body
    assert body["moved"] == 2 and body["op_kind"] == "unpark"
    assert {a, b} <= set(_inbox_ids(email))
    # Not exercised with scope=unified_inbox. That selector spans every account
    # in the database, and in a full run it would park, and write Recent actions
    # records into, mail other tests left behind (test_undo reads that list).


def test_park_all_refuses_selectors_that_are_not_an_inbox(account):
    email, aid = account["email"], account["id"]
    _seed_archive(email)
    ingest_one(email, aid, "PRKAR" + uuid.uuid4().hex[:8])
    archive_id = mailbox(email, "archive")["id"]

    code, _ = api("POST", "/api/messages/bulk/park-all",
                  {"mailbox_id": archive_id, "parked": True})
    assert code == 400
    code, _ = api("POST", "/api/messages/bulk/park-all",
                  {"scope": "flagged", "parked": True})
    assert code == 400
    code, _ = api("POST", "/api/messages/bulk/park-all",
                  {"scope": "unified_inbox", "parked": False})
    assert code == 400
    code, _ = api("POST", "/api/messages/bulk/park-all",
                  {"mailbox_id": mailbox(email, "inbox")["id"], "parked": False})
    assert code == 400


def test_inbox_bulk_trash_and_move_leave_parked_mail_alone(account):
    """Emptying the inbox must not take what the user set aside with it."""
    email, aid = account["email"], account["id"]
    _seed_archive(email)
    kept, _ = ingest_one(email, aid, "PRKT1" + uuid.uuid4().hex[:8], uid=1)
    loose, _ = ingest_one(email, aid, "PRKT2" + uuid.uuid4().hex[:8], uid=2)
    assert _park(kept)[0] == 200
    inbox_id = mailbox(email, "inbox")["id"]
    archive_id = mailbox(email, "archive")["id"]

    code, body = api("POST", "/api/messages/bulk/move-all", {
        "mailbox_id": inbox_id, "target_mailbox_id": archive_id})
    assert code == 200, body
    assert kept in _parked_ids()
    assert dbfixture.parked_placements(email, kept)[0]["imap_name"] == "INBOX"
    assert [p["imap_name"] for p in dbfixture.parked_placements(email, loose)
            if not p["deleted"]] != ["INBOX"]

    code, body = api("POST", "/api/messages/bulk/trash-all", {"mailbox_id": inbox_id})
    assert code == 200, body
    assert kept in _parked_ids()


def test_scope_parked_selectors_act_on_exactly_the_parked_mail(account):
    email, aid = account["email"], account["id"]
    _seed_archive(email)
    parked, _ = ingest_one(email, aid, "PRKP1" + uuid.uuid4().hex[:8], uid=1)
    stays, _ = ingest_one(email, aid, "PRKP2" + uuid.uuid4().hex[:8], uid=2)
    assert _park(parked)[0] == 200
    archive_id = mailbox(email, "archive")["id"]

    code, body = api("POST", "/api/messages/bulk/move-all", {
        "scope": "parked", "target_mailbox_id": archive_id})
    assert code == 200, body
    assert body["moved"] == 1
    assert parked not in _parked_ids()
    assert stays in _inbox_ids(email)

    trashed, _ = ingest_one(email, aid, "PRKP3" + uuid.uuid4().hex[:8], uid=3)
    dbfixture.create_folder(email, "Trash", "\\Trash")
    assert _park(trashed)[0] == 200
    code, body = api("POST", "/api/messages/bulk/trash-all", {"scope": "parked"})
    assert code == 200, body
    assert body["moved"] == 1
    assert trashed not in _parked_ids()
    assert stays in _inbox_ids(email)


def test_undoing_a_park_returns_the_conversation_to_the_inbox(account):
    email, aid = account["email"], account["id"]
    root, reply, _, _ = _conversation(email, aid, "PRKUN" + uuid.uuid4().hex[:8])
    op = _park(root)[1]["op_id"]

    items = _recent()
    assert items[0]["op_id"] == op and items[0]["kind"] == "park"

    code, body = api("POST", f"/api/actions/{op}/undo")
    assert code == 200, body
    assert body["ok"] is True and body["restored"] == 2
    assert {root, reply} & set(_inbox_ids(email))
    assert root not in _parked_ids()

    code, _ = api("POST", f"/api/actions/{op}/undo")
    assert code == 409


def test_undoing_an_unpark_parks_it_again_and_is_listed_as_unpark(account):
    email, aid = account["email"], account["id"]
    root, _ = ingest_one(email, aid, "PRKUU" + uuid.uuid4().hex[:8])
    _park(root)
    op = _park(root, 0)[1]["op_id"]
    assert _recent()[0]["kind"] == "unpark"

    code, body = api("POST", f"/api/actions/{op}/undo")
    assert code == 200, body
    assert root in _parked_ids()


def test_archiving_parked_mail_moves_it_out_and_undo_puts_it_back_in_parked(account):
    """The inbox is not where it was before the archive, Parked is."""
    email, aid = account["email"], account["id"]
    _seed_archive(email)
    token = "PRKAC" + uuid.uuid4().hex[:8]
    root, reply, thread_id, _ = _conversation(email, aid, token)
    pair = {root, reply}
    assert _park(root)[0] == 200

    code, body = api("POST", f"/api/messages/threads/{thread_id}/archive?account_id={aid}")
    assert code == 200, body
    assert not pair & set(_parked_ids())
    assert not pair & set(_inbox_ids(email))

    op = _recent()[0]["op_id"]
    code, body = api("POST", f"/api/actions/{op}/undo")
    assert code == 200, body
    assert pair & set(_parked_ids())
    assert not pair & set(_inbox_ids(email))


def test_undoing_an_archive_that_landed_puts_it_back_in_parked(account):
    """Once the server has the move, undo queues the reverse move. That has to
    land in Parked too, not in the inbox the mail was parked out of."""
    email, aid = account["email"], account["id"]
    _seed_archive(email)
    mid, rfc = ingest_one(email, aid, "PRKAL" + uuid.uuid4().hex[:8], uid=1)
    assert _park(mid)[0] == 200

    code, body = api("POST", f"/api/messages/{mid}/archive")
    assert code == 200, body
    op = body["op_id"]
    # The agent applies the move and the next pass brings the server's copy.
    dbfixture.apply_actions(email, minutes_ago=60)
    dbfixture.record_placement(email, rfc, uid=50, folder="Archive")

    code, body = api("POST", f"/api/actions/{op}/undo")
    assert code == 200, body
    assert mid in _parked_ids()
    assert mid not in _inbox_ids(email)
    inbox = [p for p in dbfixture.parked_placements(email, mid) if p["imap_name"] == "INBOX"]
    assert inbox and inbox[0]["parked"] is True
    # A real move went back to the server, and parking stayed local.
    moves = dbfixture.pending_actions(email, "move")
    assert len(moves) == 1 and moves[0]["payload"]["to_folder"] == "INBOX"


def test_purge_all_on_an_imported_inbox_leaves_parked_mail(local_account):
    """Deleting everything in an imported inbox means what the list showed. On an
    imported account the rows are the only copy, so this one matters most."""
    email, aid = local_account["email"], local_account["id"]
    tokens = ["PRKPG%s%d" % (uuid.uuid4().hex[:6], n) for n in range(2)]
    for token in tokens:
        dbfixture.import_raw_message(email, make_message(
            f"<{uuid.uuid4().hex}@t>", f"Subj {token}", "s@ex.com", email, token, T0))
    ids = []
    for token in tokens:
        _, r = api("GET", f"/api/search?q={token}&account_id={aid}")
        ids.append(r["rows"][0]["id"])
    kept, gone = ids
    assert _park(kept)[0] == 200

    code, body = api("POST", "/api/messages/bulk/purge-all",
                     {"mailbox_id": mailbox(email, "inbox")["id"], "confirm": True})
    assert code == 200, body
    assert body["deleted"] == 1
    assert dbfixture.message_survives(kept)["message"] is True
    assert dbfixture.message_survives(gone)["message"] is False
    assert kept in _parked_ids()


def test_a_move_landing_in_a_new_uid_inherits_the_parked_mark(account):
    """A queued move landing, or a UIDVALIDITY rebuild, writes a new placement."""
    email, aid = account["email"], account["id"]
    mid, rfc = ingest_one(email, aid, "PRKW" + uuid.uuid4().hex[:8], uid=1)
    assert _park(mid)[0] == 200

    dbfixture.record_placement(email, rfc, uid=77, folder="INBOX")
    inbox = [p for p in dbfixture.parked_placements(email, mid)
             if p["imap_name"] == "INBOX" and p["imap_uid"] == 77]
    assert inbox and inbox[0]["parked"] is True


def test_a_placement_in_another_mailbox_does_not_inherit_the_mark(account):
    email, aid = account["email"], account["id"]
    mid, rfc = ingest_one(email, aid, "PRKO" + uuid.uuid4().hex[:8], uid=1)
    assert _park(mid)[0] == 200

    dbfixture.record_placement(email, rfc, uid=5, folder="Labels/Work")
    other = [p for p in dbfixture.parked_placements(email, mid)
             if p["imap_name"] == "Labels/Work"]
    assert other and other[0]["parked"] is False
