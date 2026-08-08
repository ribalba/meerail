"""Integration tests for Recent actions and Undo.

The three outcomes of pressing Undo are the three tests worth having, and they
are separated by nothing but how far the queued move has got: not yet applied
(a true undo, nothing was ever said to the server), the agent holding it or the
move just landed (refused, because there is no UID to address), and applied and
synced (a move back). See app/routers/undo.py.
"""

import uuid

import dbfixture
from conftest import T0, ingest_one
from helpers import api, make_message


def _seed_folder(email, folder, role_hint):
    """Give the account a folder, the only way one appears — a sync that saw it."""
    dbfixture.ingest_raw_message(email, make_message(
        f"<seed-{uuid.uuid4().hex}@t>", "Seed", "x@y.com", email, "seed", T0),
        uid=1, folder=folder, role_hint=role_hint)


def _boxes(email):
    _, boxes = api("GET", "/api/mailboxes")
    return {m["role"]: m["id"] for a in boxes["accounts"] if a["email"] == email
            for m in a["mailboxes"]}


def _recent():
    code, body = api("GET", "/api/actions/recent")
    assert code == 200, body
    return body["items"]


def _in_mailbox(mailbox_id, message_id):
    _, rows = api("GET", f"/api/messages?mailbox_id={mailbox_id}&limit=100")
    return message_id in [x["id"] for x in rows["rows"]]


def _archive(account, token):
    """Archive one freshly ingested message. Returns (message_id, mailbox ids)."""
    email, aid = account["email"], account["id"]
    _seed_folder(email, "Archive", "\\Archive")
    _seed_folder(email, "Trash", "\\Trash")
    mid, _ = ingest_one(email, aid, token)
    ids = _boxes(email)
    code, body = api("POST", f"/api/messages/{mid}/archive?source_mailbox_id={ids['inbox']}")
    assert code == 200, body
    return mid, ids


def test_recent_lists_what_you_just_did(account):
    """The panel's whole point: the last action, named the way it was pressed."""
    mid, _ = _archive(account, "RECENT" + uuid.uuid4().hex[:6])

    items = _recent()
    assert items, "the archive should be the newest entry"
    assert items[0]["kind"] == "archive"
    assert items[0]["count"] == 1
    assert items[0]["undoable"] is True
    assert items[0]["to"] == "Archive"


def test_undo_before_the_agent_runs_puts_the_message_back(account):
    """Nothing was said to any mail server, so this is a true undo: the message
    is back in the inbox, and the queued move is gone rather than reversed."""
    email = account["email"]
    mid, ids = _archive(account, "UNDOPEND" + uuid.uuid4().hex[:6])
    assert not _in_mailbox(ids["inbox"], mid)

    op = _recent()[0]["op_id"]
    code, body = api("POST", f"/api/actions/{op}/undo")
    assert code == 200, body
    assert body["restored"] == 1

    assert _in_mailbox(ids["inbox"], mid)
    assert not _in_mailbox(ids["archive"], mid)
    # Nothing left for the agent to do — the move never happened.
    assert not [a for a in dbfixture.pending_actions(email) if a["type"] == "move"]


def test_undo_restores_the_flags_the_move_carried(account):
    """A message read in the inbox and then archived comes back read. The flags
    live on the placement, and the placement is what the move deleted."""
    email = account["email"]
    _seed_folder(email, "Archive", "\\Archive")
    mid, _ = ingest_one(email, account["id"], "UNDOFLAG" + uuid.uuid4().hex[:6])
    ids = _boxes(email)
    api("POST", f"/api/messages/{mid}/mark?seen=1")
    api("POST", f"/api/messages/{mid}/archive?source_mailbox_id={ids['inbox']}")

    api("POST", f"/api/actions/{_recent()[0]['op_id']}/undo")

    _, detail = api("GET", f"/api/messages/{mid}")
    assert detail["seen"] is True


def test_undo_while_the_agent_holds_it_says_to_wait(account):
    """Rewriting a row an agent is mid-apply on is the race that once left a
    message in Trash on the server and in Archive here. Refusing is the answer,
    and pressing again once the folder settles works."""
    email = account["email"]
    _archive(account, "UNDOLEASE" + uuid.uuid4().hex[:6])
    assert dbfixture.lease_actions(email) >= 1

    code, body = api("POST", f"/api/actions/{_recent()[0]['op_id']}/undo")
    assert code == 409
    assert "try again in a moment" in body["detail"]


def test_undo_just_after_the_move_landed_says_to_wait(account):
    """The move ran and the sync has not brought the server's copy back, so the
    message sits on a placement whose UID no server has heard of. There is
    nothing to aim a reverse move at yet."""
    email = account["email"]
    _archive(account, "UNDOFLIGHT" + uuid.uuid4().hex[:6])
    assert dbfixture.apply_actions(email, minutes_ago=0)

    code, body = api("POST", f"/api/actions/{_recent()[0]['op_id']}/undo")
    assert code == 409
    assert "try again in a moment" in body["detail"]


def test_undo_after_the_move_synced_queues_the_reverse(account):
    """Once the server's own copy is here, undo is an ordinary move back — the
    local row is not rewritten, because the server's placement is the truth."""
    email, aid = account["email"], account["id"]
    token = "UNDODONE" + uuid.uuid4().hex[:6]
    _seed_folder(email, "Archive", "\\Archive")
    mid_rfc = f"m-{uuid.uuid4().hex}@t"
    raw = make_message(f"<{mid_rfc}>", f"Subj {token}", "s@ex.com", email, f"{token} body", T0)
    dbfixture.ingest_raw_message(email, raw, uid=41)
    _, r = api("GET", f"/api/search?q={token}&account_id={aid}")
    mid = r["rows"][0]["id"]

    ids = _boxes(email)
    api("POST", f"/api/messages/{mid}/archive?source_mailbox_id={ids['inbox']}")
    assert dbfixture.apply_actions(email, minutes_ago=0)
    # The sync brings the server's copy back, which retires the optimistic
    # placement and gives the message a UID in Archive that can be addressed.
    dbfixture.ingest_raw_message(email, raw, uid=77, folder="Archive")

    op = _recent()[0]["op_id"]
    code, body = api("POST", f"/api/actions/{op}/undo")
    assert code == 200, body

    moves = [a for a in dbfixture.pending_actions(email) if a["type"] == "move"]
    assert len(moves) == 1
    assert moves[0]["payload"]["from_folder"] == "Archive"
    assert moves[0]["payload"]["to_folder"] == "INBOX"
    assert moves[0]["payload"]["uid"] == 77
    assert _in_mailbox(ids["inbox"], mid)


def test_undoing_twice_is_refused(account):
    """Undo is a button people press twice. The second press changes nothing and
    says so, rather than putting the message back somewhere a second time."""
    _archive(account, "UNDOTWICE" + uuid.uuid4().hex[:6])
    op = _recent()[0]["op_id"]

    assert api("POST", f"/api/actions/{op}/undo")[0] == 200
    code, body = api("POST", f"/api/actions/{op}/undo")
    assert code == 409
    assert "already been undone" in body["detail"]


def test_an_undone_action_leaves_the_list(account):
    """The panel is the set of filings you can still take back, not a diary. An
    entry going is the plainest confirmation that Undo worked, and it is also
    what makes the keyboard shortcut walk backwards one step per press."""
    _archive(account, "UNDOSHOWN" + uuid.uuid4().hex[:6])
    op = _recent()[0]["op_id"]

    api("POST", f"/api/actions/{op}/undo")

    assert op not in [i["op_id"] for i in _recent()]


def test_undoing_a_synced_move_leaves_the_reverse_on_the_list(account):
    """The one case that leaves a trace, and it should: reversing a move the mail
    server has already made queues a real move of its own, which is a thing that
    happened to the mailbox and is itself undoable."""
    email, aid = account["email"], account["id"]
    token = "UNDOTRACE" + uuid.uuid4().hex[:6]
    _seed_folder(email, "Archive", "\\Archive")
    raw = make_message(f"<m-{uuid.uuid4().hex}@t>", f"Subj {token}", "s@ex.com", email,
                       f"{token} body", T0)
    dbfixture.ingest_raw_message(email, raw, uid=51)
    _, r = api("GET", f"/api/search?q={token}&account_id={aid}")
    mid = r["rows"][0]["id"]

    ids = _boxes(email)
    api("POST", f"/api/messages/{mid}/archive?source_mailbox_id={ids['inbox']}")
    assert dbfixture.apply_actions(email, minutes_ago=0)
    dbfixture.ingest_raw_message(email, raw, uid=88, folder="Archive")

    op = _recent()[0]["op_id"]
    assert api("POST", f"/api/actions/{op}/undo")[0] == 200

    items = _recent()
    assert op not in [i["op_id"] for i in items], "the archive itself is taken back"
    assert items[0]["kind"] == "undo" and items[0]["undoable"] is True


def test_a_thread_archive_is_one_entry_and_undoes_whole(account):
    """One keypress over a three-message conversation is one line in the panel,
    and Undo puts the conversation back — not the message the reader had open."""
    email, aid = account["email"], account["id"]
    _seed_folder(email, "Archive", "\\Archive")
    token = "UNDOTHREAD" + uuid.uuid4().hex[:6]
    root = f"<thr-{uuid.uuid4().hex}@t>"
    dbfixture.ingest_raw_message(email, make_message(
        root, f"Subj {token}", "s@ex.com", email, f"{token} one", T0), uid=201)
    for n, uid in ((2, 202), (3, 203)):
        dbfixture.ingest_raw_message(email, make_message(
            f"<thr-{uuid.uuid4().hex}@t>", f"Re: Subj {token}", "s@ex.com", email,
            f"{token} {n}", T0, in_reply_to=root, refs=[root]), uid=uid)

    _, r = api("GET", f"/api/search?q={token}&account_id={aid}")
    thread = r["rows"][0]["thread_id"]
    ids = _boxes(email)
    assert api("POST", f"/api/messages/threads/{thread}/archive?account_id={aid}")[0] == 200

    items = _recent()
    assert items[0]["kind"] == "archive"
    assert items[0]["count"] == 3, "one keypress is one entry, however many messages"
    # Named by the conversation rather than counted, and named by its newest
    # message — the title the list and the reader were showing when the key was
    # pressed, which here is the reply rather than the original subject.
    assert items[0]["thread"] == f"Re: Subj {token}"

    assert api("POST", f"/api/actions/{items[0]['op_id']}/undo")[0] == 200
    _, rows = api("GET", f"/api/messages?mailbox_id={ids['inbox']}&limit=100")
    assert len([x for x in rows["rows"] if token in x["subject"]]) >= 1
    assert not [a for a in dbfixture.pending_actions(email) if a["type"] == "move"]


def test_a_mixed_selection_is_counted_not_named(account):
    """Two conversations trashed in one keypress have no single title, so the
    panel falls back to counting. Naming either of them would describe half of
    what Undo would put back."""
    email, aid = account["email"], account["id"]
    _seed_folder(email, "Trash", "\\Trash")
    token = "UNDOMIXED" + uuid.uuid4().hex[:6]
    for n, uid in ((1, 301), (2, 302)):
        dbfixture.ingest_raw_message(email, make_message(
            f"<mix-{uuid.uuid4().hex}@t>", f"Subj {n} {token}", "s@ex.com", email,
            f"{token} {n}", T0), uid=uid)

    _, r = api("GET", f"/api/search?q={token}&account_id={aid}")
    threads = {row["thread_id"] for row in r["rows"]}
    assert len(threads) == 2, "two unrelated mails are two conversations"
    code, body = api("POST", "/api/messages/bulk/trash", {
        "items": [{"account_id": aid, "thread_id": t} for t in threads]})
    assert code == 200, body

    entry = _recent()[0]
    assert entry["count"] == 2
    assert entry["thread"] is None


def _seed_archive(email):
    dbfixture.ingest_raw_message(email, make_message(
        f"<archive-seed-{uuid.uuid4().hex}@t>", "Seed", "x@y.com", email, "seed", T0),
        uid=1, folder="Archive", role_hint="\\Archive")


def _remind(message_id, days):
    from datetime import datetime, timedelta, timezone
    due = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    return api("POST", f"/api/messages/{message_id}/remind", {"due_at": due})


def test_setting_a_reminder_is_listed(account):
    """It is one keypress that takes a conversation out of the inbox, which is
    exactly what the panel is for. Leaving it out made "remind me" the only
    action that happened invisibly."""
    email, aid = account["email"], account["id"]
    _seed_archive(email)
    mid, _ = ingest_one(email, aid, "REMLIST" + uuid.uuid4().hex[:6])
    assert _remind(mid, 3)[0] == 200

    items = _recent()
    assert items and items[0]["kind"] == "remind"
    assert items[0]["undoable"] is True


def test_undoing_a_reminder_takes_back_the_promise_too(account):
    """The half that matters. Mail put back in the inbox while a live reminder
    still pointed at the conversation would be parked all over again when it
    came due — by something the user had just cancelled."""
    email, aid = account["email"], account["id"]
    _seed_archive(email)
    mid, _ = ingest_one(email, aid, "REMUNDO" + uuid.uuid4().hex[:6])
    ids = _boxes(email)
    assert _remind(mid, 3)[0] == 200
    assert not _in_mailbox(ids["inbox"], mid)

    assert api("POST", f"/api/actions/{_recent()[0]['op_id']}/undo")[0] == 200

    assert _in_mailbox(ids["inbox"], mid)
    # The reminders view selects by the promise, not by where the mail is, so an
    # empty result here is the promise itself being gone rather than the message
    # merely having moved.
    _, waiting = api("GET", "/api/messages?scope=reminders&limit=50")
    assert mid not in [r["id"] for r in waiting["rows"]]


def _seed_all_mail(email):
    """A \\All mailbox, which Proton and Gmail both publish and which nothing can
    be filed *into* — it is the union of everything the account holds."""
    dbfixture.ingest_raw_message(email, make_message(
        f"<all-seed-{uuid.uuid4().hex}@t>", "Seed", "x@y.com", email, "seed", T0),
        uid=1, folder="All Mail", role_hint="\\All")


def test_a_move_out_of_all_mail_cannot_be_undone_once_it_has_landed(account):
    """The failure this was written for: on a label server most mail lives in
    \\All, so undoing an archive aimed a move straight back into it — a request
    the server answers "operation not allowed", once per message, minutes after
    the undo had reported success.

    Refused up front instead, and refused only once the move has reached the
    server: nothing was taken out of \\All when the message was filed (the agent
    copies rather than moves out of it), so there is nothing to put back.
    """
    email, aid = account["email"], account["id"]
    _seed_all_mail(email)
    _seed_folder(email, "Archive", "\\Archive")
    token = "ALLMAIL" + uuid.uuid4().hex[:6]
    raw = make_message(f"<am-{uuid.uuid4().hex}@t>", f"Subj {token}", "s@ex.com", email,
                       f"{token} body", T0)
    dbfixture.ingest_raw_message(email, raw, uid=61, folder="All Mail", role_hint="\\All")
    _, r = api("GET", f"/api/search?q={token}&account_id={aid}")
    mid = r["rows"][0]["id"]
    all_mail = _boxes(email)["all"]

    assert api("POST", f"/api/messages/{mid}/archive?source_mailbox_id={all_mail}")[0] == 200
    # Still queued: nothing has been said to the server, so undo is a local
    # matter and the destination does not come into it.
    assert _recent()[0]["undoable"] is True

    assert dbfixture.apply_actions(email, minutes_ago=0)
    dbfixture.ingest_raw_message(email, raw, uid=62, folder="Archive")

    entry = _recent()[0]
    assert entry["undoable"] is False
    assert "All Mail" in entry["reason"]

    code, body = api("POST", f"/api/actions/{entry['op_id']}/undo")
    assert code == 400
    assert "nothing to put back" in body["detail"]
    # And no move was queued at anything — the whole point.
    assert not [a for a in dbfixture.pending_actions(email)
                if a["type"] == "move" and a["payload"].get("to_folder") == "All Mail"]


def _label_thread(email, aid, token):
    """A conversation as a label server holds one: every message also in \\All.

    Returns (thread_id, root message-id, reply message-id). The root is filed in
    Archive as well — the shape a thread takes once its older mail has been
    archived and only the newest reply is still in the inbox, which is most
    threads anyone presses Archive or Remind on.
    """
    _seed_all_mail(email)
    _seed_folder(email, "Archive", "\\Archive")
    # Bare ids: record_placement matches on what the store holds, which is the
    # id without its angle brackets. The brackets go on for the headers.
    root, reply = f"lt-{uuid.uuid4().hex}@t", f"lt-{uuid.uuid4().hex}@t"
    dbfixture.ingest_raw_message(email, make_message(
        f"<{root}>", f"Subj {token}", "s@ex.com", email, f"{token} one", T0),
        uid=71, folder="All Mail", role_hint="\\All")
    dbfixture.ingest_raw_message(email, make_message(
        f"<{reply}>", f"Re: Subj {token}", "s@ex.com", email, f"{token} two", T0,
        in_reply_to=f"<{root}>", refs=[f"<{root}>"]), uid=72)
    assert dbfixture.record_placement(email, reply, uid=73, folder="All Mail",
                                      role_hint="\\All")
    _, r = api("GET", f"/api/search?q={token}&account_id={aid}")
    return r["rows"][0]["thread_id"], root, reply


def _moves(email):
    return [a for a in dbfixture.pending_actions(email) if a["type"] == "move"]


def test_a_message_already_in_the_target_is_not_moved_out_of_all_mail(account):
    """The queue rows that broke Undo, killed at the point they were planned.

    On a label server every message is also in \\All, so archiving a thread whose
    older mail is *already* in Archive used to queue one move per message anyway:
    _move_messages drops the target placement from the origins, leaving \\All as
    the only one, and _carrier then picks it. The server takes those as no-ops —
    and the undo of one is a move back into \\All, which Proton will not do. Four
    rows in five on a real account were this.
    """
    email, aid = account["email"], account["id"]
    token = "ALREADY" + uuid.uuid4().hex[:6]
    thread, root, _ = _label_thread(email, aid, token)
    assert dbfixture.record_placement(email, root, uid=74, folder="Archive",
                                      role_hint="\\Archive")

    assert api("POST", f"/api/messages/threads/{thread}/archive?account_id={aid}")[0] == 200

    # The reply moves out of the inbox; the root is in Archive already and its
    # only other placement is \\All, which is not somewhere it can be moved from.
    assert [m["payload"]["from_folder"] for m in _moves(email)] == ["INBOX"]
    assert _recent()[0]["count"] == 1


def test_undo_skips_the_message_it_cannot_put_back_and_restores_the_rest(account):
    """One unreversible row must not cost the operation its other messages.

    A conversation where the older message lives only in \\All queues a move out
    of it — legitimately, since that is all the message has — and the reverse of
    that move has nowhere to aim. Refusing the whole undo over it is what left a
    reminder impossible to take back: the mail stayed parked and the promise
    stayed live, on account of a row whose reversal would have achieved nothing.
    """
    email, aid = account["email"], account["id"]
    token = "PARTIAL" + uuid.uuid4().hex[:6]
    thread, root, reply = _label_thread(email, aid, token)
    ids = _boxes(email)

    assert api("POST", f"/api/messages/threads/{thread}/archive?account_id={aid}")[0] == 200
    sources = sorted(m["payload"]["from_folder"] for m in _moves(email))
    assert sources == ["All Mail", "INBOX"], "the root has only \\All to move out of"

    assert dbfixture.apply_actions(email, minutes_ago=0)
    for mid_, uid in ((root, 81), (reply, 82)):
        dbfixture.record_placement(email, mid_, uid=uid, folder="Archive",
                                   role_hint="\\Archive")

    entry = _recent()[0]
    assert entry["undoable"] is True, "one skippable row does not make the entry dead"
    code, body = api("POST", f"/api/actions/{entry['op_id']}/undo")
    assert code == 200, body
    assert body["restored"] == 1

    # The reply came back, and nothing was aimed at \\All on the way.
    assert _in_mailbox(ids["inbox"], next(
        r["id"] for r in api("GET", f"/api/search?q={token}&account_id={aid}")[1]["rows"]
        if r["subject"].startswith("Re:")))
    assert not [m for m in _moves(email) if m["payload"].get("to_folder") == "All Mail"]


def test_a_refused_action_does_not_block_the_undo_with_a_story_about_uids(account):
    """"Refused" and "stale" are opposite failures and had one sentence between
    them. A destination the server would not accept says nothing about UIDs or
    rebuilt folders, and answering it that way sent the user after a fix for a
    problem their account did not have — on the one action they most wanted back.

    The move never ran, so taking it back is the same local matter as a row still
    sitting in the queue."""
    email, aid = account["email"], account["id"]
    _seed_archive(email)
    mid, _ = ingest_one(email, aid, "REFUSED" + uuid.uuid4().hex[:6])
    ids = _boxes(email)
    assert api("POST", f"/api/messages/{mid}/archive?source_mailbox_id={ids['inbox']}")[0] == 200
    assert dbfixture.drop_actions(email, "refused", "move failed: operation not allowed")

    entry = _recent()[0]
    assert entry["undoable"] is True
    code, body = api("POST", f"/api/actions/{entry['op_id']}/undo")
    assert code == 200, body
    assert _in_mailbox(ids["inbox"], mid)


def test_emptying_the_trash_is_listed_and_cannot_be_undone(account):
    """The one operation that destroys mail is in the panel saying exactly that.
    Leaving it out would make the destructive action the only invisible one."""
    email, aid = account["email"], account["id"]
    _seed_folder(email, "Trash", "\\Trash")
    token = "UNDOEMPTY" + uuid.uuid4().hex[:6]
    dbfixture.ingest_raw_message(email, make_message(
        f"<empty-{uuid.uuid4().hex}@t>", f"Subj {token}", "s@ex.com", email,
        f"{token} body", T0), uid=91, folder="Trash", role_hint="\\Trash")

    ids = _boxes(email)
    code, body = api("POST", "/api/messages/bulk/empty-trash",
                     {"mailbox_id": ids["trash"], "confirm": True})
    assert code == 200, body

    entry = next(i for i in _recent() if i["kind"] == "delete")
    assert entry["undoable"] is False
    assert "nothing left to put back" in entry["reason"]

    code, body = api("POST", f"/api/actions/{entry['op_id']}/undo")
    assert code == 400
    assert "nothing left here to put back" in body["detail"]


def test_filing_hands_back_the_operation_id(account):
    """The panel puts the row up from the action's own response rather than
    waiting to be told the list changed — that wait was the whole reason an
    archive took so long to appear. Every route that files mail returns the id,
    and the id it returns is the one the panel then finds."""
    email, aid = account["email"], account["id"]
    _seed_folder(email, "Archive", "\\Archive")
    _seed_folder(email, "Trash", "\\Trash")
    mid, _ = ingest_one(email, aid, "OPID" + uuid.uuid4().hex[:6])
    ids = _boxes(email)

    code, body = api("POST", f"/api/messages/{mid}/archive?source_mailbox_id={ids['inbox']}")
    assert code == 200 and body.get("op_id")
    assert body["op_id"] == _recent()[0]["op_id"]


def test_a_thread_action_hands_back_one_id_for_the_conversation(account):
    email, aid = account["email"], account["id"]
    _seed_folder(email, "Trash", "\\Trash")
    token = "OPIDTHREAD" + uuid.uuid4().hex[:6]
    ingest_one(email, aid, token)
    _, r = api("GET", f"/api/search?q={token}&account_id={aid}")
    thread = r["rows"][0]["thread_id"]

    code, body = api("POST", f"/api/messages/threads/{thread}/trash?account_id={aid}")
    assert code == 200 and body.get("op_id")
    assert body["op_id"] == _recent()[0]["op_id"]


def test_setting_a_reminder_hands_back_its_id_but_a_reschedule_does_not(account):
    """Moving a deadline files no mail, so there is nothing new to undo and no
    row for the panel to put up."""
    email, aid = account["email"], account["id"]
    _seed_archive(email)
    mid, _ = ingest_one(email, aid, "OPIDREM" + uuid.uuid4().hex[:6])

    _, first = _remind(mid, 3)
    assert first.get("op_id")
    _, again = _remind(mid, 9)
    assert again.get("op_id") is None


def test_a_flag_change_is_not_listed(account):
    """Marking mail read is not what the panel is for: it is undone by marking it
    unread, and listing it would bury the actions that file mail out of sight."""
    email, aid = account["email"], account["id"]
    mid, _ = ingest_one(email, aid, "UNDOFLAGS" + uuid.uuid4().hex[:6])
    api("POST", f"/api/messages/{mid}/mark?seen=1")

    assert not _recent()
