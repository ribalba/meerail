"""Unit coverage for what this server answers over.

Pure unit test: app.transport takes plain values and returns a plain string, so
the rule can be stated without a request, a socket or FastAPI — the same split
app/sessions.py has from app/deps.py.

The thing being pinned down is an ordering, not a policy. `POST /api/auth/login`
has always refused a plaintext connection; what it could not do is refuse it in
time. A browser reaches that route only after being handed the shell, and the
shell is what draws the password form — so by the time the refusal happens the
password has already crossed the network in the clear, and refusing does not
take it back. These tests are about the requests that come *before* that one.
"""

import pytest

from app.transport import (
    PROXY_UNTRUSTED, REDIRECT, REFUSE, SERVE, plaintext_verdict,
)


def verdict(**kw):
    return plaintext_verdict(**{"password_set": True, "secure": False, **kw})


def test_the_page_that_collects_a_password_is_not_served_over_plaintext():
    """The whole point. A GET for the shell over http:// never gets the shell —
    the form cannot be submitted if it was never drawn."""
    assert verdict(method="GET") == REDIRECT


def test_a_plaintext_post_is_refused_rather_than_redirected():
    """A redirect asks the client to repeat the request, and the request it
    would repeat is the one carrying the password. Sending that to a second URL
    is two plaintext copies of it, not zero."""
    assert verdict(method="POST") == REFUSE
    assert verdict(method="PUT") == REFUSE
    assert verdict(method="DELETE") == REFUSE


def test_https_is_served():
    assert verdict(secure=True, method="GET") == SERVE
    assert verdict(secure=True, method="POST") == SERVE


def test_loopback_is_served():
    """`secure` is already true for 127.0.0.1 (app/deps.py::is_secure_request):
    a browser on the same host is not putting anything on a wire."""
    assert verdict(secure=True) == SERVE


def test_an_install_with_no_password_is_left_alone():
    """The localhost install, and most of them. There is no credential to
    protect, and forcing TLS on it would break the thing it is for."""
    assert plaintext_verdict(password_set=False, secure=False, method="GET") == SERVE
    assert plaintext_verdict(password_set=False, secure=False, method="POST") == SERVE


def test_an_unbelieved_proxy_is_explained_instead_of_redirected():
    """The misconfiguration this would otherwise turn into a redirect loop.

    TLS ends at Traefik and `trusted_proxies` does not name it, so every request
    arrives looking like plain HTTP from a container address. Redirect it to
    https:// and the browser goes back to Traefik, which forwards it here, which
    sees plain HTTP again — forever, with the actual problem never stated.
    """
    assert verdict(forwarded_proto="https", method="GET") == PROXY_UNTRUSTED
    assert verdict(forwarded_proto="https", method="POST") == PROXY_UNTRUSTED


def test_the_forwarded_header_is_read_but_never_believed():
    """It decides which *message* an operator gets, never whether to serve.

    Anything that can reach the port can set this header. Trusting it here would
    hand every attacker the same bypass `trusted_proxies` exists to deny, so a
    request claiming https and not proven to be https is still not served.
    """
    assert verdict(forwarded_proto="https") != SERVE
    assert verdict(forwarded_proto="HTTPS") == PROXY_UNTRUSTED   # case is not a signal


@pytest.mark.parametrize("proto", ["", "http", None])
def test_no_https_claim_is_just_a_browser_on_http(proto):
    assert verdict(forwarded_proto=proto, method="GET") == REDIRECT
