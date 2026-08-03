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


def send_raw(account: AccountConfig, mail_from: str, rcpt_to: list[str], raw: bytes) -> None:
    server = connect(account)
    try:
        server.sendmail(mail_from, rcpt_to, to_crlf(raw))
    finally:
        server.quit()
