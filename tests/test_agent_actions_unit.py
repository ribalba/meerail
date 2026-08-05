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
from itertools import count
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

    _next_id = count(1)

    def __init__(self, type_="send", payload=None, attempts=0, status="pending"):
        self.id = next(Action._next_id)
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
    """Answers the two queries drain_actions makes, and hands back one Outbound.

    The first is the queue itself; the second re-reads the due rows under FOR
    UPDATE SKIP LOCKED to claim them for this pass, and is told apart by the
    lock clause in the SQL. `claimed` is what that second query comes back with
    — by default everything, as it does when no other agent is running.

    The due-filter is applied in Python by drain_actions, so returning every row
    here is what a real query would do too.
    """

    def __init__(self, actions, outbound=None, claimed=None):
        self._actions = actions
        self._claimed = actions if claimed is None else claimed
        self._rows = actions
        self.outbound = outbound or Outbound()
        self.commits = 0

    def execute(self, stmt):
        self._rows = self._claimed if "FOR UPDATE" in str(stmt) else self._actions
        return self

    def scalars(self):
        return self

    def all(self):
        return self._rows

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


def test_an_action_another_agent_is_holding_is_left_alone(failing_send, capsys):
    """Two agents over one database — an old process that has not exited, a
    restart that overlaps itself — must not both hand the same message to SMTP.

    The claim is a locked re-read: rows another agent holds come back skipped,
    and skipped is exactly "not this agent's to send". Without it both passes
    read the same pending row, both send it, and the user's one mail arrives
    twice with nothing able to take it back.
    """
    action = Action()
    db = DB([action], claimed=[])          # the other agent got there first

    applied, failed, sent = agent_actions.drain_actions(db, Bridge(), AccountRow())

    assert (applied, failed, sent) == (0, 0, 0)
    assert action.attempts == 0            # untouched, not failed
    assert capsys.readouterr().out == ""


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


def test_a_send_that_works_says_so(monkeypatch, capsys):
    """The one action whose success has no other witness.

    A flag or a move is the tail end of something the user watched happen in the
    UI; a send is not, and "did that mail actually go out on Friday" is a
    question the log has to be able to answer months later. Everything else in a
    healthy drain stays quiet — the pass prints its own one-line summary."""
    monkeypatch.setattr(agent_actions.smtp, "send_raw", lambda *_a, **_kw: None)
    action = Action()
    db = DB([action])

    applied, failed, sent = agent_actions.drain_actions(db, Bridge(), AccountRow())

    assert (applied, failed, sent) == (1, 0, 1)
    out = capsys.readouterr().out
    assert "sent to arne@example.com" in out
    assert action.status == "done"
    assert db.outbound.state == "sent"


def test_a_delayed_send_is_left_alone_until_its_time(monkeypatch, capsys):
    """The undo window, from the agent's side.

    A message may be written with a deadline before which it must not go out.
    That is not a backoff — it applies to the first attempt, costs no attempt
    count, and says nothing about anything being wrong — so a pass that meets
    one must walk past it in silence and leave the queue exactly as it found it.
    """
    monkeypatch.setattr(agent_actions.smtp, "send_raw", lambda *_a, **_kw: None)
    action = Action()
    action.payload = dict(action.payload,
                          not_before=(utcnow() + timedelta(minutes=5)).isoformat())
    db = DB([action])

    applied, failed, sent = agent_actions.drain_actions(db, Bridge(), AccountRow())

    assert (applied, failed, sent) == (0, 0, 0)
    assert action.attempts == 0
    assert action.status == "pending"
    assert db.outbound.state == "queued"
    assert capsys.readouterr().out == ""

    # And once the deadline is behind it, the same row goes out on the next pass
    # with nothing else having changed.
    action.payload = dict(action.payload,
                          not_before=(utcnow() - timedelta(seconds=1)).isoformat())
    applied, _failed, sent = agent_actions.drain_actions(db, Bridge(), AccountRow())

    assert (applied, sent) == (1, 1)
    assert db.outbound.state == "sent"


def test_an_unreadable_delay_sends_rather_than_stalls(monkeypatch):
    """A not_before that cannot be parsed — hand-edited, or written by something
    that did not share the convention — must not park mail forever. Sending a
    message a few seconds early is a smaller failure than never sending it."""
    monkeypatch.setattr(agent_actions.smtp, "send_raw", lambda *_a, **_kw: None)
    action = Action()
    action.payload = dict(action.payload, not_before="whenever")

    applied, _failed, sent = agent_actions.drain_actions(DB([action]), Bridge(), AccountRow())

    assert (applied, sent) == (1, 1)


def test_a_healthy_flag_push_says_nothing(capsys):
    """Only sends get a line of their own; the rest of the queue is noise."""
    action = Action("setflags", {"uid": 3, "folder": "INBOX", "add": ["\\Seen"]})
    db = DB([action])
    client = type("C", (), {"select_folder": lambda *_a: None,
                            "add_flags": lambda *_a: None})()
    bridge = type("B", (), {"acc": Account(), "ops": lambda _self: client})()

    applied, failed, sent = agent_actions.drain_actions(db, bridge, AccountRow())

    assert (applied, failed, sent) == (1, 0, 0)
    assert capsys.readouterr().out == ""


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
    """An IMAP session that records the commands a move puts to it.

    ``capabilities`` is what the server advertises, and ``source_keeps_uid``
    whether the source folder still holds the UID once the COPY has run — false
    on a server where folders are labels and the COPY was itself the move.
    """

    def __init__(self, capabilities=(), source_keeps_uid=True):
        self.calls = []
        self.capabilities = set(capabilities)
        self.source_keeps_uid = source_keeps_uid

    def has_capability(self, name):
        return name in self.capabilities

    def select_folder(self, name):
        self.calls.append(("select", name))

    def copy(self, uids, to_folder):
        self.calls.append(("copy", uids, to_folder))

    def move(self, uids, to_folder):
        self.calls.append(("move", uids, to_folder))

    def search(self, criteria):
        self.calls.append(("search", criteria))
        return [criteria[1]] if self.source_keeps_uid else []

    def delete_messages(self, uids):
        self.calls.append(("delete", uids))

    def expunge(self, uids=None):
        self.calls.append(("expunge",) if uids is None else ("expunge", uids))


class RoleDB:
    """Answers the one lookup a move makes: the role of its source folder."""

    def __init__(self, role):
        self.role = role

    def scalar(self, _stmt):
        return self.role


def _move(client, role="custom", from_folder="INBOX", to_folder="Trash"):
    bridge = type("B", (), {"acc": Account(), "ops": lambda _self: client})()
    action = Action("move", {"uid": 7, "from_folder": from_folder, "to_folder": to_folder})
    agent_actions.apply_action(RoleDB(role), bridge, AccountRow(), action)
    return client.calls


@pytest.mark.parametrize("role, filed_by_the_copy", [("all", True), ("custom", False)])
def test_a_move_out_of_all_mail_stops_at_the_copy(role, filed_by_the_copy):
    """\\All holds everything the account has, so nothing can be taken out of
    it: Proton answers the EXPUNGE with "operation not allowed" and the action
    fails forever over a step that had nothing to do. Archiving from there is
    the COPY alone. Every other folder still gets the full move."""
    calls = _move(Client(capabilities=("UIDPLUS",)), role=role,
                  from_folder="All Mail", to_folder="Archive")

    assert ("copy", [7], "Archive") in calls
    assert (("expunge", [7]) in calls) is not filed_by_the_copy
    assert (("delete", [7]) in calls) is not filed_by_the_copy


def test_a_move_uses_the_servers_own_move_command_where_there_is_one():
    """One command that the server gets to interpret, instead of a COPY plus a
    deletion this side guessed at. Every server that files one message under
    several labels — the ones the guesswork was wrong about — advertises it."""
    calls = _move(Client(capabilities=("MOVE", "UIDPLUS")))

    assert ("move", [7], "Trash") in calls
    assert not [c for c in calls if c[0] in ("copy", "delete", "expunge")]


def test_a_hand_rolled_move_removes_the_source_copy():
    """No MOVE: COPY, then take the original out — the UID is still in the
    source folder, so there is genuinely a second copy to remove."""
    calls = _move(Client(capabilities=("UIDPLUS",)))

    assert calls == [("select", "INBOX"), ("copy", [7], "Trash"),
                     ("search", ["UID", 7]), ("delete", [7]), ("expunge", [7])]


def test_a_hand_rolled_move_refuses_rather_than_expunge_the_whole_folder():
    """No UIDPLUS, so the deletion cannot be aimed at this message alone — and a
    bare EXPUNGE would take every \\Deleted message in the folder with it,
    including ones another client flagged and has not expunged yet.

    The action fails, which leaves it queued and retried (see _settle): the move
    has not happened, and a move that has not happened can still happen. The
    \\Deleted flag is not set either — a message left flagged is one the next
    client's EXPUNGE sweeps up, which is the same loss by a slower route.
    """
    client = Client()

    with pytest.raises(RuntimeError, match="UIDPLUS"):
        _move(client)

    assert client.calls == [("select", "INBOX"), ("copy", [7], "Trash"),
                            ("search", ["UID", 7])]


def test_a_delete_takes_out_the_one_message_it_names():
    client = Client(capabilities=("UIDPLUS",))
    bridge = type("B", (), {"acc": Account(), "ops": lambda _self: client})()
    action = Action("delete", {"uid": 7, "folder": "Trash"})

    agent_actions.apply_action(RoleDB("trash"), bridge, AccountRow(), action)

    assert client.calls == [("select", "Trash"), ("delete", [7]), ("expunge", [7])]


def test_a_delete_refuses_when_it_cannot_be_aimed():
    """Emptying the trash of one message must not empty it of another client's."""
    client = Client()
    bridge = type("B", (), {"acc": Account(), "ops": lambda _self: client})()
    action = Action("delete", {"uid": 7, "folder": "Trash"})

    with pytest.raises(RuntimeError, match="UIDPLUS"):
        agent_actions.apply_action(RoleDB("trash"), bridge, AccountRow(), action)

    assert client.calls == [("select", "Trash")]


def test_a_hand_rolled_move_that_the_copy_already_finished_deletes_nothing():
    """The bug that ate 22 messages.

    On a server where folders are labels, COPY to Trash *is* the move: it clears
    every other label, so the UID is already gone from the source folder. The
    \\Deleted + EXPUNGE that used to follow unconditionally then said "delete
    this message, which is in Trash", and Proton spells that "delete it for
    good" — no copy left in Trash, in \\All, or anywhere else.
    """
    calls = _move(Client(source_keeps_uid=False))

    assert ("copy", [7], "Trash") in calls
    assert ("search", ["UID", 7]) in calls
    assert not [c for c in calls if c[0] in ("delete", "expunge")]


@pytest.mark.parametrize("exc, expected", [
    (Exception("[SSL: WRONG_VERSION_NUMBER] wrong version number"), "security mode"),
    (TimeoutError("timed out"), "implicit-TLS port"),
    (Exception("SMTPAuthenticationError (535, b'Incorrect login credentials')"),
     "Bridge password"),
])
def test_hints_name_the_config_mistake_behind_a_failed_send(exc, expected):
    assert expected in agent_log.hint(exc)
