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

    All of these are transient and the retry loop handles them by itself; the
    point is that the log should say so, rather than leaving you to guess
    whether `no such user` means your config is wrong. It usually doesn't.
    """
    text = str(exc).lower()

    if "no such user" in text:
        return ("Bridge is listening but has not loaded this account yet — it is "
                "still starting, locked, or the account is signed out in the Bridge UI. "
                "Retrying; no config change needed if the address shows up in Bridge.")
    if "too many login attempts" in text:
        return ("Bridge is rate-limiting logins after repeated failures. "
                "Backing off; it clears on its own.")
    if "authentication failed" in text or "invalid credentials" in text:
        return ("Bridge rejected the password. Copy it again from the Bridge UI "
                "(Mailbox details) into agent/config.toml — it is not your Proton "
                "password and it changes when the account is re-added.")
    if isinstance(exc, ConnectionRefusedError) or "connection refused" in text:
        return "Nothing is listening there — Bridge, Postgres or Tika is not up yet."
    return ""
