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


def _seed_trash(email):
    """Give the account a \\Trash to file into, as every real server has."""
    dbfixture.ingest_raw_message(email, make_message(
        f"<trash-seed-{uuid.uuid4().hex}@t>", "Seed", "x@y.com", email, "seed", T0),
        uid=1, folder="Trash", role_hint="\\Trash")


def test_trash_removes_from_inbox_and_enqueues(account):
    email, aid = account["email"], account["id"]
    _seed_trash(email)
    mid, _ = ingest_one(email, aid, "TRASHTOK" + uuid.uuid4().hex[:6])

    before = mailbox_total(email)
    _, boxes = api("GET", "/api/mailboxes")
    inbox_id = next(m["id"] for a in boxes["accounts"] for m in a["mailboxes"]
                    if a["email"] == email and m["role"] == "inbox")
    code, _ = api("POST", f"/api/messages/{mid}/trash?source_mailbox_id={inbox_id}")
    assert code == 200
    assert mailbox_total(email) == before - 1  # left the inbox locally

    acts = _actions(email)
    # A move into Trash, and nothing that destroys anything: the message is
    # somewhere the user can get it back from, here and on the server.
    assert any(a["type"] == "move" and a["payload"]["to_folder"] == "Trash" for a in acts)
    assert not any(a["type"] == "delete" for a in acts)


def test_trash_without_a_trash_folder_refuses_rather_than_deleting(account):
    """No \\Trash to file into is a broken account, not permission to destroy.

    This used to fall through to \\Deleted + EXPUNGE, which looks identical in
    the UI — the row leaves the list either way — and is the difference between
    a message the user can fetch back out of Trash and one nobody can. Any
    server whose SPECIAL-USE flags meerail could not read (and any account whose
    Trash had not been synced yet) got the destructive reading.
    """
    email, aid = account["email"], account["id"]
    mid, _ = ingest_one(email, aid, "NOTRASHTOK" + uuid.uuid4().hex[:6])

    before = mailbox_total(email)
    _, boxes = api("GET", "/api/mailboxes")
    inbox_id = next(m["id"] for a in boxes["accounts"] for m in a["mailboxes"]
                    if a["email"] == email and m["role"] == "inbox")
    code, body = api("POST", f"/api/messages/{mid}/trash?source_mailbox_id={inbox_id}")

    assert code == 400
    assert "Trash" in body["detail"]
    assert mailbox_total(email) == before          # still in the inbox
    assert not _actions(email)                     # and nothing queued at the server


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


def test_archive_falls_back_to_all_mail(account):
    """Gmail publishes no \\Archive — archiving there files into \\All ("All Mail").

    Every Gmail account used to fail the action outright with "This account has
    no Archive folder", because only the \\Archive role was ever looked up.
    """
    email, aid = account["email"], account["id"]
    tok = "ALLTOK" + uuid.uuid4().hex[:6]
    dbfixture.ingest_raw_message(email, make_message(
        f"<g-{uuid.uuid4().hex}@t>", f"Subject {tok}", "x@y.com", email, f"{tok} body", T0),
        uid=1)
    # \\All in place of \\Archive, exactly as imap.gmail.com lists it.
    dbfixture.ingest_raw_message(email, make_message(
        f"<seed-{uuid.uuid4().hex}@t>", "Seed", "x@y.com", email, "seed", T0),
        uid=1, folder="[Gmail]/All Mail", role_hint="\\All")

    _, r = api("GET", f"/api/search?q={tok}&account_id={aid}")
    thread_id = r["rows"][0]["thread_id"]

    code, body = api("POST", f"/api/messages/threads/{thread_id}/archive?account_id={aid}")
    assert code == 200, body
    assert body["moved"] == 1

    moves = [x for x in _actions(email) if x["type"] == "move"]
    assert [m for m in moves if m["payload"]["to_folder"] == "[Gmail]/All Mail"]


def test_bulk_trash_clears_every_selected_row(account):
    """Ctrl-A then Delete: the whole ticked set goes in one request.

    Selections are conversations, so a two-message thread has to leave whole —
    the same reason the reader trashes by thread rather than by message.
    """
    email, aid = account["email"], account["id"]
    _seed_trash(email)
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
    _seed_trash(email)
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
    _seed_trash(email)
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


def test_emptying_the_trash_is_the_one_route_to_a_permanent_delete(account):
    """Deleting for good is still reachable — it just has to be asked for.

    Selecting everything in Trash and pressing delete means empty it, and there
    is nowhere left to move those messages to. That is the only place a `delete`
    action is queued now; every other route carries a target folder.
    """
    email = account["email"]
    _seed_trash(email)
    tok = "EMPTYTOK" + uuid.uuid4().hex[:6]
    dbfixture.ingest_raw_message(email, make_message(
        f"<e-{uuid.uuid4().hex}@t>", f"Doomed {tok}", "x@y.com", email, f"{tok} body", T0),
        uid=2, folder="Trash", role_hint="\\Trash")

    _, boxes = api("GET", "/api/mailboxes")
    trash_id = next(m["id"] for a_ in boxes["accounts"] for m in a_["mailboxes"]
                    if a_["email"] == email and m["role"] == "trash")
    code, body = api("POST", "/api/messages/bulk/trash-all", {"mailbox_id": trash_id})

    assert code == 200, body
    assert any(a["type"] == "delete" for a in _actions(email))


def mailbox_total(email):
    _, body = api("GET", "/api/sync/status")
    st = next(r for r in body["accounts"] if r["email"] == email)
    return next(m["total"] for m in st["mailboxes"] if m["role"] == "inbox")


# --- Filing something while the agent is away --------------------------------
#
# Archiving removes the message from the folder it was in and queues the move;
# the server's copy of it in the target folder only exists once the agent has
# run that move and the next pass has ingested it. That is a poll interval away
# with a connection and days away without one — and for all of it the message
# used to be in no folder at all.


def test_archived_mail_appears_in_the_target_folder_immediately(account):
    """It must be in Archive the moment you press it, not when the agent gets
    round to it — meerail is expected to spend days with no connection."""
    email, aid = account["email"], account["id"]
    tok = "OFFARCH" + uuid.uuid4().hex[:6]
    mid, _ = ingest_one(email, aid, tok)
    dbfixture.ingest_raw_message(email, make_message(
        f"<seed-{uuid.uuid4().hex}@t>", "Seed", "x@y.com", email, "seed", T0),
        uid=1, folder="Archive", role_hint="\\Archive")

    _, boxes = api("GET", "/api/mailboxes")
    mine = next(a for a in boxes["accounts"] if a["email"] == email)["mailboxes"]
    inbox = next(m for m in mine if m["role"] == "inbox")
    archive = next(m for m in mine if m["role"] == "archive")

    _, r = api("GET", f"/api/search?q={tok}&account_id={aid}")
    thread_id = r["rows"][0]["thread_id"]
    code, body = api("POST", f"/api/messages/threads/{thread_id}/archive?account_id={aid}")
    assert code == 200, body

    # Gone from the inbox, and already in Archive — with no agent involved.
    _, rows = api("GET", f"/api/messages?mailbox_id={inbox['id']}&limit=50")
    assert not [x for x in rows["rows"] if tok in x["subject"]]
    _, rows = api("GET", f"/api/messages?mailbox_id={archive['id']}&limit=50")
    assert [x for x in rows["rows"] if tok in x["subject"]]

    # And the move is still queued for the agent, addressed by the source UID.
    moves = [m for m in _actions(email) if m["type"] == "move"]
    assert [m for m in moves if m["payload"]["to_folder"] == "Archive"]


def test_the_optimistic_copy_survives_a_sync_and_is_replaced_by_the_real_one(account):
    """Two ways this could undo itself, both of which used to: the vanished
    sweep deleting a placement the server has never heard of, and the server's
    own copy landing beside it as a duplicate."""
    email, aid = account["email"], account["id"]
    tok = "OFFSYNC" + uuid.uuid4().hex[:6]
    rfc_id = f"os-{uuid.uuid4().hex}@t"
    raw = make_message(f"<{rfc_id}>", f"Subject {tok}", "x@y.com", email, f"{tok} body", T0)
    dbfixture.ingest_raw_message(email, raw, uid=7)
    dbfixture.ingest_raw_message(email, make_message(
        f"<seed-{uuid.uuid4().hex}@t>", "Seed", "x@y.com", email, "seed", T0),
        uid=1, folder="Archive", role_hint="\\Archive")

    _, r = api("GET", f"/api/search?q={tok}&account_id={aid}")
    api("POST", f"/api/messages/threads/{r['rows'][0]['thread_id']}/archive?account_id={aid}")
    assert dbfixture.location_count(email, "Archive") == 2      # seed + the new one

    # A sync pass reconciles Archive while the move is still queued. The server
    # lists only its own seed, and that must not take the archived message back
    # out of the folder it was just filed into.
    assert dbfixture.set_present(email, "Archive", [1]) == 0
    assert dbfixture.location_count(email, "Archive") == 2

    # The agent applies the move; the next pass ingests the server's copy under
    # a real UID. One placement, not two.
    dbfixture.record_placement(email, rfc_id, uid=42, folder="Archive",
                               role_hint="\\Archive")
    assert dbfixture.location_count(email, "Archive") == 2      # seed + the real one
    _, boxes = api("GET", "/api/mailboxes")
    archive = next(m["id"] for a in boxes["accounts"] for m in a["mailboxes"]
                   if a["email"] == email and m["role"] == "archive")
    _, rows = api("GET", f"/api/messages?mailbox_id={archive}&limit=50")
    assert len([x for x in rows["rows"] if tok in x["subject"]]) == 1


def test_filing_twice_before_the_agent_runs_re_aims_the_one_move(account):
    """Archive then trash, both offline. The server still has the message where
    it started, so there is only ever one move to make — to wherever it ended
    up. A second one would address it by a UID no server has heard of."""
    email, aid = account["email"], account["id"]
    tok = "OFFTWICE" + uuid.uuid4().hex[:6]
    ingest_one(email, aid, tok)
    for folder, role in (("Archive", "\\Archive"), ("Trash", "\\Trash")):
        dbfixture.ingest_raw_message(email, make_message(
            f"<seed-{uuid.uuid4().hex}@t>", "Seed", "x@y.com", email, "seed", T0),
            uid=1, folder=folder, role_hint=role)

    _, r = api("GET", f"/api/search?q={tok}&account_id={aid}")
    thread_id = r["rows"][0]["thread_id"]
    api("POST", f"/api/messages/threads/{thread_id}/archive?account_id={aid}")

    _, boxes = api("GET", "/api/mailboxes")
    mine = next(a for a in boxes["accounts"] if a["email"] == email)["mailboxes"]
    archive = next(m for m in mine if m["role"] == "archive")
    _, rows = api("GET", f"/api/messages?mailbox_id={archive['id']}&limit=50")
    row = next(x for x in rows["rows"] if tok in x["subject"])

    code, body = api("POST", f"/api/messages/{row['id']}/trash"
                             f"?source_mailbox_id={archive['id']}")
    assert code == 200, body

    moves = [m for m in _actions(email) if m["type"] in ("move", "delete")]
    assert len(moves) == 1                        # re-aimed, not stacked
    assert moves[0]["payload"]["to_folder"] == "Trash"
    # The source is still the folder the server actually has it in.
    assert moves[0]["payload"]["from_folder"] == "INBOX"


def test_reading_a_message_that_is_still_being_moved_queues_no_bad_uid(account):
    """Marking read while the move is queued must not send the agent an IMAP
    command against a UID that only exists here."""
    email, aid = account["email"], account["id"]
    tok = "OFFREAD" + uuid.uuid4().hex[:6]
    ingest_one(email, aid, tok)
    dbfixture.ingest_raw_message(email, make_message(
        f"<seed-{uuid.uuid4().hex}@t>", "Seed", "x@y.com", email, "seed", T0),
        uid=1, folder="Archive", role_hint="\\Archive")

    _, r = api("GET", f"/api/search?q={tok}&account_id={aid}")
    api("POST", f"/api/messages/threads/{r['rows'][0]['thread_id']}/archive?account_id={aid}")
    _, boxes = api("GET", "/api/mailboxes")
    archive = next(m["id"] for a in boxes["accounts"] for m in a["mailboxes"]
                   if a["email"] == email and m["role"] == "archive")
    _, rows = api("GET", f"/api/messages?mailbox_id={archive}&limit=50")
    row = next(x for x in rows["rows"] if tok in x["subject"])

    api("POST", f"/api/messages/{row['id']}/mark?seen=1")

    _, detail = api("GET", f"/api/messages/{row['id']}")
    assert detail["seen"] is True                 # read locally, as pressed
    for a in _actions(email):
        assert a["payload"].get("uid", 1) > 0, f"queued a local-only UID: {a}"


def _seed_folder(email, folder, role_hint):
    """Give the account a folder, the only way one appears — a sync that saw it."""
    dbfixture.ingest_raw_message(email, make_message(
        f"<seed-{uuid.uuid4().hex}@t>", "Seed", "x@y.com", email, "seed", T0),
        uid=1, folder=folder, role_hint=role_hint)


def test_trashing_a_labelled_message_queues_one_move_not_one_per_label(account):
    """One Proton message wearing two labels is one message, and wants moving once.

    Queueing a move per placement sent the agent a second command addressing a
    UID the first move had just retired — "Message does not exist", logged once
    per trashed message and retried until it settled having achieved nothing.
    Worse, it was a second chance for the destructive half of a hand-rolled move
    to run against a message already sitting in Trash, which is how it got
    deleted outright.
    """
    email, aid = account["email"], account["id"]
    tok = "LABELTOK" + uuid.uuid4().hex[:6]
    _, rfc_id = ingest_one(email, aid, tok)
    _seed_folder(email, "Trash", "\\Trash")
    # The same message under \All as well, which is how Proton and Gmail hand it
    # over: two placements, two UIDs, one message.
    assert dbfixture.record_placement(email, rfc_id, uid=99, folder="All Mail",
                                      role_hint="\\All")

    _, r = api("GET", f"/api/search?q={tok}&account_id={aid}")
    code, body = api("POST", f"/api/messages/threads/{r['rows'][0]['thread_id']}/trash"
                             f"?account_id={aid}")
    assert code == 200, body
    assert body["moved"] == 2                     # both placements left, locally

    moves = [m for m in _actions(email) if m["type"] in ("move", "delete")]
    assert len(moves) == 1, moves
    # From a real folder, not \All: a move out of \All is an added label and
    # nothing else, which would leave the message in the inbox it just left.
    assert moves[0]["payload"]["from_folder"] == "INBOX"
    assert moves[0]["payload"]["to_folder"] == "Trash"


def _archive_then_settle(email, aid, tok, minutes_ago):
    """Archive a message and retire the queued move, `minutes_ago` in the past.

    Leaves the message where a placement we wrote ourselves lives on with no
    queued action behind it — the agent applied the move and the sync has not
    brought the server's copy back.
    """
    ingest_one(email, aid, tok)
    _seed_folder(email, "Archive", "\\Archive")
    _seed_folder(email, "Trash", "\\Trash")
    _, r = api("GET", f"/api/search?q={tok}&account_id={aid}")
    api("POST", f"/api/messages/threads/{r['rows'][0]['thread_id']}/archive?account_id={aid}")
    assert dbfixture.apply_actions(email, minutes_ago=minutes_ago)

    _, boxes = api("GET", "/api/mailboxes")
    archive = next(m["id"] for a in boxes["accounts"] for m in a["mailboxes"]
                   if a["email"] == email and m["role"] == "archive")
    _, rows = api("GET", f"/api/messages?mailbox_id={archive}&limit=50")
    return next(x for x in rows["rows"] if tok in x["subject"])["id"], archive


def test_filing_a_message_whose_move_just_landed_says_to_wait(account):
    """Seconds after the move applied there is no UID to address the message by,
    and the real placement is on its way. "Try again in a moment" is true."""
    email, aid = account["email"], account["id"]
    mid, archive = _archive_then_settle(email, aid, "SETTLE" + uuid.uuid4().hex[:6],
                                        minutes_ago=0)

    code, _ = api("POST", f"/api/messages/{mid}/trash?source_mailbox_id={archive}")
    assert code == 409


def test_filing_a_message_whose_move_landed_long_ago_still_works(account):
    """The same state an hour on is not a move in flight. Something went wrong
    upstream — at worst the message is gone from the server entirely — and
    refusing forever leaves it wedged in a folder no keypress can get it out of.
    It files locally instead, and tells the server nothing it cannot address."""
    email, aid = account["email"], account["id"]
    mid, archive = _archive_then_settle(email, aid, "WEDGED" + uuid.uuid4().hex[:6],
                                        minutes_ago=60)

    code, body = api("POST", f"/api/messages/{mid}/trash?source_mailbox_id={archive}")
    assert code == 200, body

    _, boxes = api("GET", "/api/mailboxes")
    trash = next(m["id"] for a in boxes["accounts"] for m in a["mailboxes"]
                 if a["email"] == email and m["role"] == "trash")
    _, rows = api("GET", f"/api/messages?mailbox_id={trash}&limit=50")
    assert mid in [x["id"] for x in rows["rows"]]
    # Nothing was queued: there is no UID here any server has heard of.
    assert not [a for a in _actions(email) if a["type"] in ("move", "delete")]
