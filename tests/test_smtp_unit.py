"""Unit coverage for the agent's SMTP wire format.

Pure unit test: nothing here opens a socket. What it pins down is the one
transformation that stands between the MIME the server built and the bytes a
mail server actually reads.
"""

import smtplib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import smtp as agent_smtp  # noqa: E402


def test_bare_lf_becomes_crlf():
    """``EmailMessage.as_string()`` writes bare LF and ``sendmail`` leaves a
    bytes payload alone, so this is the only thing making the message legal."""
    assert agent_smtp.to_crlf(b"a\nb\nc") == b"a\r\nb\r\nc"


def test_crlf_is_left_alone():
    """Idempotent — a producer that already emits CRLF must not end up with
    blank lines between every line of the message."""
    assert agent_smtp.to_crlf(b"a\r\nb\r\nc") == b"a\r\nb\r\nc"


def test_lone_cr_is_normalised_too():
    assert agent_smtp.to_crlf(b"a\rb") == b"a\r\nb"


def test_mime_boundaries_end_properly():
    """The case that actually matters. A boundary is only a boundary when the
    line carrying it ends the way the spec says; a parser that misses one has
    no parts to choose between, and the alternative goes missing."""
    raw = (b"Content-Type: multipart/alternative; boundary=\"BOUND\"\n"
           b"\n"
           b"--BOUND\n"
           b"Content-Type: text/plain\n"
           b"\n"
           b"hello\n"
           b"--BOUND--\n")
    out = agent_smtp.to_crlf(raw)
    assert b"\r\n--BOUND\r\n" in out
    assert b"\r\n--BOUND--\r\n" in out
    assert out.count(b"\n") == out.count(b"\r\n")     # no LF survives on its own


# --- the two ways a send goes half-right -------------------------------------


class FakeServer:
    """An SMTP server driven one command at a time, as send_raw drives it.

    Faithful about *where* each answer comes from, because that is the whole
    question here: `mail` and the final status after the body are returned
    values, not exceptions, and a client that does not read them cannot tell a
    rejected message from a delivered one.

    ``refuse`` names the recipients this server will not take, ``final`` is what
    it says once it has read the whole message, and ``die`` names the point at
    which the connection fails — "body" for the reply that never comes back,
    which is the one outcome SMTP gives no way to interpret.
    """

    def __init__(self, refuse=(), final=(250, b"Queued"), sender=(250, b"OK"),
                 go_ahead=(354, b"End data with <CR><LF>.<CR><LF>"), die=None):
        self.refuse = set(refuse)
        self.final = final
        self.sender = sender
        self.go_ahead = go_ahead
        self.die = die
        self.accepted = []
        self.commands = []
        self.wire = []
        self.quit_called = False

    def mail(self, sender):
        return self.sender

    def rcpt(self, addr):
        if addr in self.refuse:
            return 550, b"No such user here"
        self.accepted.append(addr)
        return 250, b"OK"

    def putcmd(self, cmd, *args):
        self.commands.append(cmd)

    def getreply(self):
        # First reply is the go-ahead; the second is the verdict on the message.
        if not self.data_sent:
            return self.go_ahead
        if self.die == "body":
            raise smtplib.SMTPServerDisconnected("connection closed")
        return self.final

    def send(self, payload):
        self.wire.append(payload)

    @property
    def data_sent(self):
        return bool(self.wire)

    def quit(self):
        self.quit_called = True


class Account:
    email = "me@example.com"
    smtp_host = "127.0.0.1"
    smtp_port = 1025
    smtp_security = "starttls"
    smtp_timeout = 60
    username = ""
    password = ""
    verify_cert = False


def _send(monkeypatch, server, rcpt=("a@x.com", "b@x.com")):
    monkeypatch.setattr(agent_smtp, "connect", lambda *_a, **_kw: server)
    agent_smtp.send_raw(Account(), "me@example.com", list(rcpt), b"Subject: hi\r\n\r\nbody")


def test_a_recipient_the_server_refuses_is_reported_not_swallowed(monkeypatch):
    """`sendmail` hands back the refused recipients and raises only when *every*
    one was refused, so a message delivered to two people out of three came back
    as a plain success and the third never heard from you — with nothing said
    about it anywhere."""
    server = FakeServer(refuse=["b@x.com"])

    try:
        _send(monkeypatch, server)
        raise AssertionError("the refusal was swallowed")
    except agent_smtp.PartlyRefused as exc:
        assert "b@x.com" in str(exc)
        assert "550" in str(exc)

    # And it really was delivered to the other one: this is a report, not a
    # failure, which is why the queue settles it as sent (agent/actions.py).
    assert server.accepted == ["a@x.com"]
    assert server.data_sent is True


def test_every_recipient_refused_is_an_ordinary_failure(monkeypatch):
    """Nobody took it, so nothing was delivered — and the mailbox may well be
    back tomorrow. That is the case the ordinary retry is for, and the body is
    never sent at all."""
    import smtplib

    server = FakeServer(refuse=["a@x.com", "b@x.com"])

    try:
        _send(monkeypatch, server)
        raise AssertionError("a wholly refused send must not look like a success")
    except smtplib.SMTPRecipientsRefused:
        pass

    assert server.data_sent is False


def test_a_connection_that_dies_after_the_body_is_not_a_plain_failure(monkeypatch):
    """The gap SMTP leaves at the end: the message has gone, the acknowledgement
    has not come back, and there is no way to ask. Retrying might deliver it
    twice and not retrying might never deliver it — so this is its own outcome,
    and the queue parks it for a person instead of guessing."""
    server = FakeServer(die="body")

    try:
        _send(monkeypatch, server)
        raise AssertionError("an unknown outcome must not read as a failure")
    except agent_smtp.Delivered as exc:
        assert "acknowledge" in str(exc)

    assert server.data_sent is True
    assert server.quit_called is True     # the session is closed either way


def test_a_body_the_server_rejects_is_not_a_delivered_message(monkeypatch):
    """The one that loses mail silently.

    A server reads the whole message and then answers — a full mailbox, a size
    limit, a spam verdict. `smtplib.data()` hands that answer back as a value
    instead of raising on it, so a client that does not read it sees a send that
    completed: the message leaves the Outbox, the queue row is retired, and
    nothing was ever delivered.
    """
    server = FakeServer(final=(550, b"5.7.1 Message rejected"))

    try:
        _send(monkeypatch, server)
        raise AssertionError("a rejected message must not read as a sent one")
    except smtplib.SMTPDataError as exc:
        assert exc.smtp_code == 550

    assert server.wire, "the body was sent — the rejection came after it"


def test_a_temporary_rejection_is_a_failure_like_any_other(monkeypatch):
    """4xx is "not now", which is exactly what the queue's backoff is for."""
    server = FakeServer(final=(452, b"4.2.2 Mailbox full"))

    with pytest.raises(smtplib.SMTPDataError):
        _send(monkeypatch, server)


def test_a_refused_sender_never_gets_as_far_as_the_body(monkeypatch):
    """`mail()` returns its status too. Ignoring it meant carrying on with a
    conversation the server had already ended."""
    server = FakeServer(sender=(550, b"5.7.1 Sender rejected"))

    with pytest.raises(smtplib.SMTPSenderRefused):
        _send(monkeypatch, server)

    assert server.accepted == [] and not server.wire


def test_a_server_that_will_not_take_data_sends_no_body(monkeypatch):
    """No "go ahead" means the message was never started, so this is an ordinary
    retryable failure and not the ambiguous one."""
    server = FakeServer(go_ahead=(451, b"4.3.0 Try again"))

    with pytest.raises(smtplib.SMTPDataError):
        _send(monkeypatch, server)

    assert not server.wire


def test_a_leading_dot_in_the_body_is_escaped(monkeypatch):
    """A line that begins with a period would end the message where it stands."""
    server = FakeServer()
    monkeypatch.setattr(agent_smtp, "connect", lambda *_a, **_kw: server)
    agent_smtp.send_raw(Account(), "me@example.com", ["a@x.com"],
                        b"Subject: hi\r\n\r\n.hidden\r\nvisible\r\n")

    sent = b"".join(server.wire)
    assert b"\r\n..hidden\r\n" in sent
    assert sent.endswith(b"\r\n.\r\n")     # and the real terminator is still there
