"""Unit coverage for the password gate's session tokens and login rate limiter.

Pure unit tests: app.sessions is stdlib-only and every function takes its inputs
as arguments, so neither FastAPI nor a database is needed. The HTTP wiring
(cookie flags, 401s on the routers) is exercised against a live server only when
one is configured with a password, which the throwaway test stack is not.
"""

from app.sessions import (
    LoginRateLimiter, constant_time_eq, issue_token, new_session_id, verify_token,
)

SECRET = "unit-secret"
PASSWORD = "unit-password"
NOW = 1_780_000_000  # fixed clock — expiry math must not depend on wall time
DAY = 86_400
SESSION = "test-session-id"


def token(max_age=30 * DAY, secret=SECRET, password=PASSWORD, now=NOW, session=SESSION):
    return issue_token(secret, password, max_age, session, now=now)


def test_roundtrip():
    # A token verifies to the session it names — which is what the server then
    # looks up, and what logging out deletes.
    assert verify_token(token(), SECRET, PASSWORD, now=NOW) == SESSION


def test_each_login_gets_its_own_name():
    """Two browsers signed in with one password are two sessions, so that
    logging out on the laptop does not sign out the phone."""
    assert new_session_id() != new_session_id()
    first, second = token(session="a"), token(session="b")
    assert verify_token(first, SECRET, PASSWORD, now=NOW) == "a"
    assert verify_token(second, SECRET, PASSWORD, now=NOW) == "b"


def test_a_session_id_cannot_be_swapped_for_another():
    """The id is inside the signed message, so a cookie cannot be edited to name
    somebody else's session."""
    version, _sid, expires, signature = token().split(".")
    forged = f"{version}.someone-elses.{expires}.{signature}"
    assert verify_token(forged, SECRET, PASSWORD, now=NOW) is None


def test_expires_after_max_age():
    t = token(max_age=30 * DAY)
    assert verify_token(t, SECRET, PASSWORD, now=NOW + 30 * DAY - 1)
    assert not verify_token(t, SECRET, PASSWORD, now=NOW + 30 * DAY)


def test_tampered_signature_rejected():
    t = token()
    flipped = t[:-1] + ("0" if t[-1] != "0" else "1")
    assert not verify_token(flipped, SECRET, PASSWORD, now=NOW)


def test_extending_expiry_breaks_signature():
    version, sid, expires, sig = token().split(".")
    forged = f"{version}.{sid}.{int(expires) + 365 * DAY}.{sig}"
    assert not verify_token(forged, SECRET, PASSWORD, now=NOW)


def test_password_change_invalidates_sessions():
    assert not verify_token(token(), SECRET, "new-password", now=NOW)


def test_secret_change_invalidates_sessions():
    assert not verify_token(token(), "new-secret", PASSWORD, now=NOW)


def test_malformed_tokens_rejected():
    for bad in ("", "v2", "v2.sid.notanumber.abc", "v1." + token().split(".", 1)[1], None):
        assert not verify_token(bad, SECRET, PASSWORD, now=NOW)


def test_constant_time_eq():
    assert constant_time_eq("abc", "abc")
    assert not constant_time_eq("abc", "abd")
    assert not constant_time_eq("abc", "abcd")  # unequal lengths must not raise


def test_limiter_locks_after_max_failures():
    lim = LoginRateLimiter(max_failures=5, window_seconds=900)
    for i in range(5):
        assert lim.retry_after("1.2.3.4", now=NOW + i) == 0
        lim.record_failure("1.2.3.4", now=NOW + i)
    assert lim.retry_after("1.2.3.4", now=NOW + 5) > 0
    # Another address is unaffected.
    assert lim.retry_after("5.6.7.8", now=NOW + 5) == 0


def test_limiter_unlocks_when_window_passes():
    lim = LoginRateLimiter(max_failures=5, window_seconds=900)
    for i in range(5):
        lim.record_failure("1.2.3.4", now=NOW + i)
    assert lim.retry_after("1.2.3.4", now=NOW + 899) > 0
    assert lim.retry_after("1.2.3.4", now=NOW + 901) == 0


def test_limiter_success_resets():
    lim = LoginRateLimiter(max_failures=5, window_seconds=900)
    for i in range(5):
        lim.record_failure("1.2.3.4", now=NOW + i)
    lim.reset("1.2.3.4")
    assert lim.retry_after("1.2.3.4", now=NOW + 5) == 0


def test_limiter_table_stays_bounded():
    lim = LoginRateLimiter(max_failures=5, window_seconds=900, max_tracked=100)
    for i in range(500):
        lim.record_failure(f"10.0.0.{i}", now=NOW + i)
    assert len(lim._failures) <= 100
    # The newest attacker is still being counted despite the eviction churn.
    assert lim._failures["10.0.0.499"]
