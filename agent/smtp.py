"""SMTP sending via Bridge. Fully wired for outbound actions in M5; the send
primitive lives here so both the action drain loop and tests can use it."""

from __future__ import annotations

import smtplib
import ssl

from config import AccountConfig


def connect(account: AccountConfig, timeout: float | None = None) -> smtplib.SMTP:
    """Open an authenticated SMTP session. Caller is responsible for quit()."""
    ctx = ssl.create_default_context()
    if not account.verify_cert:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    kwargs = {"timeout": timeout} if timeout is not None else {}
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
        server.sendmail(mail_from, rcpt_to, raw)
    finally:
        server.quit()
