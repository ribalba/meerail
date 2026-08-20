"""Integration tests for message actions (read/flag/trash) + agent-action queue."""

import uuid
from datetime import timedelta

import dbfixture
from conftest import T0, ingest_one
from core.mail.store import STUCK_AFTER
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


def test_archiving_a_conversation_twice_says_it_is_already_filed(account):
    """A second press moves nothing, and says so.

    It used to answer 200 with `moved: 0`, which the reader shows as a
    successful archive: the row leaves the list, the next refresh puts it back,
    and nothing anywhere explains why. The same silence is what made Delete look
    broken in Trash — see test_deleting_something_already_in_trash_says_so.
    """
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
    assert code == 409
    assert "already in" in again["detail"]


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


def test_archive_prefers_a_folder_called_archive_over_all_mail(account):
    """The fallback above is for accounts with nowhere else to file, and this
    account has somewhere: a folder called Archive whose role was never recorded.

    ``role`` is derived once and stored, so a row written before the server
    published its SPECIAL-USE flags keeps ``custom`` for good — and the archive
    then went to \\All, which on Proton is a move the server refuses. Every
    press queued another one, none of them ever ran, and the app went on showing
    the mail as filed.
    """
    email, aid = account["email"], account["id"]
    tok = "NAMEDARCH" + uuid.uuid4().hex[:6]
    ingest_one(email, aid, tok)
    _seed_folder(email, "Archive", "\\Archive")
    dbfixture.ingest_raw_message(email, make_message(
        f"<seed-{uuid.uuid4().hex}@t>", "Seed", "x@y.com", email, "seed", T0),
        uid=1, folder="All Mail", role_hint="\\All")
    # What the account actually looked like: the folder is there, the role is not.
    dbfixture.set_mailbox_role(email, "Archive", "custom")

    _, r = api("GET", f"/api/search?q={tok}&account_id={aid}")
    code, body = api("POST", f"/api/messages/threads/{r['rows'][0]['thread_id']}"
                             f"/archive?account_id={aid}")

    assert code == 200, body
    moves = [x for x in _actions(email) if x["type"] == "move"]
    assert [m["payload"]["to_folder"] for m in moves] == ["Archive"]


def test_archive_refuses_when_all_mail_is_the_only_target_and_the_server_said_no(account):
    """Once the agent has been told no, the app stops queueing the same move.

    \\All is a destination on Gmail and not one on Proton, and only the agent
    ever finds that out — the app picks the folder and has no connection to try
    it with. So the refusal is written down (Mailbox.writes_refused_at) and read
    here: the keypress fails, at the keypress, saying what the account is
    missing. Before this it succeeded, and the failure arrived fifteen minutes
    later in a log nobody was reading.
    """
    email, aid = account["email"], account["id"]
    tok = "NOARCH" + uuid.uuid4().hex[:6]
    ingest_one(email, aid, tok)
    dbfixture.ingest_raw_message(email, make_message(
        f"<seed-{uuid.uuid4().hex}@t>", "Seed", "x@y.com", email, "seed", T0),
        uid=1, folder="All Mail", role_hint="\\All")
    dbfixture.refuse_writes(email, "All Mail")

    _, r = api("GET", f"/api/search?q={tok}&account_id={aid}")
    code, body = api("POST", f"/api/messages/threads/{r['rows'][0]['thread_id']}"
                             f"/archive?account_id={aid}")

    assert code == 400
    assert "Archive folder" in body["detail"]
    assert not [x for x in _actions(email) if x["type"] == "move"]


def test_a_move_the_server_keeps_refusing_stops_holding_the_local_view(account):
    """Who is believed while a queued move is not getting through.

    The source placement goes the moment the key is pressed, so until the server
    catches up the app is showing something only it knows — and the sweep that
    would repair a placement the server still lists is told to leave it alone
    while the move is in flight. Since nothing ever retires a failing action,
    "in flight" used to have no end: a move the server refused every fifteen
    minutes kept the app's version alive for good, which is precisely how a
    message showed as archived here and sat in the inbox there.
    """
    email, aid = account["email"], account["id"]
    tok = "STUCKMOVE" + uuid.uuid4().hex[:6]
    _, rfc_id = ingest_one(email, aid, tok)
    _seed_folder(email, "Archive", "\\Archive")
    _, r = api("GET", f"/api/search?q={tok}&account_id={aid}")
    assert api("POST", f"/api/messages/threads/{r['rows'][0]['thread_id']}"
                       f"/archive?account_id={aid}")[0] == 200

    # Queued and failing: a Bridge restart looks exactly like this, so the move
    # is still believed and the inbox placement stays gone.
    assert dbfixture.fail_actions(email, attempts=1) == 1
    assert dbfixture.move_in_flight(email, rfc_id) is True

    # A quarter of an hour of the same refusal is not a server catching up.
    assert dbfixture.fail_actions(email, attempts=STUCK_AFTER) == 1
    assert dbfixture.move_in_flight(email, rfc_id) is False


def test_a_change_that_will_not_go_through_reaches_the_status_panel(account):
    """The queue's failures have to be visible somewhere in the app.

    A queued move has no folder of its own and no row in any list — the message
    is already showing the change — so a write-back that stops working was
    reported nowhere at all: the log said it, fifteen minutes later, to nobody.
    ``dropped_kind`` rides along because the two ways a change is dropped need
    opposite advice, and the panel picks its sentence from it.
    """
    email, aid = account["email"], account["id"]
    tok = "PANEL" + uuid.uuid4().hex[:6]
    ingest_one(email, aid, tok)
    _seed_folder(email, "Archive", "\\Archive")
    _, r = api("GET", f"/api/search?q={tok}&account_id={aid}")
    assert api("POST", f"/api/messages/threads/{r['rows'][0]['thread_id']}"
                       f"/archive?account_id={aid}")[0] == 200

    # Failing, and long enough that a passing outage no longer explains it.
    dbfixture.fail_actions(email, attempts=STUCK_AFTER, error="move failed: nope")
    _, status = api("GET", "/api/sync/status")
    assert status["actions"]["stuck"] == 1
    assert status["actions"]["error"] == "move failed: nope"
    assert status["actions"]["dropped"] == 0

    # Dropped: out of the queue, and named by the reason it was dropped for.
    dbfixture.drop_actions(email, "stale", "the folder was rebuilt")
    _, status = api("GET", "/api/sync/status")
    assert status["actions"]["stuck"] == 0
    assert status["actions"]["dropped"] == 1
    assert status["actions"]["dropped_kind"] == "stale"
    assert status["actions"]["dropped_error"] == "the folder was rebuilt"


def _status_actions():
    _, status = api("GET", "/api/sync/status")
    return status["actions"]


def test_a_dropped_notice_can_be_dismissed_and_a_later_failure_brings_it_back(account):
    """The notice reports something that has finished happening, so nothing it
    counts will ever stop being true on its own — it stood for a full day
    whatever the reader did about it, including fixing the cause. Dismissing is
    the only state left to change, and it must not be a way of going deaf: a
    failure after the press is stamped later than it and comes back by itself."""
    email, aid = account["email"], account["id"]
    _seed_folder(email, "Archive", "\\Archive")
    for n in range(2):
        tok = f"DISMISS{n}" + uuid.uuid4().hex[:6]
        ingest_one(email, aid, tok)
        _, r = api("GET", f"/api/search?q={tok}&account_id={aid}")
        assert api("POST", f"/api/messages/threads/{r['rows'][0]['thread_id']}"
                           f"/archive?account_id={aid}")[0] == 200
        if n == 0:
            dbfixture.drop_actions(email, "stale", "the folder was rebuilt")
            assert _status_actions()["dropped"] == 1
            assert api("POST", "/api/sync/actions/dismiss")[0] == 200
            assert _status_actions()["dropped"] == 0, "dismissing clears what it covered"

    # The second archive fails *after* the dismissal, so it is news again.
    dbfixture.drop_actions(email, "stale", "the folder was rebuilt again")
    assert _status_actions()["dropped"] == 1


def test_a_move_the_server_forbids_reaches_the_panel_as_news_not_as_a_fault(account):
    """The two refusals need opposite sentences, and the panel picks by the code.

    A destination that takes no mail at all is a fault in the account: it recurs
    until somebody makes an Archive folder, and the notice says so with a button.
    A move between the inbox and Sent is not a fault anywhere — Proton has no
    such operation, the mail never left the folder it was in, and there is nothing
    to repair. Both used to arrive as the same red "could not be made", which is
    an alarm about mail sitting exactly where its owner last saw it.
    """
    email, aid = account["email"], account["id"]
    _seed_folder(email, "Sent", "\\Sent")
    mid, _ = ingest_one(email, aid, "ROUTE" + uuid.uuid4().hex[:6])
    ids = _role_ids(email)
    assert api("POST", f"/api/messages/{mid}/move?mailbox_id={ids['sent']}"
                       f"&source_mailbox_id={ids['inbox']}")[0] == 200

    dbfixture.drop_actions(email, "refused", "The server will not move mail between "
                          "INBOX and Sent in either direction", refused_kind="route")

    ac = _status_actions()
    assert ac["dropped"] == 1 and ac["dropped_kind"] == "refused"
    assert ac["dropped_reason"] == "route"
    # Nothing to build: the button is for an account with nowhere to archive to,
    # and this refusal has nothing to do with archiving.
    assert ac["dropped_fix"] is None
    # The reason still travels, because it names the two folders and quotes the
    # server — the part the panel's own sentence cannot know.
    assert "INBOX" in ac["dropped_error"] and "Sent" in ac["dropped_error"]


def _role_ids(email):
    _, boxes = api("GET", "/api/mailboxes")
    return {m["role"]: m["id"] for a in boxes["accounts"] if a["email"] == email
            for m in a["mailboxes"]}


def _refused_into_all_mail(email, aid, tok):
    """A move the server answered "operation not allowed" to, aimed at \\All.

    Queued as a plain move rather than through Archive, because whether archiving
    even *picks* \\All is the thing under test — the refusal has to be able to
    arrive on an account that archives somewhere sensible, which is how it
    reached a real mailbox (an undo's reverse move)."""
    mid, _ = ingest_one(email, aid, tok)
    ids = _role_ids(email)
    assert api("POST", f"/api/messages/{mid}/move?mailbox_id={ids['all']}"
                       f"&source_mailbox_id={ids['inbox']}")[0] == 200
    dbfixture.drop_actions(email, "refused", "move failed: operation not allowed")
    # What the agent writes down the first time a folder says no, and what stops
    # the app offering \All as an archive target ever again.
    dbfixture.refuse_writes(email, "All Mail")


def test_the_dropped_notice_offers_the_archive_folder_only_where_one_is_missing(account):
    """The advice these refusals carry — go and make an Archive folder — is only
    true for an account that has not got one, and it was printed at accounts that
    had. So the button is offered per account, and only when the answer is no."""
    email, aid = account["email"], account["id"]
    _seed_folder(email, "All Mail", "\\All")
    _refused_into_all_mail(email, aid, "FIXBTN" + uuid.uuid4().hex[:6])

    fix = _status_actions()["dropped_fix"]
    assert fix and fix["kind"] == "create_archive"
    assert fix["account_id"] == aid and fix["name"] == "Archive"

    # Pressing it queues the folder for the agent, and nothing is written here:
    # prune_mailboxes would delete an optimistic row on the confirming pass.
    assert api("POST", f"/api/sync/actions/create-archive?account_id={aid}")[0] == 200
    creates = [a for a in dbfixture.pending_actions(email, "create_folder")]
    assert [a["payload"]["name"] for a in creates] == ["Archive"]
    # Idempotent: a second press before the agent's pass must not queue a second.
    assert api("POST", f"/api/sync/actions/create-archive?account_id={aid}")[0] == 200
    assert len(dbfixture.pending_actions(email, "create_folder")) == 1


def test_no_archive_folder_is_offered_to_an_account_that_has_one(account):
    """The case that made the old advice wrong: the refusal aimed at \\All came
    from somewhere other than archiving, and telling this user to build a folder
    they already have sends them off to fix nothing."""
    email, aid = account["email"], account["id"]
    _seed_folder(email, "All Mail", "\\All")
    _seed_folder(email, "Archive", "\\Archive")
    _refused_into_all_mail(email, aid, "HASARCH" + uuid.uuid4().hex[:6])

    assert _status_actions()["dropped"] == 1
    assert _status_actions()["dropped_fix"] is None
    code, body = api("POST", f"/api/sync/actions/create-archive?account_id={aid}")
    assert code == 409
    assert "already archives into" in body["detail"]


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


def _trash_id(email):
    _, boxes = api("GET", "/api/mailboxes")
    return next(m["id"] for a_ in boxes["accounts"] for m in a_["mailboxes"]
                if a_["email"] == email and m["role"] == "trash")


def test_emptying_the_trash_is_the_one_route_to_a_permanent_delete(account):
    """Deleting for good is still reachable — it just has to be asked for, by
    name, and confirmed. It is the only place a `delete` action is queued;
    every other route carries a target folder to move the message to."""
    email = account["email"]
    _seed_trash(email)
    tok = "EMPTYTOK" + uuid.uuid4().hex[:6]
    dbfixture.ingest_raw_message(email, make_message(
        f"<e-{uuid.uuid4().hex}@t>", f"Doomed {tok}", "x@y.com", email, f"{tok} body", T0),
        uid=2, folder="Trash", role_hint="\\Trash")
    trash_id = _trash_id(email)

    # Unconfirmed is refused outright: a client that forgets the flag gets an
    # error, never a deletion.
    code, body = api("POST", "/api/messages/bulk/empty-trash", {"mailbox_id": trash_id})
    assert code == 400, body
    assert not any(a["type"] == "delete" for a in _actions(email))

    code, body = api("POST", "/api/messages/bulk/empty-trash",
                     {"mailbox_id": trash_id, "confirm": True})
    assert code == 200, body
    assert body["deleted"] >= 1 and body["done"] is True
    assert any(a["type"] == "delete" for a in _actions(email))


def test_empty_trash_says_what_it_could_not_delete_yet(account):
    """A Trash holding only mail whose move has not landed deletes nothing.

    That much is deliberate: until the server confirms the move, the message is
    still filed where it came from as far as any mail server knows, and
    "empty the Trash" is not permission to delete it there. What was missing was
    the saying so — the route answered `deleted: 0, done: true`, which a client
    can only render as a button that did nothing at all. That is exactly what a
    Trash looks like while the agent is working through a backlog of moves.
    """
    email, aid = account["email"], account["id"]
    _seed_trash(email)                       # one the server has, uid and all
    trash_id = _trash_id(email)
    tok = "QUEUEDTOK" + uuid.uuid4().hex[:6]
    mid, _ = ingest_one(email, aid, tok)
    _, boxes = api("GET", "/api/mailboxes")
    inbox_id = next(m["id"] for a_ in boxes["accounts"] for m in a_["mailboxes"]
                    if a_["email"] == email and m["role"] == "inbox")
    # Trashed here, not on the server: the placement this leaves in Trash is one
    # the server has never seen.
    assert api("POST", f"/api/messages/{mid}/trash?source_mailbox_id={inbox_id}")[0] == 200

    code, body = api("POST", "/api/messages/bulk/empty-trash",
                     {"mailbox_id": trash_id, "confirm": True})
    assert code == 200, body
    assert body["deleted"] == 1          # the seeded one, which the server has
    assert body["queued"] == 1           # the one still on its way there

    # Emptying again is the case that read as broken: nothing left that can be
    # deleted, and the only thing worth answering is how much is waiting on the
    # sync rather than on the button.
    code, body = api("POST", "/api/messages/bulk/empty-trash",
                     {"mailbox_id": trash_id, "confirm": True})
    assert code == 200, body
    assert body["deleted"] == 0 and body["queued"] == 1

    # And it is still mail, not something quietly destroyed on the way past.
    assert api("GET", f"/api/messages/{mid}")[0] == 200
    assert not any(a["type"] == "delete" and a["message_pk"] == mid
                   for a in _actions(email))


def test_mail_deleted_for_good_stops_being_readable_at_once(account):
    """"Permanently deleted" has to mean it, in every read path and immediately.

    The placement goes the moment Empty Trash is confirmed; the content row goes
    when the agent's next completed pass collects it, which can be hours. In
    between, the message was listed nowhere and still answered to its own id, its
    own source URL and its own attachments — and came back first hit in a search
    for its subject, because a search has no folder in it to filter by.
    """
    email, aid = account["email"], account["id"]
    _seed_trash(email)
    tok = "GONETOK" + uuid.uuid4().hex[:6]
    dbfixture.ingest_raw_message(email, make_message(
        f"<g-{uuid.uuid4().hex}@t>", f"Doomed {tok}", "x@y.com", email, f"{tok} body", T0),
        uid=4, folder="Trash", role_hint="\\Trash")
    message_id = _search_id(aid, tok)
    assert api("GET", f"/api/messages/{message_id}")[0] == 200

    code, _ = api("POST", "/api/messages/bulk/empty-trash",
                  {"mailbox_id": _trash_id(email), "confirm": True})
    assert code == 200

    # Every way in, not just the list: an id, its source, the parts of it a
    # `cid:` names, a reply that would quote it back, and a task that would carry
    # it out to another service entirely.
    assert api("GET", f"/api/messages/{message_id}")[0] == 404
    assert api("GET", f"/api/messages/{message_id}/source")[0] == 404
    assert api("GET", f"/api/messages/{message_id}/cid/anything")[0] == 404
    assert api("GET", f"/api/compose/reply-context/{message_id}?mode=forward")[0] == 404
    assert api("POST", "/api/tasks", {"message_id": message_id})[0] in (404, 409)
    assert api("GET", f"/api/search?q={tok}&account_id={aid}")[1]["rows"] == []


def test_bulk_delete_never_infers_a_permanent_delete_from_where_a_message_sits(account):
    """The one that could empty someone's Trash without ever saying "delete".

    `Delete all Flagged` selects flagged messages from *every* folder, and a
    placement that was already in Trash used to be re-read as "the user means
    destroy this" — because that is what the Trash-folder version of the same
    button meant. So a flagged message the user had trashed weeks ago was
    permanently deleted by a button that named neither Trash nor permanence.

    Now Trash is where this puts mail and the one place it will not touch.
    """
    email, aid = account["email"], account["id"]
    _seed_trash(email)
    tok = "FLAGGEDTOK" + uuid.uuid4().hex[:6]
    dbfixture.ingest_raw_message(email, make_message(
        f"<f-{uuid.uuid4().hex}@t>", f"Kept {tok}", "x@y.com", email, f"{tok} body", T0),
        uid=7, folder="Trash", role_hint="\\Trash", flags={"flagged": True})
    # And one flagged message still in the inbox, so the batch has real work to
    # do: an endpoint that did nothing at all would pass this test by accident.
    mid, _ = ingest_one(email, aid, tok + "INBOX", uid=8)
    api("POST", f"/api/messages/{mid}/flag?flagged=1")

    code, body = api("POST", "/api/messages/bulk/trash-all", {"scope": "flagged"})

    assert code == 200, body
    assert body["done"] is True
    assert not any(a["type"] == "delete" for a in _actions(email))
    # The one in Trash is still there, and still in Trash.
    _, rows = api("GET", f"/api/messages?mailbox_id={_trash_id(email)}&limit=50")
    assert [r for r in rows["rows"] if f"Kept {tok}" in r["subject"]]


def test_bulk_delete_inside_trash_points_at_the_operation_that_means_it(account):
    """Escalating a folder-wide delete inside Trash is not a permanent delete by
    another name; it is refused, with the name of the thing that is."""
    email = account["email"]
    _seed_trash(email)

    code, body = api("POST", "/api/messages/bulk/trash-all", {"mailbox_id": _trash_id(email)})

    assert code == 400
    assert "already in Trash" in body["detail"]
    assert not any(a["type"] == "delete" for a in _actions(email))


def test_deleting_something_already_in_trash_says_so(account):
    """Delete is offered on every message the reader shows, including the ones
    it is showing from Trash. Moving a message to the folder it is already in
    changes nothing, and answering "ok" to it made Delete look broken: the row
    left the list and the next refresh put it straight back."""
    email = account["email"]
    _seed_trash(email)
    tok = "INTRASHTOK" + uuid.uuid4().hex[:6]
    dbfixture.ingest_raw_message(email, make_message(
        f"<t-{uuid.uuid4().hex}@t>", f"Already {tok}", "x@y.com", email, f"{tok} body", T0),
        uid=3, folder="Trash", role_hint="\\Trash")
    trash_id = _trash_id(email)
    _, rows = api("GET", f"/api/messages?mailbox_id={trash_id}&limit=50")
    mid = next(r["id"] for r in rows["rows"] if f"Already {tok}" in r["subject"])

    code, body = api("POST", f"/api/messages/{mid}/trash?source_mailbox_id={trash_id}")

    assert code == 409
    assert "already in" in body["detail"] and "Empty" in body["detail"]
    assert not any(a["type"] == "delete" for a in _actions(email))


def test_every_queued_action_carries_the_uid_epoch_it_was_written_in(account):
    """A UID means nothing without the UIDVALIDITY it was issued under: after a
    reset the same number names a different message, and a queued delete would
    find it. Every action the UI writes therefore records the epoch, and the
    agent checks it against the folder it has just opened."""
    email, aid = account["email"], account["id"]
    _seed_trash(email)
    tok = "EPOCHTOK" + uuid.uuid4().hex[:6]
    mid, _ = ingest_one(email, aid, tok)

    api("POST", f"/api/messages/{mid}/flag?flagged=1")
    _, boxes = api("GET", "/api/mailboxes")
    inbox_id = next(m["id"] for a_ in boxes["accounts"] for m in a_["mailboxes"]
                    if a_["email"] == email and m["role"] == "inbox")
    api("POST", f"/api/messages/{mid}/trash?source_mailbox_id={inbox_id}")

    queued = [a for a in _actions(email) if a["type"] in ("setflags", "move")]
    assert queued
    for action in queued:
        assert action["payload"]["uidvalidity"] == 1      # what dbfixture ingested under


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


def test_a_bulk_delete_of_a_labelled_message_queues_one_move_not_one_per_label(account):
    """The same message under three labels is one message, and one move.

    The thread and selection routes have collapsed labels since the second
    command was found addressing a UID the first move had just retired
    ("Message does not exist", retried forever, achieving nothing) — and giving
    the destructive half of a hand-rolled move a second run at a message already
    sitting in Trash, which is how mail got deleted outright. This route walked
    the placement rows straight out of the selector and had the bug back.
    """
    email, aid = account["email"], account["id"]
    tok = "BULKLABELTOK" + uuid.uuid4().hex[:6]
    _, rfc_id = ingest_one(email, aid, tok)
    _seed_trash(email)
    assert dbfixture.record_placement(email, rfc_id, uid=98, folder="All Mail",
                                      role_hint="\\All")
    api("POST", f"/api/messages/{_search_id(aid, tok)}/flag?flagged=1")

    code, body = api("POST", "/api/messages/bulk/trash-all", {"scope": "flagged"})

    assert code == 200, body
    assert body["moved"] == 2                     # both placements left, locally
    moves = [m for m in _actions(email) if m["type"] in ("move", "delete")]
    assert len(moves) == 1, moves
    # From a real folder, not \All: a move out of \All is an added label and
    # nothing else, which would leave the message in the inbox it just left.
    assert moves[0]["payload"]["from_folder"] == "INBOX"


def test_refiling_a_message_an_agent_is_already_moving_says_to_wait(account):
    """A queued move can be re-aimed right up until an agent takes it, and not
    after: the row is claimed and being applied, and rewriting the destination
    over the top of it would send the old one to the server and record the new
    one here — a disagreement neither side ever revisits.
    """
    email, aid = account["email"], account["id"]
    tok = "LEASEDMOVE" + uuid.uuid4().hex[:6]
    ingest_one(email, aid, tok)
    _seed_trash(email)
    _seed_folder(email, "Archive", "\\Archive")
    thread = api("GET", f"/api/search?q={tok}&account_id={aid}")[1]["rows"][0]["thread_id"]
    assert api("POST", f"/api/messages/threads/{thread}/trash?account_id={aid}")[0] == 200
    assert dbfixture.lease_actions(email) >= 1        # the agent has it now

    code, body = api("POST", f"/api/messages/threads/{thread}/archive?account_id={aid}")

    assert code == 409
    assert "still being moved" in body["detail"]
    # The agent's move is exactly as it left it: one action, still to Trash.
    with_target = [a for a in dbfixture.pending_actions(email, status="leased")
                   if a["type"] == "move"]
    assert len(with_target) == 1
    assert with_target[0]["payload"]["to_folder"] == "Trash"


def _search_id(account_id: int, token: str) -> int:
    _, r = api("GET", f"/api/search?q={token}&account_id={account_id}")
    return r["rows"][0]["id"]


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


# --- What a deleted placement stops counting for ------------------------------
#
# A message can sit in more than one folder at once — that is what a label-based
# server gives you, and what `locations` in the message detail is for.
#
# IMAP deletes in two steps: a client sets \\Deleted on a message and an EXPUNGE
# later actually removes it, which can be a long time later or never. The agent
# syncs that flag onto the placement (core/mail/store.py::_apply_flags), so a
# placement marked deleted is an ordinary thing to find in the database — with
# whatever `seen` and `flagged` it had when it was marked.
#
# So anything rolling flags up across a message's placements has to leave those
# out, or the copy still sitting in the inbox inherits the state of the copy
# somebody threw away in another client.


def _read_and_deleted_in_trash(email, tok, uid=40):
    """One message filed in INBOX *and* Trash: unread in the inbox, and read,
    starred and \\Deleted in the trash — an ordinary IMAP delete, not yet
    expunged."""
    mid = f"<both-{uuid.uuid4().hex}@t>"
    raw = make_message(mid, f"Doubled {tok}", "x@y.com", email, f"{tok} body", T0)
    dbfixture.ingest_raw_message(email, raw, uid=uid, folder="INBOX")
    dbfixture.ingest_raw_message(email, raw, uid=uid + 1, folder="Trash",
                                 role_hint="\\Trash",
                                 flags={"seen": True, "flagged": True, "deleted": True})
    return mid


def test_a_deleted_placement_stops_deciding_whether_a_message_is_read(account):
    """The inbox copy is unread. The trash copy was read, starred and marked for
    deletion in some other client. The message reads as unread, because the only
    placement that still counts is the unread one."""
    email, aid = account["email"], account["id"]
    _seed_trash(email)
    tok = "TWOPLACES" + uuid.uuid4().hex[:6]
    _read_and_deleted_in_trash(email, tok)
    message_id = _search_id(aid, tok)

    _, detail = api("GET", f"/api/messages/{message_id}")
    assert [l["role"] for l in detail["locations"]] == ["inbox"]
    assert detail["seen"] is False
    assert detail["flagged"] is False


def test_search_reads_the_same_flags_the_message_does(account):
    """Search rolled its own flags up, and rolled them up over every placement —
    so the row it drew for a message could contradict the message it opened."""
    email, aid = account["email"], account["id"]
    _seed_trash(email)
    tok = "SEARCHFLAG" + uuid.uuid4().hex[:6]
    _read_and_deleted_in_trash(email, tok, uid=50)

    _, results = api("GET", f"/api/search?q={tok}&account_id={aid}")
    assert len(results["rows"]) == 1
    row = results["rows"][0]
    _, detail = api("GET", f"/api/messages/{row['id']}")
    assert (row["seen"], row["flagged"]) == (detail["seen"], detail["flagged"])
    assert row["seen"] is False and row["flagged"] is False


def test_search_counts_a_thread_by_what_opening_it_would_show(account):
    """"3 messages" on a row that opens on one is the search result disagreeing
    with the thread view about a conversation half of which was deleted.

    Emptying the Trash takes the placements away at once and leaves the message
    rows behind until the agent's next completed pass collects them — so this is
    the ordinary state of things for however long that takes, not a rare one.
    """
    email, aid = account["email"], account["id"]
    _seed_trash(email)
    tok = "THREADCOUNT" + uuid.uuid4().hex[:6]
    root = f"<tc-root-{uuid.uuid4().hex}@t>"
    dbfixture.ingest_raw_message(email, make_message(
        root, f"Thread {tok}", "x@y.com", email, f"{tok} first", T0), uid=60)
    for n in (1, 2):
        dbfixture.ingest_raw_message(email, make_message(
            f"<tc-{n}-{uuid.uuid4().hex}@t>", f"Re: Thread {tok}", "x@y.com", email,
            f"{tok} reply {n}", T0 + timedelta(minutes=n), in_reply_to=root, refs=[root]),
            uid=60 + n, folder="Trash", role_hint="\\Trash")

    _, before = api("GET", f"/api/search?q={tok}&account_id={aid}")
    assert before["rows"][0]["thread_count"] == 3

    api("POST", "/api/messages/bulk/empty-trash",
        {"mailbox_id": _trash_id(email), "confirm": True})

    _, after = api("GET", f"/api/search?q={tok}&account_id={aid}")
    assert after["rows"][0]["thread_count"] == 1
