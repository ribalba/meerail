"""The Outbox: mail written here that no mail server has taken yet.

Integration coverage for /api/outbox, and for the sidebar count that leads to
it. What is pinned here is the thing the folder was built for — a message that
is not going out has to be findable, and has to say why. Before it, a send
against a wrong SMTP port looked exactly like a delivered one from inside the
app: mail piling up in `outbound` with no way to know it was there.
"""

import sys
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
