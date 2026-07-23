"""Signed, expiring browser-session tokens for the password gate, plus the
login rate limiter.

Stateless on purpose: a token is "v1.<expires>.<hmac>", so nothing has to be
stored server-side and a restart logs nobody out. The signing key is derived
from the secret_key AND the password together, which buys two properties:

  * changing either one invalidates every session that is out there, and
  * forging a token requires knowing the password even on an install that left
    secret_key at its shipped default.

The expiry is inside the signed message, so it cannot be extended by editing
the token. Stdlib only — the unit tests import this without FastAPI installed.
"""

import hashlib
import hmac
import time

TOKEN_VERSION = "v1"


def constant_time_eq(a: str, b: str) -> bool:
    """Compare two secrets without leaking where they diverge — or their lengths,
    which a bare compare_digest on unequal-length strings gives away."""
    da = hashlib.sha256(a.encode("utf-8")).digest()
    db = hashlib.sha256(b.encode("utf-8")).digest()
    return hmac.compare_digest(da, db)


def _signing_key(secret_key: str, password: str) -> bytes:
    material = f"meerail-ui-session\0{secret_key}\0{password}".encode("utf-8")
    return hashlib.sha256(material).digest()


def _signature(secret_key: str, password: str, expires: int) -> str:
    msg = f"{TOKEN_VERSION}.{expires}".encode("utf-8")
    return hmac.new(_signing_key(secret_key, password), msg, hashlib.sha256).hexdigest()


def issue_token(secret_key: str, password: str, max_age_seconds: int, now: float | None = None) -> str:
    expires = int(time.time() if now is None else now) + int(max_age_seconds)
    return f"{TOKEN_VERSION}.{expires}.{_signature(secret_key, password, expires)}"


def verify_token(token: str, secret_key: str, password: str, now: float | None = None) -> bool:
    try:
        version, expires_str, signature = token.split(".")
        expires = int(expires_str)
    except (AttributeError, ValueError):
        return False
    if version != TOKEN_VERSION:
        return False
    expected = _signature(secret_key, password, expires)
    if not hmac.compare_digest(signature, expected):
        return False
    return int(time.time() if now is None else now) < expires


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
