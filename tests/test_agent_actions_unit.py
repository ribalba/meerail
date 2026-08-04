"""Unit coverage for the action queue: what it says when something fails, and
what it does with mail it cannot send yet.

Pure unit test: the session, the bridge and SMTP are all stubbed, so this runs
without Postgres, without Bridge and without sending anything.

Two behaviours are pinned here, both from issue #7. A send that fails has to be
visible in the log — before this the failure went to a database column nothing
reads, and the agent printed "applied 1 queued action(s)" for a mail that never
left the machine. And a send that fails has to stay queued, forever: the queue
used to retire a message as failed after five attempts, which on a 30-second
poll is under three minutes of a wrong port or a signed-out Bridge.
"""

import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import actions as agent_actions  # noqa: E402
import log as agent_log  # noqa: E402
from core.models import utcnow  # noqa: E402


class Account:
    """Just enough AccountConfig for the endpoint in a failure line."""

    email = "me@example.com"
    smtp_host = "127.0.0.1"
    smtp_port = 1025
    smtp_security = "starttls"


class Action:
    """A PendingAction row, without the mapper."""

    def __init__(self, type_="send", payload=None, attempts=0, status="pending"):
        self.type = type_
        self.payload = payload or {"outbound_id": 1, "mail_from": "me@example.com",
                                   "rcpt_to": ["arne@example.com"]}
        self.account_id = 1
        self.status = status
        self.attempts = attempts
        self.error = None
        self.created_at = utcnow()
        self.updated_at = None


class Outbound:
    state = "queued"
    error = None
    sent_at = None
    raw_mime = "From: me@example.com\r\n\r\nhi"


class DB:
    """Answers the one query drain_actions makes, and hands back one Outbound.

    The due-filter is applied in Python by drain_actions, so returning every row
    here is what a real query would do too.
    """

    def __init__(self, actions, outbound=None):
        self._actions = actions
        self.outbound = outbound or Outbound()
        self.commits = 0

    def execute(self, _stmt):
        return self

    def scalars(self):
        return self

    def all(self):
        return self._actions

    def get(self, _model, _pk):
        return self.outbound

    def commit(self):
        self.commits += 1


class Bridge:
    """A bridge whose SMTP always times out — Bridge answering an implicit-TLS
    port with `starttls` configured, which is exactly the reported failure."""

    acc = Account()

    def ops(self):
        return self


class AccountRow:
    id = 1


@pytest.fixture
def failing_send(monkeypatch):
    def boom(*_a, **_kw):
        raise TimeoutError("timed out")
    monkeypatch.setattr(agent_actions.smtp, "send_raw", boom)


def test_failed_send_is_logged_with_the_endpoint_it_tried(failing_send, capsys):
    action = Action()
    db = DB([action])

    applied, failed, sent = agent_actions.drain_actions(db, Bridge(), AccountRow())

    assert (applied, failed, sent) == (0, 1, 0)
    out = capsys.readouterr().out
    # Who it was for, where it went, and when it will be tried again.
    assert "arne@example.com" in out
    assert "127.0.0.1:1025 (starttls)" in out
    assert "attempt 1, retrying in 1m" in out
    # The hint names the thing that is actually wrong.
    assert "security mode" in out
    # Still queued, so a later pass retries it.
    assert action.status == "pending"
    assert db.outbound.state == "queued"


def test_a_send_is_never_given_up_on(failing_send, capsys):
    """The heart of it: no number of failures turns the user's mail into
    something the agent may throw away."""
    action = Action(attempts=500)
    db = DB([action])

    agent_actions.drain_actions(db, Bridge(), AccountRow())

    assert action.status == "pending"
    assert action.attempts == 501
    assert db.outbound.state == "queued"
    assert db.outbound.raw_mime                      # the bytes are still there
    out = capsys.readouterr().out
    assert "giving up" not in out


def test_a_failed_action_waits_before_the_next_attempt(failing_send, capsys):
    """Each failed send costs the pass its full SMTP timeout, at the head of the
    pass. Retrying every time is what made a broken config slow every sync."""
    action = Action()
    db = DB([action])

    agent_actions.drain_actions(db, Bridge(), AccountRow())
    assert action.attempts == 1
    capsys.readouterr()

    # Same pass, seconds later: not due, so not attempted.
    agent_actions.drain_actions(db, Bridge(), AccountRow())
    assert action.attempts == 1
    assert capsys.readouterr().out == ""

    # Once the backoff has elapsed it is picked up again — forever, on a clock
    # that tops out at _RETRY_CEILING.
    action.updated_at = utcnow() - agent_actions.retry_delay(1) - timedelta(seconds=1)
    agent_actions.drain_actions(db, Bridge(), AccountRow())
    assert action.attempts == 2


def test_the_backoff_climbs_and_then_holds():
    delays = [agent_actions.retry_delay(n).total_seconds() for n in range(0, 12)]
    assert delays[0] == delays[1] == agent_actions._RETRY_BASE   # first failure
    assert delays[2] == 2 * agent_actions._RETRY_BASE
    assert max(delays) == agent_actions._RETRY_CEILING
    assert delays == sorted(delays)                              # never goes backwards


def test_an_offline_week_does_not_hold_anything_back(failing_send):
    """A laptop that has been shut for days comes back to a queue where
    everything is long overdue, and works through all of it at once."""
    actions = [Action() for _ in range(3)]
    for a in actions:
        a.attempts = 4
        a.updated_at = utcnow() - timedelta(days=4)
    db = DB(actions)

    _applied, failed, _sent = agent_actions.drain_actions(db, Bridge(), AccountRow())

    assert failed == 3
    assert all(a.attempts == 5 for a in actions)


def test_a_healthy_drain_says_nothing_and_counts_the_action(monkeypatch, capsys):
    monkeypatch.setattr(agent_actions.smtp, "send_raw", lambda *_a, **_kw: None)
    action = Action()
    db = DB([action])

    applied, failed, sent = agent_actions.drain_actions(db, Bridge(), AccountRow())

    assert (applied, failed, sent) == (1, 0, 1)
    assert capsys.readouterr().out == ""
    assert action.status == "done"
    assert db.outbound.state == "sent"


def test_mail_an_older_agent_abandoned_is_found_and_can_be_requeued(capsys):
    """Rows retired by the version that had an attempt cap are still in the
    database. They are reported on every run, and re-queued only when asked."""
    retired = Action(attempts=5, status="error")
    retired.error = "TimeoutError('timed out')"
    db = DB([retired], outbound=Outbound())
    db.outbound.state = "error"

    assert agent_actions.report_abandoned(db) == 1
    out = capsys.readouterr().out
    assert "never sent" in out
    assert "arne@example.com" in out
    assert "--requeue-abandoned" in out
    # Reporting alone changes nothing.
    assert retired.status == "error"

    assert agent_actions.requeue_abandoned(db) == 1
    assert retired.status == "pending"
    assert retired.attempts == 0
    assert db.outbound.state == "queued"


class Client:
    """An IMAP session that records the commands a move puts to it."""

    def __init__(self):
        self.calls = []

    def select_folder(self, name):
        self.calls.append(("select", name))

    def copy(self, uids, to_folder):
        self.calls.append(("copy", uids, to_folder))

    def delete_messages(self, uids):
        self.calls.append(("delete", uids))

    def expunge(self):
        self.calls.append(("expunge",))


class RoleDB:
    """Answers the one lookup a move makes: the role of its source folder."""

    def __init__(self, role):
        self.role = role

    def scalar(self, _stmt):
        return self.role


@pytest.mark.parametrize("role, filed_by_the_copy", [("all", True), ("custom", False)])
def test_a_move_out_of_all_mail_stops_at_the_copy(role, filed_by_the_copy):
    """\\All holds everything the account has, so nothing can be taken out of
    it: Proton answers the EXPUNGE with "operation not allowed" and the action
    fails forever over a step that had nothing to do. Archiving from there is
    the COPY alone. Every other folder still gets the full move."""
    client = Client()
    bridge = type("B", (), {"acc": Account(), "ops": lambda _self: client})()
    action = Action("move", {"uid": 7, "from_folder": "All Mail",
                             "to_folder": "Archive"})

    agent_actions.apply_action(RoleDB(role), bridge, AccountRow(), action)

    assert ("copy", [7], "Archive") in client.calls
    assert (("expunge",) in client.calls) is not filed_by_the_copy
    assert (("delete", [7]) in client.calls) is not filed_by_the_copy


@pytest.mark.parametrize("exc, expected", [
    (Exception("[SSL: WRONG_VERSION_NUMBER] wrong version number"), "security mode"),
    (TimeoutError("timed out"), "implicit-TLS port"),
    (Exception("SMTPAuthenticationError (535, b'Incorrect login credentials')"),
     "Bridge password"),
])
def test_hints_name_the_config_mistake_behind_a_failed_send(exc, expected):
    assert expected in agent_log.hint(exc)
