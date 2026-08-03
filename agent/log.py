"""Timestamped stdout logging for the agent.

The agent's normal home is `docker compose logs -f agent`, where the only record
of what it did is what it printed. Bare print() was enough while that was a
terminal you were watching live; in a log you read after the fact it is not —
there is no way to tell a line from this run apart from one three restarts ago,
and an agent that is quiet because it is healthy looks exactly like one that is
quiet because it is wedged.

So every line carries a UTC timestamp and the account it belongs to, and success
is logged as loudly as failure. Docker captures stdout, and the Dockerfile sets
PYTHONUNBUFFERED=1, so these land in `docker logs` as they happen rather than in
a block when the buffer flushes.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timezone

# Colour only for a terminal: `docker logs` is usually piped, and escape codes
# in a captured log are noise. Same rule preflight.py uses.
_COLOUR = sys.stdout.isatty()

_STYLES = {"ok": "32", "warn": "33", "error": "31"}

# One print() per line, serialised. There is a sync thread per account, and two
# threads interleaving fragments of a line produces something worse than no log.
_lock = threading.Lock()


def _paint(text: str, level: str) -> str:
    code = _STYLES.get(level, "")
    return f"\033[{code}m{text}\033[0m" if _COLOUR and code else text


def log(message: str, *, account: str | None = None, level: str = "info") -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    where = f" [{account}]" if account else ""
    with _lock:
        print(f"{stamp}{where} {_paint(message, level)}", flush=True)


def info(message: str, account: str | None = None) -> None:
    log(message, account=account)


def ok(message: str, account: str | None = None) -> None:
    log(message, account=account, level="ok")


def warn(message: str, account: str | None = None) -> None:
    log(message, account=account, level="warn")


def error(message: str, account: str | None = None) -> None:
    log(message, account=account, level="error")


def hint(exc: Exception) -> str:
    """An actionable line for the failures that actually recur in the logs.

    Two kinds live here. Most are transient — the retry loop handles them by
    itself, and the point is that the log should say so rather than leaving you
    to guess whether `no such user` means your config is wrong. It usually
    doesn't. The rest are config mismatches, which retrying cannot fix and which
    are worth naming precisely: the security mode is the one people get wrong,
    because Bridge chooses it per platform and per protocol (macOS answers SMTP
    with implicit TLS, not STARTTLS) and a mismatch does not say so — it hangs
    until the timeout, or fails deep inside the TLS handshake.

    The type name is matched alongside the message because smtplib carries the
    diagnosis in the class rather than the text: SMTPAuthenticationError
    stringifies to a bare status code and a server blob.
    """
    text = f"{type(exc).__name__} {exc}".lower()

    # TLS in the wrong place: a plaintext/STARTTLS client that opened an
    # implicit-TLS port sees the server's handshake as garbage, and an SSL
    # client on a plaintext port sees the greeting as a broken record.
    if ("wrong version number" in text or "record layer failure" in text
            or "unknown protocol" in text or "packet length too long" in text):
        return ("The security mode does not match what the server speaks on that "
                "port. Set imap_security/smtp_security in meerail.toml to match "
                "Bridge's own settings — on macOS Bridge offers SMTP as \"ssl\", "
                "not \"starttls\".")
    if isinstance(exc, TimeoutError) or "timed out" in text or "timeout" in text:
        return ("Timed out with the connection open. A \"starttls\" client on an "
                "implicit-TLS port waits for a greeting that never comes, so check "
                "the port and security mode in meerail.toml against Bridge's "
                "settings before assuming the server is slow.")
    if "smtpauthenticationerror" in text:
        return ("The SMTP server rejected the password. It is the Bridge password "
                "(Mailbox details), not your Proton password, and it changes when "
                "the account is re-added.")
    if "smtpsenderrefused" in text or "smtprecipientsrefused" in text:
        return ("The server refused the envelope. The From must be an address this "
                "account may send as — check the addresses under this account in "
                "meerail.toml and in Bridge.")
    if "no such user" in text:
        return ("Bridge is listening but has not loaded this account yet — it is "
                "still starting, locked, or the account is signed out in the Bridge UI. "
                "Retrying; no config change needed if the address shows up in Bridge.")
    if "too many login attempts" in text:
        return ("Bridge is rate-limiting logins after repeated failures. "
                "Backing off; it clears on its own.")
    if "authentication failed" in text or "invalid credentials" in text:
        return ("Bridge rejected the password. Copy it again from the Bridge UI "
                "(Mailbox details) into meerail.toml — it is not your Proton "
                "password and it changes when the account is re-added.")
    if isinstance(exc, ConnectionRefusedError) or "connection refused" in text:
        return "Nothing is listening there — Bridge, Postgres or Tika is not up yet."
    return ""
