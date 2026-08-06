"""Unit coverage for who this server believes about where a request came from.

Pure unit test: the middleware is driven with an ASGI scope directly, so there
is no socket, no server and no proxy — which is the only way to state the thing
that matters, that an *untrusted* peer's forwarded headers are ignored.

Two things downstream read what this decides, and both are wrong in a way that
looks like nothing on a deployment with TLS terminated in front of it:

  * the login cookie's Secure flag follows `request.url.scheme`, so a server
    that thinks it is being spoken to over plain HTTP issues a cookie a browser
    will hand back over plain HTTP; and
  * the login rate limiter counts failures per source address, so a server that
    sees only the proxy counts every user as the same one and locks the whole
    install out after one attacker's five wrong passwords.
"""

import asyncio

import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from core.config import trusted_proxy_hosts

PROXY = "10.0.0.7"          # where Traefik sits on the container network
CLIENT = "203.0.113.9"      # the browser, out on the internet


def seen_by_the_app(trusted, peer=PROXY, headers=None):
    """Run one request through the middleware and hand back the scope the app
    would be handed: what it thinks the scheme and the client are."""
    scope = {
        "type": "http",
        "scheme": "http",
        "client": (peer, 51234),
        "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    seen = {}

    async def app(scope, _receive, _send):
        seen.update(scheme=scope["scheme"], client=scope["client"])

    middleware = ProxyHeadersMiddleware(app, trusted_hosts=trusted)
    asyncio.run(middleware(scope, None, None))
    return seen


FORWARDED = {"x-forwarded-for": CLIENT, "x-forwarded-proto": "https"}


def test_a_trusted_proxy_is_believed():
    seen = seen_by_the_app(trusted_proxy_hosts("10.0.0.0/8"), headers=FORWARDED)

    assert seen["scheme"] == "https"          # so the session cookie gets Secure
    assert seen["client"][0] == CLIENT        # so the limiter counts per browser


def test_an_unknown_peer_is_not_believed():
    """The whole reason this is a list and not a boolean. Anything that can
    reach the port can set these headers; only the addresses the operator named
    are allowed to be speaking for someone else."""
    seen = seen_by_the_app(trusted_proxy_hosts("10.0.0.0/8"), peer="198.51.100.5",
                           headers=FORWARDED)

    assert seen["scheme"] == "http"
    assert seen["client"][0] == "198.51.100.5"


def test_configuring_nothing_trusts_nothing():
    """The default, and right for a laptop install: the browser is talking to
    this server directly, so a forwarded header is somebody's claim, not a fact.
    """
    assert trusted_proxy_hosts("") == []
    seen = seen_by_the_app(["127.0.0.1"], headers=FORWARDED)

    assert seen["scheme"] == "http"
    assert seen["client"][0] == PROXY


@pytest.mark.parametrize("raw, expected", [
    ("10.0.0.0/8", ["10.0.0.0/8"]),
    ("10.0.0.0/8,172.16.0.0/12", ["10.0.0.0/8", "172.16.0.0/12"]),
    (" 10.0.0.1 , 10.0.0.2 ", ["10.0.0.1", "10.0.0.2"]),   # as an env var arrives
    ("", []),
    (None, []),
])
def test_the_configured_list_is_read_the_way_it_is_written(raw, expected):
    assert trusted_proxy_hosts(raw) == expected
