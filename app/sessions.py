"""Signed, expiring browser-session tokens for the password gate, plus the
login rate limiter.

A token is ``v2.<id>.<expires>.<hmac>``. The signature is what makes it
unforgeable and the id is what makes it *revocable*: the id names a row the
server keeps (``core.models.UiSession``), and logging out deletes that row.

Both halves are needed, and the signature is the half this file used to have on
its own. Stateless tokens cannot be taken back — a cookie copied off a machine
stayed valid for its full thirty days however many times its owner pressed Log
out, because nothing anywhere recorded that they had. Signing still earns its
keep in front of the lookup: a forged or expired token is rejected without
touching the database, so an attacker cannot make the login gate do a query per
guess.

The signing key is derived from the secret_key AND the password together, which
buys two more properties:

  * changing either one invalidates every session that is out there, and
  * forging a token requires knowing the password even on an install that left
    secret_key at its shipped default.

The expiry is inside the signed message, so it cannot be extended by editing the
token.

The whole decision lives here — signature, session row, API token — rather than
in the FastAPI dependency that calls it, so that "who is allowed in" can be
tested against a real database without a web server in the way. app/deps.py is
the adapter: it turns a request into these arguments and this answer into a 401.
No FastAPI import in this file.
"""

import hashlib
import hmac
import secrets
import time
from datetime import timedelta

from core.models import UiSession, utcnow

# How stale ``last_seen_at`` may get before a request writes it. A session's
# last use is worth knowing and is not worth an UPDATE on every API call, and
# the UI makes a lot of them.
TOUCH_EVERY = timedelta(minutes=5)

TOKEN_VERSION = "v2"


def constant_time_eq(a: str, b: str) -> bool:
    """Compare two secrets without leaking where they diverge — or their lengths,
    which a bare compare_digest on unequal-length strings gives away."""
    da = hashlib.sha256(a.encode("utf-8")).digest()
    db = hashlib.sha256(b.encode("utf-8")).digest()
    return hmac.compare_digest(da, db)


def _signing_key(secret_key: str, password: str) -> bytes:
    material = f"meerail-ui-session\0{secret_key}\0{password}".encode("utf-8")
    return hashlib.sha256(material).digest()


def _signature(secret_key: str, password: str, session_id: str, expires: int) -> str:
    msg = f"{TOKEN_VERSION}.{session_id}.{expires}".encode("utf-8")
    return hmac.new(_signing_key(secret_key, password), msg, hashlib.sha256).hexdigest()


def new_session_id() -> str:
    """The name of one login, and the handle logging out revokes it by."""
    return secrets.token_urlsafe(18)


def issue_token(secret_key: str, password: str, max_age_seconds: int,
                session_id: str, now: float | None = None) -> str:
    expires = int(time.time() if now is None else now) + int(max_age_seconds)
    signature = _signature(secret_key, password, session_id, expires)
    return f"{TOKEN_VERSION}.{session_id}.{expires}.{signature}"


def verify_token(token: str, secret_key: str, password: str,
                 now: float | None = None) -> str | None:
    """The session id this token names, or None if it is not a token we issued.

    A truthy return is not yet an authenticated request: the id still has to
    name a session the server has not revoked. See app/deps.py.
    """
    try:
        version, session_id, expires_str, signature = token.split(".")
        expires = int(expires_str)
    except (AttributeError, ValueError):
        return None
    if version != TOKEN_VERSION:
        return None
    expected = _signature(secret_key, password, session_id, expires)
    if not hmac.compare_digest(signature, expected):
        return None
    if int(time.time() if now is None else now) >= expires:
        return None
    return session_id


class LoginRateLimiter:
    """Per-address throttle on failed logins.

    In-memory is enough: the app is a single process, and the point is to make
    online guessing of one password impractical, not to survive restarts — a
    restart resets the counters but an attacker cannot trigger one.

    An address that fails `max_failures` times inside `window_seconds` is locked
    out until the oldest failure ages past the window. Successes clear the
    slate. The table is capped so a botnet cycling source addresses grows state,
    not unboundedly.
    """

    def __init__(self, max_failures: int = 5, window_seconds: int = 900, max_tracked: int = 10_000):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.max_tracked = max_tracked
        self._failures: dict[str, list[float]] = {}

    def _prune(self, addr: str, now: float) -> list[float]:
        cutoff = now - self.window_seconds
        kept = [t for t in self._failures.get(addr, []) if t > cutoff]
        if kept:
            self._failures[addr] = kept
        else:
            self._failures.pop(addr, None)
        return kept

    def retry_after(self, addr: str, now: float | None = None) -> int:
        """Seconds until this address may try again; 0 = not locked out."""
        now = time.time() if now is None else now
        failures = self._prune(addr, now)
        if len(failures) < self.max_failures:
            return 0
        return max(1, int(failures[0] + self.window_seconds - now) + 1)

    def record_failure(self, addr: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if addr not in self._failures and len(self._failures) >= self.max_tracked:
            for other in list(self._failures):
                self._prune(other, now)
            if len(self._failures) >= self.max_tracked:
                # Still full of live entries: drop the least-recently-failed.
                oldest = min(self._failures, key=lambda a: self._failures[a][-1])
                del self._failures[oldest]
        self._failures.setdefault(addr, []).append(now)

    def reset(self, addr: str) -> None:
        self._failures.pop(addr, None)


# --- the sessions a server is holding ----------------------------------------


def start(db, secret_key: str, password: str, max_age_seconds: int) -> str:
    """Open a session and return the cookie value naming it."""
    now = utcnow()
    session_id = new_session_id()
    db.add(UiSession(id=session_id, created_at=now, last_seen_at=now,
                     expires_at=now + timedelta(seconds=max_age_seconds)))
    # Sessions that have run out are of no interest to anyone and nothing else
    # ever visits this table; a login is the natural moment to sweep them.
    db.query(UiSession).filter(UiSession.expires_at < now).delete()
    db.commit()
    return issue_token(secret_key, password, max_age_seconds, session_id)


def revoke(db, token: str | None, secret_key: str, password: str) -> None:
    """End the session a cookie names.

    Silent about one that is already gone: logging out twice is not an error,
    and neither is logging out with a cookie this server never issued.
    """
    if not token:
        return
    session_id = verify_token(token, secret_key, password)
    if session_id:
        db.query(UiSession).filter(UiSession.id == session_id).delete()
        db.commit()


def authorize(db, *, secret_key: str, password: str, api_token: str,
              authorization: str | None, cookie: str | None) -> bool:
    """May this request in?

    Three answers in order, and the order is deliberate. No password configured
    is an open install — a localhost meerail asks nobody for anything. An API
    token, if one is set, is what a scripted client presents; it is not the UI
    password, which used to be accepted here and made the thing a person types
    into a browser a permanent key to the mailbox. And then the cookie:
    signature first, database second, so a forged or expired one is refused
    without a query and guessing at the gate costs this server nothing.
    """
    if not password:
        return True
    if api_token and authorization is not None and constant_time_eq(
        authorization, f"Bearer {api_token}"
    ):
        return True

    session_id = cookie and verify_token(cookie, secret_key, password)
    if not session_id:
        return False
    row = db.get(UiSession, session_id)
    now = utcnow()
    if row is None or row.expires_at <= now:
        # Signed by us, and not a session any more: logged out, expired, or
        # swept. This is the half a signature cannot answer.
        return False
    if now - row.last_seen_at > TOUCH_EVERY:
        row.last_seen_at = now
        db.commit()
    return True
