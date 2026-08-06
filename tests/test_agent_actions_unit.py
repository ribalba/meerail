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
from imapclient import IMAPClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import actions as agent_actions  # noqa: E402
from core.mail.parse import content_key  # noqa: E402
import log as agent_log  # noqa: E402
from core.models import utcnow  # noqa: E402


# The UID epoch every folder in this file is on. A UID is only unique within
# one of these, so an action and the folder it is applied to have to agree on it
# or the number names a different message — see _select_verified.
EPOCH = 42


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
        self.message_pk = None
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

    The first is the queue scan; the second re-reads one row under FOR UPDATE
    SKIP LOCKED to lease it for this pass, and is told apart by the lock clause
    in the SQL. `claimed` is which rows that second query comes back with — by
    default all of them, as it does when no other agent is running.

    The real scan filters for due rows in SQL; returning every row here and
    letting the pass's own checks apply is the stricter test of the two, since
    anything the Python side stopped doing would show up as work happening that
    should not have.
    """

    def __init__(self, actions, outbound=None, claimed=None):
        self._actions = actions
        self._claimable = actions if claimed is None else claimed
        self._rows = actions
        self.outbound = outbound or Outbound()
        self.commits = 0
        self.rollbacks = 0

    def execute(self, stmt):
        try:
            sql = str(stmt)
        except Exception:            # a dialect-specific scan clause; not the lock read
            sql = ""
        if "FOR UPDATE" in sql:
            wanted = set(stmt.compile().params.values())
            self._rows = [a for a in self._claimable if a.id in wanted]
        else:
            self._rows = self._actions
        return self

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self, _stmt):
        return None

    def get(self, _model, _pk):
        return self.outbound

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


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
    action = Action("setflags",
                    {"uid": 3, "folder": "INBOX", "uidvalidity": 9, "add": ["\\Seen"]})
    db = DB([action])
    client = type("C", (), {"select_folder": lambda *_a: {b"UIDVALIDITY": 9},
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

    ``capabilities`` is what the server advertises, ``source_keeps_uid`` whether
    the source folder still holds the UID once the COPY has run — false on a
    server where folders are labels and the COPY was itself the move — and
    ``uidvalidity`` the UID epoch SELECT reports, which is what says whether the
    numbers in the queue still mean anything (see EPOCH).

    Every command that acts on messages records *which folder was selected* when
    it ran, because that is what IMAP acts on: `UID COPY 7 Trash` copies the
    selected mailbox's message 7, and a UID means nothing without the mailbox it
    was issued by. A stub that only recorded the arguments called a move correct
    while it was copying out of the folder it was moving into.
    """

    def __init__(self, capabilities=(), source_keeps_uid=True, uidvalidity=EPOCH,
                 in_target=(), target_body=None, refuses=(), refusal=None):
        self.calls = []
        self.capabilities = set(capabilities)
        self.source_keeps_uid = source_keeps_uid
        self.uidvalidity = uidvalidity
        # Folders the server will not accept mail into, and how it says so. A
        # tagged NO ("operation not allowed", the default here) is a verdict on
        # the command; the abort subclass is the connection going away mid-way
        # and no verdict at all. The two have to end differently — see
        # agent/imap.refused.
        self.refuses = set(refuses)
        self.refusal = refusal or (lambda: IMAPClient.Error(
            "move failed: operation not allowed"))
        # Message-IDs the target folder already holds — what a COPY whose answer
        # was lost leaves behind for the retry to find.
        self.in_target = set(in_target)
        # ...and what that message *is*. The default is this message; anything
        # else is a different mail wearing the same id.
        self.target_body = MOVED if target_body is None else target_body

    def fetch(self, uids, what):
        self.calls.append(("fetch", list(uids)))
        return {uid: {b"BODY[]": self.target_body} for uid in uids}

    def has_capability(self, name):
        return name in self.capabilities

    selected = None

    def select_folder(self, name, readonly=False):
        self.selected = name
        self.calls.append(("peek", name) if readonly else ("select", name))
        return {b"UIDVALIDITY": self.uidvalidity}

    def copy(self, uids, to_folder):
        self.calls.append(("copy", self.selected, uids, to_folder))
        self._answer(to_folder)

    def move(self, uids, to_folder):
        self.calls.append(("move", self.selected, uids, to_folder))
        self._answer(to_folder)

    def _answer(self, to_folder):
        """What the server says to a message being filed into that folder. The
        command is recorded either way — it was sent."""
        if to_folder in self.refuses:
            raise self.refusal()

    def search(self, criteria):
        self.calls.append(("search", criteria))
        if criteria[0] == "HEADER":                    # "is it in the target already?"
            return [1] if criteria[2] in self.in_target else []
        return [criteria[1]] if self.source_keeps_uid else []

    def delete_messages(self, uids):
        self.calls.append(("delete", self.selected, uids))

    def expunge(self, uids=None):
        self.calls.append(("expunge", self.selected) if uids is None
                          else ("expunge", self.selected, uids))


MSGID = "carried@example.com"
# The message being moved, and what it hashes to — which is what a retry
# compares a candidate in the target folder against before deciding the copy has
# already been made.
MOVED = b"Message-ID: <carried@example.com>\r\nSubject: Moved\r\n\r\nthe body\r\n"
MOVED_HASH = content_key(MOVED)


class RoleDB:
    """Answers the two lookups a move makes: the role of its source folder, and
    the identity of the mail it is about (which is how a retry asks whether the
    copy it is about to make has already been made)."""

    def __init__(self, role, message_id=MSGID, content_hash=MOVED_HASH):
        self.role = role
        self.identity = (message_id, content_hash)

    def scalar(self, _stmt):
        return self.role

    def execute(self, _stmt):
        return self

    def first(self):
        return self.identity


def _move(client, role="custom", from_folder="INBOX", to_folder="Trash", db=None):
    bridge = type("B", (), {"acc": Account(), "ops": lambda _self: client})()
    action = Action("move", {"uid": 7, "from_folder": from_folder,
                             "uidvalidity": EPOCH, "to_folder": to_folder})
    action.message_pk = 11
    agent_actions.apply_action(db or RoleDB(role), bridge, AccountRow(), action)
    return client.calls


@pytest.mark.parametrize("role, filed_by_the_copy", [("all", True), ("custom", False)])
def test_a_move_out_of_all_mail_stops_at_the_copy(role, filed_by_the_copy):
    """\\All holds everything the account has, so nothing can be taken out of
    it: Proton answers the EXPUNGE with "operation not allowed" and the action
    fails forever over a step that had nothing to do. Archiving from there is
    the COPY alone. Every other folder still gets the full move."""
    calls = _move(Client(capabilities=("UIDPLUS",)), role=role,
                  from_folder="All Mail", to_folder="Archive")

    assert ("copy", "All Mail", [7], "Archive") in calls
    assert (("expunge", "All Mail", [7]) in calls) is not filed_by_the_copy
    assert (("delete", "All Mail", [7]) in calls) is not filed_by_the_copy


def test_a_move_uses_the_servers_own_move_command_where_there_is_one():
    """One command that the server gets to interpret, instead of a COPY plus a
    deletion this side guessed at. Every server that files one message under
    several labels — the ones the guesswork was wrong about — advertises it."""
    calls = _move(Client(capabilities=("MOVE", "UIDPLUS")))

    assert ("move", "INBOX", [7], "Trash") in calls
    assert not [c for c in calls if c[0] in ("copy", "delete", "expunge")]


def test_a_hand_rolled_move_removes_the_source_copy():
    """No MOVE: look in the target, COPY, then take the original out — the UID
    is still in the source folder, so there is genuinely a second copy to
    remove."""
    calls = _move(Client(capabilities=("UIDPLUS",)))

    # Every message-bearing command names the folder it was issued against, and
    # the copy's is the *source*: the check above leaves the target selected, and
    # a COPY sent in that state copies the target's own uid 7 — or nothing — and
    # the source is expunged either way on the strength of it.
    assert calls == [("select", "INBOX"),
                     ("peek", "Trash"), ("search", ["HEADER", "MESSAGE-ID", MSGID]),
                     ("select", "INBOX"),
                     ("copy", "INBOX", [7], "Trash"),
                     ("search", ["UID", 7]),
                     ("delete", "INBOX", [7]), ("expunge", "INBOX", [7])]


def test_a_message_that_only_shares_an_id_is_not_proof_the_copy_landed():
    """The one that would delete the message outright.

    A retry skips the COPY when the target already holds this message — and a
    Message-ID is not that. Ids are written by senders and two mails can wear
    one, so a colliding message sitting in Trash used to read as "already
    copied": the COPY was skipped, the source was expunged, and the mail the user
    asked to move was then nowhere on the server at all.

    Size and internal date settle it, both read from this server in this session.
    """
    client = Client(capabilities=("UIDPLUS",), in_target=[MSGID],
                    target_body=b"Message-ID: <carried@example.com>\r\n\r\nnot it\r\n")

    calls = _move(client)

    assert ("copy", "INBOX", [7], "Trash") in calls      # copied, not assumed
    assert ("expunge", "INBOX", [7]) in calls            # and only then removed


def test_a_hand_rolled_move_retried_after_a_lost_answer_copies_once():
    """The COPY landed and its response did not: a dropped connection, a Bridge
    restart, the watchdog closing the socket. The action stays queued — a move
    that has not visibly happened is one this module retries forever — and the
    retry used to copy the message a second time, leaving two of it in Trash.

    So the target is asked first, by the one name that survives a copy.
    """
    client = Client(capabilities=("UIDPLUS",), in_target=[MSGID])

    calls = _move(client)

    assert not [c for c in calls if c[0] == "copy"]     # already there from last time
    # ...and the half that did not finish still does: the source copy goes, out
    # of the source folder, which is selected again before anything is removed.
    assert ("delete", "INBOX", [7]) in calls and ("expunge", "INBOX", [7]) in calls


def test_a_move_with_no_message_id_still_copies():
    """Mail whose sender wrote no Message-ID cannot be looked for, and "I cannot
    tell" is not evidence that the copy landed. Copying twice is a duplicate the
    user can delete; not copying at all is a message taken out of the source
    folder and put nowhere."""
    client = Client(capabilities=("UIDPLUS",), in_target=[MSGID])

    calls = _move(client, db=RoleDB("custom", message_id=None))

    assert ("copy", "INBOX", [7], "Trash") in calls
    assert not [c for c in calls if c[0] == "peek"]      # nothing to ask about


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

    assert client.calls == [("select", "INBOX"),
                            ("peek", "Trash"), ("search", ["HEADER", "MESSAGE-ID", MSGID]),
                            ("select", "INBOX"),
                            ("copy", "INBOX", [7], "Trash"),
                            ("search", ["UID", 7])]


def _delete(client, payload=None, db=None):
    bridge = type("B", (), {"acc": Account(), "ops": lambda _self: client})()
    action = Action("delete", payload or {"uid": 7, "folder": "Trash", "uidvalidity": EPOCH})
    agent_actions.apply_action(db or RoleDB("trash"), bridge, AccountRow(), action)
    return action


def test_a_delete_takes_out_the_one_message_it_names():
    client = Client(capabilities=("UIDPLUS",))

    _delete(client)

    assert client.calls == [("select", "Trash"),
                            ("delete", "Trash", [7]), ("expunge", "Trash", [7])]


def test_a_delete_refuses_when_it_cannot_be_aimed():
    """Emptying the trash of one message must not empty it of another client's."""
    client = Client()

    with pytest.raises(RuntimeError, match="UIDPLUS"):
        _delete(client)

    assert client.calls == [("select", "Trash")]


# --- the UID epoch -----------------------------------------------------------
#
# A UID identifies a message only within one UIDVALIDITY. Bridge starts a new
# one whenever its side of the mapping is rebuilt — a re-login, a reinstall, a
# restored cache — and every number then goes back into the pool to be handed
# out again from 1. A queued action carrying only a folder and a number is
# therefore aimed at nothing in particular after that, and for a delete "nothing
# in particular" means whichever message inherited its UID.


def test_an_action_from_an_older_uid_epoch_deletes_nothing():
    """The one that would destroy the wrong mail. The folder has been rebuilt
    since this delete was queued, so uid 7 is no longer the message the user
    emptied out of Trash — it is whatever arrived after the reset."""
    client = Client(capabilities=("UIDPLUS",), uidvalidity=EPOCH + 1)

    with pytest.raises(agent_actions.StaleUid):
        _delete(client)

    assert client.calls == [("select", "Trash")]      # opened, and nothing else


def test_a_move_from_an_older_uid_epoch_moves_nothing():
    client = Client(capabilities=("MOVE", "UIDPLUS"), uidvalidity=EPOCH + 1)

    with pytest.raises(agent_actions.StaleUid):
        _move(client)

    assert [c for c in client.calls if c[0] != "select"] == []


def test_a_flag_change_from_an_older_uid_epoch_flags_nothing():
    """Not just the destructive ones: a flag applied to a stranger's message is
    still a change nobody asked for, in a mailbox nobody was looking at."""
    client = Client(uidvalidity=EPOCH + 1)
    client.add_flags = lambda *_a: client.calls.append(("add_flags",))
    bridge = type("B", (), {"acc": Account(), "ops": lambda _self: client})()
    action = Action("setflags", {"uid": 7, "folder": "INBOX", "uidvalidity": EPOCH,
                                 "add": ["\\Seen"]})

    with pytest.raises(agent_actions.StaleUid):
        agent_actions.apply_action(RoleDB("inbox"), bridge, AccountRow(), action)

    assert ("add_flags",) not in client.calls


def test_a_row_queued_before_epochs_were_recorded_is_refused():
    """A legacy row cannot be rescued by reading the epoch off the folder.

    That column says what the *last sync pass* saw, and the thing that moves it
    is a pass noticing the epoch has changed — so a legacy row that outlives one
    such pass (it was in its backoff, or past the pass's limit) would be compared
    against the new epoch, match it, and apply an old UID to whatever message
    inherited the number. The fallback failed hardest in the case it was for.

    The cost of refusing is the actions queued at the moment of an upgrade, all
    of which can be asked for again. The cost of the fallback is somebody's mail.
    """
    client = Client(capabilities=("UIDPLUS",))

    with pytest.raises(agent_actions.StaleUid, match="did not record it"):
        _delete(client, {"uid": 7, "folder": "Trash"})

    assert client.calls == [("select", "Trash")]


def test_a_folder_that_says_nothing_about_its_epoch_is_refused():
    """Both halves have to be known for the check to mean anything, and a delete
    that cannot be checked is a delete that does not happen. UIDVALIDITY is a
    required part of a SELECT response, so this is a server behaving oddly —
    which is exactly when guessing is worst."""
    client = Client(capabilities=("UIDPLUS",))
    client.select_folder = lambda name: client.calls.append(("select", name))

    with pytest.raises(agent_actions.StaleUid):
        _delete(client)

    assert client.calls == [("select", "Trash")]


def test_a_stale_action_is_dropped_rather_than_retried_forever(capsys):
    """The one thing in the queue that is not retried. The UID cannot become
    valid again, so every retry would be the same instruction pointed at the
    same stranger's mail — and a queue row that can never succeed would sit
    there being reported as waiting mail for good."""
    action = Action("delete", {"uid": 7, "folder": "Trash", "uidvalidity": EPOCH})
    db = DB([action])
    client = Client(capabilities=("UIDPLUS",), uidvalidity=EPOCH + 1)
    bridge = type("B", (), {"acc": Account(), "ops": lambda _self: client})()

    applied, failed, sent = agent_actions.drain_actions(db, bridge, AccountRow())

    assert (applied, failed, sent) == (0, 0, 0)     # neither applied nor failing
    assert action.status == "stale"
    assert action.attempts == 0                    # nothing was attempted
    out = capsys.readouterr().out
    assert "dropped" in out and "UIDVALIDITY" in out


def test_a_dropped_move_takes_back_the_copy_the_ui_filed(monkeypatch):
    """A move files the message in the target folder here before the server has
    been told anything, and that copy is retired when the real one arrives. If
    the move is dropped, none ever arrives — so the copy goes now, and the
    re-walk the same UIDVALIDITY change triggers puts the message back wherever
    the server actually has it."""
    action = Action("move", {"uid": 7, "from_folder": "INBOX", "to_folder": "Trash",
                             "uidvalidity": EPOCH})
    action.message_pk = 55
    db = DB([action])
    db.scalar = lambda _stmt: 3                    # the Trash folder's id
    dropped = []
    monkeypatch.setattr(agent_actions.store, "drop_pending_placement",
                        lambda _db, pk, mailbox_id: dropped.append((pk, mailbox_id)))
    client = Client(capabilities=("MOVE", "UIDPLUS"), uidvalidity=EPOCH + 1)
    bridge = type("B", (), {"acc": Account(), "ops": lambda _self: client})()

    agent_actions.drain_actions(db, bridge, AccountRow())

    assert action.status == "stale"
    assert dropped == [(55, 3)]


class FoldersDB(DB):
    """A queue that also knows what the account's folders are.

    ``roles`` is imap_name -> role, which is what a move asks the database:
    \\All is not a folder a message can be taken *out* of, and — the half this
    fixture was added for — not one every server will let a message be put
    *into*. ``ids`` answers the second lookup, the one that finds the placement
    a dropped move has to take back.
    """

    def __init__(self, actions, roles, ids=None):
        super().__init__(actions)
        self.roles = roles
        self.ids = ids or {}
        self.updates = []

    def scalar(self, stmt):
        sql = str(stmt)
        name = next((v for v in stmt.compile().params.values() if isinstance(v, str)), None)
        if "mailboxes.role" in sql:
            return self.roles.get(name)
        if "mailboxes.id" in sql:
            return self.ids.get(name)
        return None

    def execute(self, stmt):
        try:
            sql = str(stmt)
        except Exception:                            # not a statement we look at
            sql = ""
        if sql.startswith("UPDATE mailboxes"):
            self.updates.append(stmt.compile().params)
            return self
        return super().execute(stmt)


def _archive_to(to_folder, roles, client, message_pk=55):
    """Drain one queued archive — a move out of the inbox into `to_folder`."""
    action = Action("move", {"uid": 7, "from_folder": "INBOX", "to_folder": to_folder,
                             "uidvalidity": EPOCH})
    action.message_pk = message_pk
    db = FoldersDB([action], roles, ids={to_folder: 9})
    bridge = type("B", (), {"acc": Account(), "ops": lambda _self: client})()
    return action, db, agent_actions.drain_actions(db, bridge, AccountRow())


def test_a_move_into_all_mail_the_server_refuses_is_dropped_not_retried_forever(
        monkeypatch, capsys):
    """The loop this whole path exists to end.

    \\All is a real destination on Gmail — "archive" there is exactly INBOX ->
    All Mail — and no destination at all on Proton, which answers the MOVE with
    "operation not allowed". Nothing in a LIST tells the two apart, so the
    command goes out and the answer decides; and this answer will be the same
    answer in fifteen minutes, and in a week.

    Retrying it was not merely wasted: a queued move is also a claim on the local
    view (core.mail.store.move_in_flight), so every retry went on insisting the
    message had been archived while the server had it in the inbox, and the sweep
    that repairs exactly that was told to wait — forever.
    """
    dropped = []
    monkeypatch.setattr(agent_actions.store, "drop_pending_placement",
                        lambda _db, pk, mailbox_id: dropped.append((pk, mailbox_id)))
    client = Client(capabilities=("MOVE", "UIDPLUS"), refuses=("All Mail",))

    action, db, counts = _archive_to(
        "All Mail", {"INBOX": "inbox", "All Mail": "all"}, client)

    assert counts == (0, 0, 0)              # neither applied nor waiting to be
    assert action.status == "refused"       # and out of the queue for good
    # The copy the UI filed goes back, so the next sweep can put the message
    # where the server has actually had it all along.
    assert dropped == [(55, 9)]
    # ...and the folder is marked, so the app stops choosing a destination this
    # server has already said no to.
    assert [p for p in db.updates if p.get("writes_refused_at")]
    out = capsys.readouterr().out
    assert "dropped" in out and "All Mail" in out and "Archive folder" in out


def test_a_move_into_all_mail_that_the_connection_broke_on_is_still_retried():
    """The difference between an answer and a silence.

    A dropped socket mid-MOVE decided nothing — the server may not even have
    read the command — and dropping the action on the strength of it is a
    filing the user has to do a second time. Only a tagged refusal is a verdict.
    """
    client = Client(capabilities=("MOVE", "UIDPLUS"), refuses=("All Mail",),
                    refusal=lambda: IMAPClient.AbortError("socket error: EOF"))

    action, _db, (applied, failed, _sent) = _archive_to(
        "All Mail", {"INBOX": "inbox", "All Mail": "all"}, client)

    assert (applied, failed) == (0, 1)
    assert action.status == "pending"       # still queued, still due later
    assert action.attempts == 1


def test_a_refusal_from_an_ordinary_folder_is_still_retried():
    """Only \\All is a destination with nothing to wait for. Over quota, a
    folder that has not been created yet, a mailbox somebody is repairing: those
    are refusals too, and they all stop being refusals when the condition behind
    them clears — which is the case this module's patience is for."""
    client = Client(capabilities=("MOVE", "UIDPLUS"), refuses=("Archive",))

    action, _db, (applied, failed, _sent) = _archive_to(
        "Archive", {"INBOX": "inbox", "Archive": "archive", "All Mail": "all"}, client)

    assert (applied, failed) == (0, 1)
    assert action.status == "pending"


def test_a_move_into_all_mail_the_server_does_accept_is_an_ordinary_archive():
    """Gmail publishes no \\Archive: archiving there is dropping the INBOX label
    while the mail stays in the union, which as IMAP is this exact move. It has
    to keep working — the refusal above is the server's to declare, not ours to
    assume from the folder's role."""
    client = Client(capabilities=("MOVE", "UIDPLUS"))

    action, _db, (applied, failed, _sent) = _archive_to(
        "[Gmail]/All Mail", {"INBOX": "inbox", "[Gmail]/All Mail": "all"}, client)

    assert (applied, failed) == (1, 0)
    assert action.status == "done"
    assert ("move", "INBOX", [7], "[Gmail]/All Mail") in client.calls


def test_a_send_the_server_took_from_some_recipients_is_not_sent_again(monkeypatch, capsys):
    """It was delivered — to everyone the server would take. Retrying would send
    it a second time to those people for the sake of the ones no number of
    attempts can reach, so it settles as sent, with the refusals recorded
    against it rather than dropped."""
    refused = agent_actions.smtp.PartlyRefused({"b@x.com": (550, b"No such user")})
    monkeypatch.setattr(agent_actions.smtp, "send_raw",
                        lambda *_a, **_kw: (_ for _ in ()).throw(refused))
    action = Action()
    db = DB([action])

    applied, failed, sent = agent_actions.drain_actions(db, Bridge(), AccountRow())

    assert (applied, failed, sent) == (1, 0, 1)
    assert action.status == "done"
    assert db.outbound.state == "sent"
    assert "b@x.com" in capsys.readouterr().out


def test_a_send_with_no_answer_is_parked_rather_than_retried(monkeypatch, capsys):
    """The connection died between the message going out and the server saying
    it had it. Retrying might deliver it twice and not retrying might never
    deliver it at all; there is no third question to ask, and the choice is the
    user's. So it waits in the Outbox and says what is known."""
    monkeypatch.setattr(agent_actions.smtp, "send_raw", lambda *_a, **_kw: (_ for _ in ()).throw(
        agent_actions.smtp.Delivered("the connection failed while waiting to acknowledge")))
    action = Action()
    db = DB([action])

    applied, failed, _sent = agent_actions.drain_actions(db, Bridge(), AccountRow())

    assert (applied, failed) == (0, 0)         # neither sent nor failed
    assert action.status == "held"             # the drain does not select these
    assert db.outbound.state == "held"
    out = capsys.readouterr().out
    assert "NOT been retried" in out and "Send now" in out


# --- the lease ---------------------------------------------------------------


def test_a_send_is_marked_as_being_sent_before_it_is_sent(monkeypatch):
    """The row has to say "an agent has this" *before* the SMTP conversation,
    and the saying has to be committed — the Outbox reads the database, not this
    process's memory. Without it Cancel could report success for a message
    already delivered, and Send now could re-queue one mid-flight."""
    action = Action()
    db = DB([action])
    seen = {}

    def record(*_a, **_kw):
        seen["status"] = action.status
        seen["commits"] = db.commits
    monkeypatch.setattr(agent_actions.smtp, "send_raw", record)

    agent_actions.drain_actions(db, Bridge(), AccountRow())

    assert seen["status"] == "leased"
    assert seen["commits"] >= 1                     # and written down, not just set
    assert action.status == "done"                  # cleared once it settled


def test_a_send_another_agent_is_already_sending_is_left_alone(monkeypatch, capsys):
    """A lease is a claim two agents can both see, so the second one walks past
    a row the first is inside the SMTP conversation for."""
    monkeypatch.setattr(agent_actions.smtp, "send_raw", lambda *_a, **_kw: None)
    action = Action(status="leased")
    action.updated_at = utcnow()                    # taken a moment ago
    db = DB([action])

    applied, failed, sent = agent_actions.drain_actions(db, Bridge(), AccountRow())

    assert (applied, failed, sent) == (0, 0, 0)
    assert action.status == "leased"
    assert capsys.readouterr().out == ""


def test_a_lease_nobody_came_back_for_is_taken_over(monkeypatch):
    """The agent holding it was killed between taking the row and settling it.
    Nothing else would ever pick the row up, and for a send that is mail that
    never goes out — so a lease that has gone stale is reclaimed, as work that
    is due rather than as a failure."""
    monkeypatch.setattr(agent_actions.smtp, "send_raw", lambda *_a, **_kw: None)
    action = Action(status="leased")
    action.updated_at = utcnow() - agent_actions._LEASE_TTL - timedelta(seconds=1)
    db = DB([action])

    applied, _failed, sent = agent_actions.drain_actions(db, Bridge(), AccountRow())

    assert (applied, sent) == (1, 1)
    assert action.status == "done"


def test_a_hand_rolled_move_that_the_copy_already_finished_deletes_nothing():
    """The bug that ate 22 messages.

    On a server where folders are labels, COPY to Trash *is* the move: it clears
    every other label, so the UID is already gone from the source folder. The
    \\Deleted + EXPUNGE that used to follow unconditionally then said "delete
    this message, which is in Trash", and Proton spells that "delete it for
    good" — no copy left in Trash, in \\All, or anywhere else.
    """
    calls = _move(Client(source_keeps_uid=False))

    assert ("copy", "INBOX", [7], "Trash") in calls
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
