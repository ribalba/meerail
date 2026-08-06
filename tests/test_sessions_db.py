"""What a session is, once it exists: who it lets in and how it ends.

Needs the database (a session is a row) and nothing else — the throwaway test
stack runs without a password, so the gate is a no-op there and these call the
dependency directly with the password patched in, which is the same code every
request runs through.

The behaviour being pinned is the one a stateless token cannot have. Signing
proves a cookie came from this server; it cannot take one back. A cookie copied
off a machine used to go on working for the rest of its thirty days however many
times its owner pressed Log out, because nothing anywhere recorded that they had.
"""

import pytest

import dbfixture
from app import sessions

SECRET = "test-secret"
PASSWORD = "correct-horse"
DAY = 86_400


@pytest.fixture
def token():
    """No API token configured, which is the default and the usual state."""
    return ""


def allowed(cookie=None, authorization=None, api_token="", password=PASSWORD) -> bool:
    """Would a request carrying this get through?"""
    with dbfixture.session() as db:
        return sessions.authorize(db, secret_key=SECRET, password=password,
                                  api_token=api_token, authorization=authorization,
                                  cookie=cookie)


def sign_in() -> str:
    with dbfixture.session() as db:
        return sessions.start(db, SECRET, PASSWORD, 30 * DAY)


def test_logging_out_ends_the_session_here_not_only_in_the_browser(token):
    cookie = sign_in()
    assert allowed(cookie=cookie)

    # The same cookie value, copied elsewhere before the logout — which is the
    # whole point: deleting it from one browser cannot reach this copy.
    stolen = cookie
    with dbfixture.session() as db:
        sessions.revoke(db, cookie, SECRET, PASSWORD)

    assert not allowed(cookie=stolen)


def test_logging_out_here_leaves_the_other_browser_alone(token):
    laptop, phone = sign_in(), sign_in()

    with dbfixture.session() as db:
        sessions.revoke(db, laptop, SECRET, PASSWORD)

    assert not allowed(cookie=laptop)
    assert allowed(cookie=phone)


def test_a_perfectly_signed_cookie_for_no_session_is_refused(token):
    """The signature says this server issued it. The session says whether it is
    still one — and after a logout, or after the row is gone for any other
    reason, the answer is no."""
    cookie = sign_in()
    dbfixture.drop_sessions()

    assert not allowed(cookie=cookie)


def test_an_expired_session_is_refused_even_with_time_left_on_the_signature(token):
    """Two clocks, and the row is the one that decides. It is what an operator
    can shorten; the token's own expiry was fixed when it was issued."""
    cookie = sign_in()
    dbfixture.expire_sessions()

    assert not allowed(cookie=cookie)


def test_nonsense_is_refused_without_asking_the_database(token):
    assert not allowed(cookie="not-a-token")
    assert not allowed(cookie="")
    assert not allowed()


def test_the_ui_password_is_not_an_api_credential():
    """It used to be: `Authorization: Bearer <the password>` opened every
    endpoint. That made the thing a person types into a browser a permanent key
    to the whole mailbox, revocable only by changing the password and signing
    every browser out."""
    assert not allowed(authorization=f"Bearer {PASSWORD}")


def test_a_configured_api_token_is():
    assert allowed(authorization="Bearer s3cret-token", api_token="s3cret-token")
    assert not allowed(authorization="Bearer wrong", api_token="s3cret-token")
    # And still not the password, which is a different credential entirely.
    assert not allowed(authorization=f"Bearer {PASSWORD}", api_token="s3cret-token")


def test_no_password_means_no_gate():
    """A localhost install is open by default, and nothing above changes that."""
    assert allowed(password="")
