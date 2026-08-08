"""Regressions for three things that were wrong and had no test to say so.

Each of these passed the whole suite while broken, which is the only reason they
survived: the flag no-op was invisible because it produced the right *state* by
the wrong route, the undo branch was unreachable through the UI, and the staging
sweep did not exist. They are grouped here rather than filed into the three
existing modules because what they have in common is being the cheap test that
was missing, not the feature they belong to.
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


def _flag_actions(email):
    return [a for a in dbfixture.pending_actions(email, "setflags")]


# --- flagging what is already flagged ----------------------------------------


def test_flagging_twice_queues_one_command(account):
    """The second press changes nothing, so it must say nothing to the server.

    Every queued setflags is an IMAP round trip the agent has to make, and on a
    label server one keypress reaches every placement the message has. Re-sending
    the flag it already carries was therefore N commands and N folder recounts to
    arrive back where it started — the same waste ingest.update_flags was rewritten
    to stop making on the way in. set_seen had the guard; set_flagged did not.
    """
    email = account["email"]
    mid, _ = ingest_one(email, account["id"], "FLAGDUP" + uuid.uuid4().hex[:6])

    before = len(_flag_actions(email))
    code, _ = api("POST", f"/api/messages/{mid}/flag?flagged=true")
    assert code == 200
    after_first = len(_flag_actions(email))
    assert after_first == before + 1, "flagging should queue exactly one command"

    # Same request again. The message is already flagged, so there is nothing to
    # tell anyone.
    code, body = api("POST", f"/api/messages/{mid}/flag?flagged=true")
    assert code == 200, body
    assert body["flagged"] is True, "the answer is still the state, not the change"
    assert len(_flag_actions(email)) == after_first, \
        "re-flagging an already-flagged message must not queue a second command"


def test_unflagging_still_queues(account):
    """The guard must not swallow a real change — the obvious way to get it wrong."""
    email = account["email"]
    mid, _ = ingest_one(email, account["id"], "FLAGOFF" + uuid.uuid4().hex[:6])

    api("POST", f"/api/messages/{mid}/flag?flagged=true")
    before = len(_flag_actions(email))
    code, body = api("POST", f"/api/messages/{mid}/flag?flagged=false")
    assert code == 200, body
    assert body["flagged"] is False
    assert len(_flag_actions(email)) == before + 1, \
        "clearing a flag that is set is a change and has to reach the server"

    _, detail = api("GET", f"/api/messages/{mid}")
    assert detail["flagged"] is False


def test_marking_read_twice_queues_one_command(account):
    """set_seen's guard, asserted rather than assumed — it is what set_flagged copies."""
    email = account["email"]
    mid, _ = ingest_one(email, account["id"], "SEENDUP" + uuid.uuid4().hex[:6])

    api("POST", f"/api/messages/{mid}/mark?seen=true")
    before = len(_flag_actions(email))
    api("POST", f"/api/messages/{mid}/mark?seen=true")
    assert len(_flag_actions(email)) == before, \
        "marking a read message read must not queue a second command"


# --- undoing an action that names no message ---------------------------------


def test_undo_retires_an_action_with_no_message(account):
    """The defensive branch in _cancel has to defend rather than raise.

    A logged move whose message_pk is NULL cannot happen through the UI today,
    which is why nothing caught that the call retiring it was missing an
    argument: the branch answered a 500 instead of taking the row out of the
    queue. Reached here directly, because "unreachable" is a property of today's
    callers and not of the code.
    """
    email = account["email"]
    _seed_folder(email, "Archive", "\\Archive")
    op_id = uuid.uuid4().hex
    action_id = dbfixture.queue_move_without_message(email, op_id)

    code, body = api("POST", f"/api/actions/{op_id}/undo")
    assert code == 200, body
    # Nothing was put back, because there was nothing to put back — but the
    # operation succeeded, which is the difference between this and a traceback.
    assert body["restored"] == 0

    status, marked = dbfixture.action_status(action_id)
    assert marked, "the row has to be stamped undone so a second press is a no-op"
    assert status == "undone", \
        "and taken out of the queue, or the agent goes on trying to move nothing"

    # The panel drops it, which is how the user sees that the undo worked.
    _, recent = api("GET", "/api/actions/recent")
    assert op_id not in [item["op_id"] for item in recent["items"]]


def test_undo_is_idempotent_for_such_a_row(account):
    """Undo is a button people press twice; the second press must not 500 either."""
    email = account["email"]
    _seed_folder(email, "Archive", "\\Archive")
    op_id = uuid.uuid4().hex
    dbfixture.queue_move_without_message(email, op_id)

    assert api("POST", f"/api/actions/{op_id}/undo")[0] == 200
    code, body = api("POST", f"/api/actions/{op_id}/undo")
    assert code == 409, body
    assert "already been undone" in str(body.get("detail", ""))
