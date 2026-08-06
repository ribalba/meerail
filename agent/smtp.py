"""SMTP sending via Bridge. Fully wired for outbound actions in M5; the send
primitive lives here so both the action drain loop and tests can use it."""

from __future__ import annotations

import re
import smtplib
import ssl

from core.config import AccountConfig

_EOL = re.compile(rb"\r\n|\r|\n")


def to_crlf(raw: bytes) -> bytes:
    """Line endings as SMTP requires them.

    ``EmailMessage.as_string()`` writes bare LF, and ``sendmail`` fixes up the
    endings of a ``str`` payload only — bytes go on the wire exactly as handed
    over. Without this every message leaves with LF endings, which RFC 5321
    does not permit. A single-part mail usually survives that, because the
    first hop tidies up after us; a multipart one is at the mercy of whichever
    parser meets it first, and a boundary counts as a boundary only when the
    line carrying it ends the way the spec says. Idempotent, so a caller that
    already hands over CRLF is left alone.
    """
    return _EOL.sub(b"\r\n", raw)


def connect(account: AccountConfig, timeout: float | None = None) -> smtplib.SMTP:
    """Open an authenticated SMTP session. Caller is responsible for quit().

    ``timeout`` defaults to the account's ``smtp_timeout`` rather than to
    smtplib's default, which is no timeout at all: a socket built that way is
    blocking, and a blocking socket against a server that accepts the connection
    and then says nothing parks the caller forever. That caller is the sync
    thread — send is drained at the top of every pass — so the account stops
    syncing entirely, with no error, until the process is restarted.
    """
    ctx = ssl.create_default_context()
    if not account.verify_cert:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    kwargs = {"timeout": timeout if timeout is not None else account.smtp_timeout}
    if account.smtp_security == "ssl":
        server = smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, context=ctx, **kwargs)
    else:
        server = smtplib.SMTP(account.smtp_host, account.smtp_port, **kwargs)
        if account.smtp_security == "starttls":
            server.starttls(context=ctx)

    if account.username and account.password:
        try:
            server.login(account.username or account.email, account.password)
        except smtplib.SMTPNotSupportedError:
            pass  # server doesn't offer AUTH (e.g. a local test server) — send unauthenticated
    return server


class Delivered(Exception):
    """The message reached the server, and the server never said so.

    Raised when the connection dies after the message body has been handed over
    but before the final acknowledgement comes back. SMTP has no way to tell that
    apart from "it never arrived": the server may have accepted and queued the
    mail and then lost the socket, or dropped it on the floor.

    It is an exception because the send did not complete, and its own exception
    because the ordinary answer to an incomplete send — try again — is the wrong
    one here. Retrying a message the far end already took is how one mail arrives
    twice, and nothing on a later connection can ask "did you get that". So the
    queue parks it instead and says so, and a person decides (see
    agent/actions.py::_settle and the Outbox's Send now).
    """


class PartlyRefused(Exception):
    """Some of the recipients were refused, and the rest were taken.

    Not a failure — the message *was* delivered, to everyone the server accepted
    — so it must not be retried: a second attempt would deliver it to those
    people twice for the sake of the ones it can never reach. It carries the
    refusals so they can be reported against the message they belong to.
    """

    def __init__(self, refused: dict):
        self.refused = refused
        detail = "; ".join(f"{addr}: {code} {msg.decode(errors='replace') if isinstance(msg, bytes) else msg}"
                           for addr, (code, msg) in sorted(refused.items()))
        super().__init__(f"delivered, but the server refused {len(refused)} recipient(s) — {detail}")


def send_raw(account: AccountConfig, mail_from: str, rcpt_to: list[str], raw: bytes) -> None:
    """Hand one message to the SMTP server.

    The conversation is driven step by step rather than through ``sendmail``,
    because the two things that can go half-right are invisible from the outside
    of that call.

    The first is a partial refusal. ``sendmail`` returns a dict of the recipients
    the server would not take and raises only when it would take *none* of them —
    so a message accepted for two addresses out of three came back as a plain
    success, and the third person simply never heard from you. Nothing in meerail
    ever mentioned it again.

    The second is the gap at the end. After the body goes out there is one more
    response to read, and a connection that dies while waiting for it leaves no
    way to know whether the server had already taken the message. Driving the
    steps here is what makes that distinguishable from a failure before the data
    was sent, which is genuinely safe to retry.

    Every step's answer is read, and that is not a detail. ``mail()`` and
    ``data()`` *return* their status rather than raising on it — only the
    intermediate "go ahead" of DATA raises — so a server that rejects the sender,
    or takes the whole message and then answers ``550 message rejected``, says so
    in a value that is easy to drop on the floor. Dropped, the send is a success
    as far as everything downstream can tell: the message leaves the Outbox, the
    queue row is retired, and the mail was never delivered to anyone.
    """
    server = connect(account)
    try:
        code, resp = server.mail(mail_from)
        if code != 250:
            # The envelope sender was refused, so nothing else in this
            # conversation can succeed. An ordinary failure: the address may be
            # one the server accepts again once its configuration is fixed.
            raise smtplib.SMTPSenderRefused(code, resp, mail_from)
        refused = {}
        for addr in rcpt_to:
            code, resp = server.rcpt(addr)
            if code not in (250, 251):
                refused[addr] = (code, resp)
        if len(refused) == len(rcpt_to):
            # Nobody took it, so nothing was delivered and the ordinary retry is
            # exactly right — the mailbox may be back tomorrow.
            raise smtplib.SMTPRecipientsRefused(refused)
        _send_body(server, to_crlf(raw))
        if refused:
            raise PartlyRefused(refused)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001 — the send is already decided
            pass


# A line that begins with a period, which in DATA would end the message. RFC
# 5321 says to double it; the receiving server undoes that.
_LEADING_DOT = re.compile(rb"(?m)^\.")


def _send_body(server: smtplib.SMTP, body: bytes) -> None:
    """The DATA phase, one step at a time.

    ``smtplib.SMTP.data`` does all of this in one call and hands back the final
    status instead of raising on it, which is how a rejected message came to read
    as a delivered one. Driving it here is also the only way to know *where* a
    dropped connection dropped, and that changes the answer completely:

      * before the "go ahead", or part way through the body — the server has an
        incomplete DATA and will deliver nothing, so this is an ordinary failure
        and the queue retries it;
      * while waiting for the final status, with the terminating dot already
        sent — the server may have taken the message and may not, and there is
        no way to ask. That is `Delivered`, which is parked rather than retried.

    Treating the two alike either duplicates mail or parks mail that certainly
    never went.
    """
    server.putcmd("data")
    code, resp = server.getreply()
    if code != 354:
        raise smtplib.SMTPDataError(code, resp)

    quoted = _LEADING_DOT.sub(b"..", body)
    if not quoted.endswith(b"\r\n"):
        quoted += b"\r\n"
    server.send(quoted + b".\r\n")

    try:
        code, resp = server.getreply()
    except (smtplib.SMTPServerDisconnected, TimeoutError, OSError) as exc:
        raise Delivered(f"the connection failed while waiting for the server to "
                        f"acknowledge the message ({exc!r})") from exc
    if code != 250:
        # The server read the whole message and said no — a full mailbox, a size
        # limit, a spam verdict. Nothing was delivered, so this is a failure like
        # any other and the queue keeps the message and keeps trying.
        raise smtplib.SMTPDataError(code, resp)
