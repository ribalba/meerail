"""The Outbox: mail written here that no mail server has taken yet.

Integration coverage for /api/outbox, and for the sidebar count that leads to
it. What is pinned here is the thing the folder was built for — a message that
is not going out has to be findable, and has to say why. Before it, a send
against a wrong SMTP port looked exactly like a delivered one from inside the
app: mail piling up in `outbound` with no way to know it was there.
"""

import sys
import time
from pathlib import Path

import pytest

import dbfixture
from helpers import api

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import actions as agent_actions  # noqa: E402


def queue_one(account, subject="Waiting", body="body text", to="dest@example.com") -> int:
    """Send a message from the app; return the outbound id sitting in the queue."""
    code, r = api("POST", "/api/compose/send", {
        "account_id": account["id"], "to": [to], "subject": subject, "body_text": body})
    assert code == 200 and r["state"] == "queued"
    return r["id"]


@pytest.fixture
def no_send_delay():
    """The send delay is an install-wide setting, so a test that changes it has
    to put it back — the next test's mail would otherwise sit in the outbox
    waiting out a delay nobody asked for."""
    before = api("GET", "/api/outbox/settings")[1]["send_delay_seconds"]
    yield lambda seconds: api("PUT", "/api/outbox/settings", {"send_delay_seconds": seconds})
    api("PUT", "/api/outbox/settings", {"send_delay_seconds": before})


def row_for(oid: int) -> dict | None:
    code, body = api("GET", "/api/outbox")
    assert code == 200
    return next((r for r in body["rows"] if r["id"] == oid), None)


def test_queued_mail_is_listed_with_who_it_is_for(account):
    oid = queue_one(account, subject="Not gone yet", to="arne@example.com")

    row = row_for(oid)
    assert row is not None
    assert row["subject"] == "Not gone yet"
    assert row["to"] == ["arne@example.com"]
    assert row["account_email"] == account["email"]
    # Nothing has been tried yet, so there is nothing to be alarmed about.
    assert row["error"] is None
    assert row["attempts"] == 0
    assert row["queued"] is True


def test_a_failed_send_says_why_and_when_it_will_be_tried_again(account):
    oid = queue_one(account, subject="Stuck")
    dbfixture.record_send_failure(oid, "TimeoutError('timed out')", attempts=2)

    row = row_for(oid)
    assert "timed out" in row["error"]
    assert row["attempts"] == 2
    assert row["last_attempt_at"]
    # The backoff doubles from a minute, so a second failure is due later than
    # the attempt that produced it — the UI's "next attempt in 2m".
    assert row["next_attempt_at"] > row["last_attempt_at"]

    # And the message itself, with its body, for whoever opens the row.
    code, detail = api("GET", f"/api/outbox/{oid}")
    assert code == 200
    assert detail["body_text"]
    assert "timed out" in detail["error"]
    assert detail["from_address"] == account["email"]
    assert detail["size_bytes"] > 0


def test_the_sidebar_counts_what_is_waiting_and_what_is_failing(account):
    _, before = api("GET", "/api/mailboxes")
    start, failing = before["smart"]["outbox_unsent"], before["smart"]["outbox_failing"]

    oid = queue_one(account)
    _, mid = api("GET", "/api/mailboxes")
    assert mid["smart"]["outbox_unsent"] == start + 1
    # Waiting is the normal state for a second after every send: the row must
    # not go red for it.
    assert mid["smart"]["outbox_failing"] == failing

    dbfixture.record_send_failure(oid, "ConnectionRefusedError()")
    _, after = api("GET", "/api/mailboxes")
    assert after["smart"]["outbox_failing"] == failing + 1


def test_sent_mail_leaves_the_outbox(account):
    _, before = api("GET", "/api/mailboxes")
    oid = queue_one(account)
    dbfixture.mark_outbound_sent(oid)

    assert row_for(oid) is None
    assert api("GET", f"/api/outbox/{oid}")[0] == 404
    _, side = api("GET", "/api/mailboxes")
    assert side["smart"]["outbox_unsent"] == before["smart"]["outbox_unsent"]


def test_try_now_makes_a_backed_off_message_due_again(account):
    """The button for the moment right after you fixed the port. The attempt
    count is deliberately kept — it is the record of how long this has been
    failing — and only the clock the backoff runs against is moved."""
    oid = queue_one(account)
    dbfixture.record_send_failure(oid, "TimeoutError('timed out')", attempts=6)
    before = dbfixture.send_action_state(oid)

    code, _ = api("POST", f"/api/outbox/{oid}/retry")
    assert code == 200

    after = dbfixture.send_action_state(oid)
    assert after["attempts"] == before["attempts"] == 6
    assert after["status"] == "pending"
    # The retry clock is dated well back, which is exactly what _due reads as
    # "this action may go now".
    assert after["updated_at"] < before["updated_at"]
    assert row_for(oid)["queued"] is True


# --- The delay, and the two buttons it exists for ---------------------------
#
# A sent message is unrecallable the instant the SMTP server takes it, so the
# only place to put "actually, no" is before that — a stretch of time in which
# the mail is written, visible, and still here. These pin the three things that
# has to mean: it waits, it can be released early, and it can be stopped.


def test_a_delayed_send_waits_and_says_when_it_will_go(account, no_send_delay):
    set_delay = no_send_delay
    set_delay(600)
    oid = queue_one(account, subject="Second thoughts")

    row = row_for(oid)
    assert row["send_at"], "a delayed send has to say when it is going"
    assert row["send_at"] > row["created_at"]
    # The clock the UI shows is the later of the two, and before the first
    # attempt the delay is the only one running.
    assert row["next_attempt_at"] == row["send_at"]
    assert row["error"] is None and row["attempts"] == 0 and row["held"] is False

    # The agent is told the same thing, in the payload it reads.
    assert dbfixture.send_action_state(oid)["payload"]["not_before"]


def test_no_delay_leaves_a_send_exactly_as_it_was(account, no_send_delay):
    """The default, and every install that predates the setting: nothing is
    written on the queue row and nothing waits."""
    no_send_delay(0)
    oid = queue_one(account)

    assert row_for(oid)["send_at"] is None
    assert "not_before" not in dbfixture.send_action_state(oid)["payload"]


def test_send_now_ends_the_delay(account, no_send_delay):
    no_send_delay(3600)
    oid = queue_one(account, subject="Go on then")
    assert row_for(oid)["send_at"]

    assert api("POST", f"/api/outbox/{oid}/retry")[0] == 200

    assert row_for(oid)["send_at"] is None
    # Cleared on the row the agent actually reads — not merely on the copy the
    # UI was shown.
    assert "not_before" not in dbfixture.send_action_state(oid)["payload"]


def test_cancel_stops_a_send_without_throwing_it_away(account, no_send_delay):
    """The middle ground between letting it go and deleting it: the message
    stays, with its envelope, and nothing comes for it until it is sent."""
    no_send_delay(3600)
    oid = queue_one(account, subject="Not this one", to="oops@example.com")
    envelope = dbfixture.send_action_state(oid)["payload"]

    code, body = api("POST", f"/api/outbox/{oid}/cancel")
    assert code == 200 and body["held"] is True

    row = row_for(oid)
    assert row is not None and row["held"] is True
    assert row["state"] == "held"
    assert row["subject"] == "Not this one"

    # Parked, not deleted: the agent's drain only selects "pending", and the
    # envelope it was built with is still there to send from.
    after = dbfixture.send_action_state(oid)
    assert after["status"] == "held"
    assert after["payload"]["mail_from"] == envelope["mail_from"]
    assert after["payload"]["rcpt_to"] == envelope["rcpt_to"]

    # Asking twice is not an error — two windows, or a double click.
    assert api("POST", f"/api/outbox/{oid}/cancel")[0] == 200


def test_send_now_puts_a_cancelled_message_back(account, no_send_delay):
    no_send_delay(3600)
    oid = queue_one(account)
    api("POST", f"/api/outbox/{oid}/cancel")

    assert api("POST", f"/api/outbox/{oid}/retry")[0] == 200

    row = row_for(oid)
    assert row["held"] is False and row["state"] == "queued"
    assert row["send_at"] is None
    after = dbfixture.send_action_state(oid)
    assert after["status"] == "pending"
    assert "not_before" not in after["payload"]


def test_a_cancelled_message_can_still_be_deleted(account, no_send_delay):
    no_send_delay(3600)
    oid = queue_one(account)
    api("POST", f"/api/outbox/{oid}/cancel")

    assert api("DELETE", f"/api/outbox/{oid}")[0] == 204
    assert row_for(oid) is None
    assert dbfixture.send_action_state(oid) is None


def test_a_message_being_sent_right_now_cannot_be_cancelled_retried_or_deleted(
        account, no_send_delay):
    """The race the lease exists to end.

    An agent picks a send up, spends a minute inside SMTP and writes the result
    at the end. For that whole minute the queue row used to still say "pending",
    so the Outbox happily cancelled a message that had already been delivered —
    the exact thing the undo window exists to prevent — and Send now re-queued
    one that was mid-flight, which is how a single message arrives twice.

    The claim is written down before the first SMTP command now, so all three
    verbs can see it and refuse. "Too late" is a worse answer than "cancelled",
    and the only true one.
    """
    no_send_delay(3600)
    oid = queue_one(account, subject="Going out right now")
    dbfixture.lease_send_action(oid)

    assert row_for(oid)["sending"] is True
    for method, path in (("POST", f"/api/outbox/{oid}/cancel"),
                         ("POST", f"/api/outbox/{oid}/retry"),
                         ("DELETE", f"/api/outbox/{oid}")):
        code, body = api(method, path)
        assert code == 409, (method, path, code)
        assert "being sent right now" in body["detail"]

    # And nothing was changed on the way to saying so: the agent still owns the
    # row, with the envelope it was built with.
    after = dbfixture.send_action_state(oid)
    assert after["status"] == "leased"
    assert row_for(oid)["state"] == "queued"


def test_a_message_that_has_just_gone_cannot_be_put_back(account, no_send_delay):
    """The other half of the same race, and the one a lock alone does not close.

    These verbs are reached through the Outbound row, which is read before the
    queue row is locked. A send that succeeds in between leaves the request
    holding a row that still says "queued" for a message that has gone — and the
    queue row it then looks for is finished, which reads exactly like "this was
    never queued". Send now took that reading and *re-queued* the message, which
    delivers it a second time; Delete took it and threw away the record of a mail
    that had actually been sent.

    Both are refused now, from the state of the row rather than from the state
    the request started with.
    """
    no_send_delay(0)
    oid = queue_one(account, subject="Already gone")
    # The queue row finished; the Outbound row is what this request read before
    # that happened. See dbfixture.finish_send_action.
    dbfixture.finish_send_action(oid)

    for method, path in (("POST", f"/api/outbox/{oid}/retry"),
                         ("POST", f"/api/outbox/{oid}/cancel"),
                         ("DELETE", f"/api/outbox/{oid}")):
        code, body = api(method, path)
        assert code == 409, (method, code, body)
        assert "already been sent" in body["detail"]

    # And above all: no second send was queued behind our backs, and the record
    # of the one that did go out is still here.
    assert dbfixture.send_action_state(oid)["status"] == "done"
    assert len(dbfixture.send_actions_for(oid)) == 1


def test_a_long_send_keeps_its_lease_alive(account, no_send_delay, monkeypatch):
    """A lease that expires under a working transfer is a duplicate send.

    No fixed expiry can be long enough: a large attachment over a slow uplink is
    a send that is *succeeding* for half an hour, and the socket timeout does not
    bound it — every chunk that goes out resets that clock. So the agent says it
    is still there while it works, and the expiry measures silence rather than
    duration.
    """
    no_send_delay(0)
    oid = queue_one(account, subject="Slow and large")
    monkeypatch.setattr(agent_actions, "_LEASE_RENEW", 0.2)
    seen = {}

    def slow_send(*_a, **_kw):
        # Read from a session of its own, as a second agent would: the renewal
        # runs on its own connection, so this is the only place it shows up —
        # and it has to be caught while the send is still in flight, because
        # settling the action writes the same column at the end.
        seen["taken"] = dbfixture.send_action_state(oid)["updated_at"]
        time.sleep(1.0)                                  # five renewal intervals
        seen["during"] = dbfixture.send_action_state(oid)["updated_at"]

    monkeypatch.setattr(agent_actions.smtp, "send_raw", slow_send)

    class Bridge:
        acc = type("A", (), {"email": account["email"], "smtp_host": "h", "smtp_port": 25,
                             "smtp_security": "starttls"})()

        def ops(self):
            return None

    with dbfixture.session() as db:
        applied, _failed, _sent = agent_actions.drain_actions(
            db, Bridge(), db.get(dbfixture.Account, account["id"]))

    assert applied == 1
    # Touched while the send was in progress, so a second agent reading the row
    # mid-transfer finds a lease that is minutes from expiring rather than one
    # it may take over — which is the duplicate this exists to prevent.
    assert seen["during"] > seen["taken"]


def test_mail_waiting_on_purpose_does_not_hold_up_mail_that_is_ready(
        account, no_send_delay, monkeypatch):
    """A pass takes the oldest *due* work, not the oldest work.

    A send can be told to wait — up to a day, from the Outbox's own delay — and
    those rows are then the oldest pending rows in the table for as long as they
    wait. Filtering for "due" after reading a fixed slice of the oldest rows
    meant a few hundred of them filled the slice and everything queued behind
    went unapplied until they cleared: a flag change made this afternoon waiting
    on mail scheduled for tomorrow morning.
    """
    # More waiting messages than one pass will look at, all of them older than
    # the one that is ready — the shape the old scan could not see past, since
    # it read a slice of the oldest rows and only then asked which were due.
    # The pass's depth is turned down rather than the queue filled to the old
    # 500-row window: it is the same relationship between the two numbers, and
    # this way the test does not cost 500 composed messages to state it.
    monkeypatch.setattr(agent_actions, "_PER_PASS", 2)
    no_send_delay(24 * 3600)
    held = [queue_one(account, subject=f"Later {i}") for i in range(5)]
    no_send_delay(0)
    ready = queue_one(account, subject="Now please")

    class Bridge:
        """A bridge for a send: no IMAP session is needed, and asking for one
        would mean the pass had reached something other than a send."""
        acc = type("A", (), {"email": account["email"], "smtp_host": "h", "smtp_port": 25,
                             "smtp_security": "starttls"})()

        def ops(self):
            return None

    sent = []
    original = agent_actions.smtp.send_raw
    agent_actions.smtp.send_raw = lambda _acc, _frm, rcpt, _mime: sent.append(rcpt)
    try:
        with dbfixture.session() as db:
            applied, failed, _ = agent_actions.drain_actions(
                db, Bridge(), db.get(dbfixture.Account, account["id"]))
    finally:
        agent_actions.smtp.send_raw = original

    assert (applied, failed) == (1, 0)                   # the ready one, and only it
    assert len(sent) == 1
    assert row_for(ready) is None                        # it went out and left the outbox
    # Every deliberately-delayed message is untouched, waiting for its own
    # deadline rather than for a pass to work its way down to it.
    for oid in held:
        row = row_for(oid)
        assert row is not None and row["send_at"] is not None
        assert dbfixture.send_action_state(oid)["status"] == "pending"


def test_changing_the_delay_does_not_move_a_deadline_already_running(account, no_send_delay):
    """A message whose author has already watched a countdown start keeps the
    deadline they were shown. The setting decides what the next message gets."""
    set_delay = no_send_delay
    set_delay(3600)
    oid = queue_one(account)
    before = row_for(oid)["send_at"]

    set_delay(0)

    assert row_for(oid)["send_at"] == before
    assert row_for(queue_one(account))["send_at"] is None


def test_the_delay_setting_round_trips_and_refuses_nonsense(no_send_delay):
    set_delay = no_send_delay
    assert set_delay(45) == (200, {"send_delay_seconds": 45})
    assert api("GET", "/api/outbox/settings")[1]["send_delay_seconds"] == 45
    assert set_delay(-1)[0] == 400
    assert set_delay(10 ** 9)[0] == 400
    # And the ceiling is reported, so the UI does not have to know it too.
    assert api("GET", "/api/outbox/settings")[1]["max_delay_seconds"] > 0


def test_the_agent_says_out_loud_what_is_still_waiting(account, capsys):
    """The other half of the same problem, on the other side of the database.

    A failed *attempt* reports itself. A send that is never attempted — Bridge
    down, the host asleep, the pass dying at connect() — reports nothing at all,
    and that is the case people hit: "sync failed" on a loop with unsent mail
    behind it and nothing connecting the two. So the queue is printed too, at
    startup and after a failed pass.
    """
    oid = queue_one(account, subject="Never went out", to="arne@example.com")
    dbfixture.record_send_failure(oid, "ConnectionRefusedError()", attempts=3)

    with dbfixture.session() as db:
        waiting = agent_actions.report_waiting(db, account["email"])

    assert waiting == 1
    out = capsys.readouterr().out
    assert "1 message(s) in the outbox have not been sent yet" in out
    assert "Never went out" in out          # which message
    assert "arne@example.com" in out        # who it was for
    assert "ConnectionRefusedError" in out  # and why it is still here


def test_the_agent_is_quiet_about_an_empty_outbox(account, capsys):
    with dbfixture.session() as db:
        assert agent_actions.report_waiting(db, account["email"]) == 0
    assert capsys.readouterr().out == ""


def test_the_agent_is_quiet_about_a_cancelled_message(account, capsys, no_send_delay):
    """The warning is for mail that cannot get out. A message someone stopped by
    hand is neither news nor a fault, and repeating it every pass would bury the
    one line in that log that means something."""
    no_send_delay(3600)
    oid = queue_one(account, subject="Stopped on purpose")
    api("POST", f"/api/outbox/{oid}/cancel")

    with dbfixture.session() as db:
        assert agent_actions.report_waiting(db, account["email"]) == 0
    assert capsys.readouterr().out == ""

    # But it is still in the folder — quiet is not the same as gone.
    assert row_for(oid)["held"] is True


def test_discarding_takes_a_message_out_of_the_queue_for_good(account):
    """The only way out for mail nobody wants any more. The agent never gives up
    on a queued message, so without this a typo'd address is retried forever."""
    email = account["email"]
    oid = queue_one(account, to="nobody@invalid.example")
    assert dbfixture.pending_actions(email, "send")

    assert api("DELETE", f"/api/outbox/{oid}")[0] == 204

    assert row_for(oid) is None
    assert dbfixture.send_action_state(oid) is None
    assert not [a for a in dbfixture.pending_actions(email, "send")
                if a["payload"].get("outbound_id") == oid]
    assert api("DELETE", f"/api/outbox/{oid}")[0] == 404
