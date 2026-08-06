"""Integration tests for "remind me".

The promise has three halves and each is separately breakable: the conversation
has to *leave* the inbox when the reminder is set, it has to be told to the mail
server rather than only to this database, and it has to come *back* — unread, in
the folder it came from, on its own.

The last of those is the reason this file waits on a clock at all: the reminder
worker is a loop in the server, so the only honest way to test that a parked
conversation reappears by itself is to backdate the deadline and watch. The test
stack ticks every two seconds (docker-compose.test.yml).
"""

import time
import uuid
from datetime import datetime, timedelta, timezone

import dbfixture
from conftest import T0, ingest_one
from helpers import api, make_message


def _seed_archive(email):
    """Give the account an \\Archive to park into, as every real server has."""
    dbfixture.ingest_raw_message(email, make_message(
        f"<archive-seed-{uuid.uuid4().hex}@t>", "Seed", "x@y.com", email, "seed", T0),
        uid=1, folder="Archive", role_hint="\\Archive")


def _boxes(email):
    _, sidebar = api("GET", "/api/mailboxes")
    acc = next(a for a in sidebar["accounts"] if a["email"] == email)
    return {m["role"]: m for m in acc["mailboxes"]}, sidebar["smart"]


def _rows(params=""):
    code, body = api("GET", "/api/messages?" + params)
    assert code == 200
    return body["rows"]


def _soon(**kw):
    """An absolute instant, offset-aware, exactly as the browser sends one."""
    return (datetime.now(timezone.utc) + timedelta(**kw)).isoformat()


def _remind(message_id, **kw):
    return api("POST", f"/api/messages/{message_id}/remind", {"due_at": _soon(**kw)})


def test_setting_a_reminder_parks_the_mail_and_tells_the_server(account):
    email, aid = account["email"], account["id"]
    _seed_archive(email)
    mid, _ = ingest_one(email, aid, "REMTOK" + uuid.uuid4().hex[:6])

    code, body = _remind(mid, days=3)
    assert code == 200, body
    assert body["state"] == "pending"

    boxes, smart = _boxes(email)
    assert boxes["inbox"]["total"] == 0        # gone from the inbox…
    assert boxes["archive"]["total"] == 2      # …and filed beside the seed
    assert smart["reminders_pending"] == 1

    # Not only local: the agent has a move to apply, or the mail would come back
    # to an inbox the mail server never took it out of.
    moves = [a for a in dbfixture.pending_actions(email) if a["type"] == "move"]
    assert len(moves) == 1
    assert moves[0]["payload"]["to_folder"] == "Archive"


def test_the_reminders_view_lists_what_is_waiting(account):
    email, aid = account["email"], account["id"]
    _seed_archive(email)
    mid, _ = ingest_one(email, aid, "REMLIST" + uuid.uuid4().hex[:6])
    _remind(mid, days=5)

    rows = _rows("scope=reminders")
    assert [r["id"] for r in rows] == [mid]
    # Every list says when a parked conversation is due back, not just this one:
    # in Archive the difference between filed and coming-back is the whole point.
    assert rows[0]["remind_at"]
    boxes, _ = _boxes(email)
    archived = _rows("mailbox_id=%d" % boxes["archive"]["id"])
    assert next(r["remind_at"] for r in archived if r["id"] == mid)

    # And the mail that is merely archived — the seed — carries nothing.
    assert all(r["remind_at"] is None for r in archived if r["id"] != mid)


def test_the_thread_says_when_it_is_coming_back(account):
    email, aid = account["email"], account["id"]
    _seed_archive(email)
    mid, _ = ingest_one(email, aid, "REMTHR" + uuid.uuid4().hex[:6])
    _remind(mid, days=2)

    _, msg = api("GET", f"/api/messages/{mid}")
    code, thread = api("GET", f"/api/threads/{msg['thread_id']}?account_id={aid}")
    assert code == 200
    assert thread["reminder"] and thread["reminder"]["state"] == "pending"


def test_setting_a_second_reminder_only_moves_the_deadline(account):
    email, aid = account["email"], account["id"]
    _seed_archive(email)
    mid, _ = ingest_one(email, aid, "REMTWICE" + uuid.uuid4().hex[:6])

    _, first = _remind(mid, days=2)
    _, second = _remind(mid, days=9)
    assert second["id"] == first["id"]
    assert second["due_at"] != first["due_at"]

    # The mail is in Archive because of the first reminder; re-parking it would
    # record Archive as where it came from and lose the way back to the inbox.
    [row] = dbfixture.reminder_rows(email)
    inbox_id = _boxes(email)[0]["inbox"]["id"]
    assert row["parked"] == [{"message": mid, "from": [inbox_id]}]


def _land_in_archive(email, rfc_id, uid=50, seen=False):
    """Finish the parking the way a sync pass does.

    Two separate events, and the return trip needs both: the agent applies the
    move, and the pass after it ingests the server's own copy in Archive. Until
    the second one the placement is optimistic — no UID any server has heard of —
    and the mail can be brought back but nothing can be *told* about it.

    ``seen`` is what the server reports for the landed copy, which has to agree
    with what was read here: a placement that arrives behind local state gets a
    flag catch-up queued for it (core/mail/store.py), and a test counting the
    queue would then be counting that too.
    """
    dbfixture.apply_actions(email)
    assert dbfixture.record_placement(email, rfc_id, uid=uid, folder="Archive",
                                      role_hint="\\Archive", flags={"seen": seen})


def test_it_comes_back_on_its_own_unread(account):
    email, aid = account["email"], account["id"]
    _seed_archive(email)
    mid, rfc_id = ingest_one(email, aid, "REMBACK" + uuid.uuid4().hex[:6])
    api("POST", f"/api/messages/{mid}/mark?seen=1")
    _remind(mid, days=7)
    _land_in_archive(email, rfc_id, seen=True)

    assert dbfixture.make_reminder_due(email) == 1

    deadline = time.time() + 30
    while time.time() < deadline:
        boxes, _ = _boxes(email)
        if boxes["inbox"]["total"] == 1:
            break
        time.sleep(0.5)

    boxes, smart = _boxes(email)
    assert boxes["inbox"]["total"] == 1, "the worker never brought it back"
    assert boxes["archive"]["total"] == 1          # only the seed is left
    assert smart["reminders_pending"] == 0

    # Unread, or it comes back invisible — sorted in among last week's mail with
    # nothing to say it has just arrived.
    _, detail = api("GET", f"/api/messages/{mid}")
    assert detail["seen"] is False

    [row] = dbfixture.reminder_rows(email)
    assert row["state"] == "done" and row["fired_at"] is not None

    # Both halves went to the server: clear \\Seen while it is still in Archive
    # (the only UID that names it), then move it. In that order — the queue is
    # drained in the order it was written.
    queued = [a for a in dbfixture.pending_actions(email)]
    seen = next(a for a in queued if a["type"] == "setflags")
    move = next(a for a in queued if a["type"] == "move"
                and a["payload"]["to_folder"] == "INBOX")
    assert "\\Seen" in seen["payload"]["remove"]
    assert seen["payload"]["folder"] == "Archive"
    assert queued.index(seen) < queued.index(move)


def test_bring_it_back_now(account):
    email, aid = account["email"], account["id"]
    _seed_archive(email)
    mid, rfc_id = ingest_one(email, aid, "REMWAKE" + uuid.uuid4().hex[:6])
    _remind(mid, days=30)
    _land_in_archive(email, rfc_id)

    code, body = api("DELETE", f"/api/messages/{mid}/remind?restore=1")
    assert code == 200 and body["state"] == "done"
    boxes, smart = _boxes(email)
    assert boxes["inbox"]["total"] == 1
    assert smart["reminders_pending"] == 0


def test_cancelling_leaves_the_mail_filed(account):
    email, aid = account["email"], account["id"]
    _seed_archive(email)
    mid, rfc_id = ingest_one(email, aid, "REMDROP" + uuid.uuid4().hex[:6])
    _remind(mid, days=30)
    _land_in_archive(email, rfc_id)

    code, body = api("DELETE", f"/api/messages/{mid}/remind?restore=0")
    assert code == 200 and body["state"] == "cancelled"
    boxes, smart = _boxes(email)
    # The other half of the pair: the reminder is gone and the mail has not
    # moved. Cancelling a reminder is not the same as asking for the mail back.
    assert boxes["inbox"]["total"] == 0
    assert boxes["archive"]["total"] == 2
    assert smart["reminders_pending"] == 0
    assert _rows("scope=reminders") == []


def test_a_reminder_set_before_the_agent_ran_cancels_the_move_it_queued(account):
    """The offline case: parked, and brought back while the move is still queued.

    Nothing has reached the mail server, so as far as it is concerned the message
    never left the inbox — and the right answer is to drop the queued move rather
    than to queue a second one moving INBOX to INBOX, which is a command that
    either does nothing or fails forever.
    """
    email, aid = account["email"], account["id"]
    _seed_archive(email)
    mid, _ = ingest_one(email, aid, "REMOFFL" + uuid.uuid4().hex[:6])
    _remind(mid, days=1)
    # Deliberately no apply_actions(): the agent has not been round.

    code, _ = api("DELETE", f"/api/messages/{mid}/remind?restore=1")
    assert code == 200

    boxes, _ = _boxes(email)
    assert boxes["inbox"]["total"] == 1
    assert boxes["archive"]["total"] == 1
    # The two moves cancelled out, so there is nothing left for the agent to do
    # about this message's placement at all.
    assert [a for a in dbfixture.pending_actions(email) if a["type"] == "move"] == []


def test_a_reminder_in_the_past_is_refused(account):
    email, aid = account["email"], account["id"]
    _seed_archive(email)
    mid, _ = ingest_one(email, aid, "REMPAST" + uuid.uuid4().hex[:6])

    code, body = _remind(mid, days=-1)
    assert code == 400 and "passed" in body["detail"]
    # …and nothing moved on the strength of a request that was refused.
    boxes, smart = _boxes(email)
    assert boxes["inbox"]["total"] == 1
    assert smart["reminders_pending"] == 0

    code, body = _remind(mid, days=900)
    assert code == 400 and "two years" in body["detail"]


def test_an_account_with_no_archive_says_so(account):
    email, aid = account["email"], account["id"]
    mid, _ = ingest_one(email, aid, "REMNOARCH" + uuid.uuid4().hex[:6])

    code, body = _remind(mid, days=1)
    # Refused where it was asked for, rather than parking the mail somewhere it
    # would not come back from.
    assert code == 400 and "Archive" in body["detail"]
    assert _boxes(email)[0]["inbox"]["total"] == 1


def test_clearing_a_reminder_nobody_set(account):
    email, aid = account["email"], account["id"]
    mid, _ = ingest_one(email, aid, "REMNONE" + uuid.uuid4().hex[:6])
    code, _ = api("DELETE", f"/api/messages/{mid}/remind")
    assert code == 404
