"""Unit coverage for the Meerato client's pure pieces.

Pure unit test: no server, no database, no Meerato. `parse_endpoint` has to be
right before anything is stored, since everything downstream builds its URLs off
what it returns; `create_task`'s request body has to be right because a wrong
shape files the task in the wrong place rather than failing loudly; and
`guard_destination` has to be right because the request goes out from the
server, on a network the person typing the URL cannot otherwise reach.

The destination tests use IP literals, which `getaddrinfo` answers without
asking a resolver — so they neither need the network nor depend on what it says.
"""

import httpx
import pytest

import app.meerato as meerato

from app.meerato import (
    TOKEN_MASK, BlockedHost, create_task, endpoint_url, guard_destination,
    parse_endpoint, pinned, redact,
)


def test_splits_the_url_meerato_hands_out():
    base, token = parse_endpoint("https://meerato.example.com/api/create?token=abc123")
    assert base == "https://meerato.example.com"
    assert token == "abc123"


def test_keeps_a_sub_path_mount():
    """Only the /api/create suffix is stripped — a Meerato behind a path prefix
    still needs that prefix to reach its attachment endpoint."""
    base, _ = parse_endpoint("https://host.example/todo/api/create?token=t")
    assert base == "https://host.example/todo"


def test_accepts_a_bare_origin_with_the_token():
    base, token = parse_endpoint("http://localhost:8080?token=t")
    assert base == "http://localhost:8080"
    assert token == "t"


def test_surrounding_whitespace_is_ignored():
    # Pasted URLs routinely arrive with a trailing newline.
    base, token = parse_endpoint("  https://m.example/api/create?token=abc\n")
    assert (base, token) == ("https://m.example", "abc")


@pytest.mark.parametrize("raw", ["", "   ", "meerato.example.com/api/create?token=t",
                                 "ftp://host/api/create?token=t"])
def test_rejects_anything_that_is_not_an_http_url(raw):
    with pytest.raises(ValueError, match="full http"):
        parse_endpoint(raw)


def test_rejects_a_url_with_no_token():
    with pytest.raises(ValueError, match="token"):
        parse_endpoint("https://meerato.example.com/api/create")


# --- The create request ----------------------------------------------------


def _sent_body(monkeypatch, **kwargs) -> dict:
    """Run create_task against a stubbed transport and hand back what it posted."""
    seen: dict = {}

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def post(self, url, params=None, json=None):
            seen.update(url=url, params=params, json=json)
            return httpx.Response(200, json={"id": "1", "public_token": "p", "title": "t"},
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "Client", FakeClient)
    # Where the request goes is a different question, answered by its own tests
    # (and by a real resolver, which has never heard of m.example). This one is
    # about the body.
    monkeypatch.setattr(meerato, "pinned", lambda base: (base, {}))
    create_task("https://m.example", "tok", "Title", "Body", **kwargs)
    return seen


def test_a_plain_task_carries_no_schedule(monkeypatch):
    seen = _sent_body(monkeypatch)
    assert seen["params"] == {"token": "tok"}
    assert seen["json"] == {"title": "Title", "text": "Body"}


def test_a_scheduled_task_parks_in_the_backlog_and_moves_to_now(monkeypatch):
    """What "Send & Ticket" asks for: filed under a bucket, status Backlog, and
    a date on which Meerato flips it onto the list by itself."""
    seen = _sent_body(monkeypatch, bucket_id="b1", status="open", schedule_date="2026-08-01")
    assert seen["json"] == {
        "title": "Title", "text": "Body", "bucket_id": "b1", "status": "open",
        "schedule": {"date": "2026-08-01", "status": "on_list"},
    }


# --- Where the server is allowed to send the request -------------------------
#
# The URL is typed into Settings and fetched by the *server*, which sits on a
# network the browser cannot see: the compose network with Postgres and Tika on
# it, the host's own loopback, a cloud provider's metadata address. Without a
# restriction, "Add Task" is a way to aim the server at all of those and read
# the result back — authenticated, but by whoever can open Settings, which is
# not the same set of people as "whoever may reach the internal network".


@pytest.mark.parametrize("host, what", [
    ("127.0.0.1", "loopback"),
    ("10.1.2.3", "an RFC 1918 address"),
    ("192.168.0.10", "a home network"),
    ("172.16.5.4", "the other private range"),
    ("169.254.169.254", "the cloud metadata service"),
    ("[::1]", "loopback, in IPv6"),
    ("[::ffff:127.0.0.1]", "loopback wearing an IPv6 spelling"),
])
def test_the_server_will_not_fetch_from_its_own_network(host, what):
    with pytest.raises(BlockedHost):
        guard_destination(f"https://{host}:8080")


def test_an_ordinary_public_address_goes_through():
    guard_destination("https://93.184.216.34")          # documentation address, routable


def test_a_hostname_that_cannot_be_resolved_is_not_visited():
    """A failed lookup is not permission to try anyway.

    It reads like "there is nothing there to reach" and it is not: the request
    would do its own lookup, so a name this call missed — a transient SERVFAIL, a
    resolver that answers NXDOMAIN once, an attacker returning failure first and
    127.0.0.1 second — is a name that then goes unchecked. That is the whole
    check undone by an error it was never about.
    """
    with pytest.raises(BlockedHost):
        guard_destination("https://no-such-host.invalid")


def test_the_restriction_can_be_lifted_for_a_meerato_on_the_lan(monkeypatch):
    """Which is the deployment this client was written for. It is a decision the
    operator makes in the config, not one anyone can make from the UI."""
    import app.meerato as meerato

    settings = meerato.get_settings()
    monkeypatch.setattr(settings, "meerato_allow_private_hosts", True)
    guard_destination("http://192.168.1.20:8080")


def test_the_request_goes_to_the_address_that_was_checked():
    """Checking and connecting have to be the same decision.

    A check on its own is a lookup, and the connection makes another one: an
    attacker who owns the name and sets a one-second TTL answers the first with a
    public address and the second with 127.0.0.1, and the request lands exactly
    where the check said it must not. So the approved address is what gets
    connected to, with the name carried in the Host header and in the TLS
    handshake so an ordinary Meerato still answers and still verifies.
    """
    target, extra = pinned("https://one.one.one.one:8443/todo")

    assert target in ("https://1.1.1.1:8443/todo", "https://1.0.0.1:8443/todo")
    assert extra["headers"] == {"Host": "one.one.one.one:8443"}
    assert extra["extensions"] == {"sni_hostname": "one.one.one.one"}


def test_pinning_refuses_the_same_addresses_the_check_does():
    with pytest.raises(BlockedHost):
        pinned("http://127.0.0.1:8080")


def test_a_url_that_cannot_be_pinned_is_refused_rather_than_sent_unpinned():
    """Pinning is the check, so failing to pin has to fail the request. Handing
    back the URL untouched would have sent it with no check at all — the one
    outcome the resolution exists to prevent."""
    with pytest.raises(BlockedHost):
        pinned("https://no-such-host.invalid")


# --- What leaves the server -------------------------------------------------
#
# The token creates tasks in somebody's Meerato. This module exists so that it
# never has to reach the browser at all — every call is proxied from the server,
# for CORS reasons first and this one second — and a Settings page that returned
# the saved URL in full handed it over anyway, to whatever reads the DOM, to a
# screenshot, to the browser's cache. The mask is what the page gets instead.


def test_a_saved_url_comes_back_without_its_token():
    masked = redact("https://meerato.example.com/api/create?token=s3cret-token")

    assert "s3cret-token" not in masked
    assert masked == f"https://meerato.example.com/api/create?token={TOKEN_MASK}"


def test_the_host_survives_the_mask():
    """Which is the only reason the string is returned at all: the field is
    edited in place to correct a host, not retyped whole."""
    masked = redact("https://meerato.example.com/sub/mount/api/create?token=abc")

    assert masked.startswith("https://meerato.example.com/sub/mount/")


def test_the_mask_round_trips_back_to_the_stored_token():
    """What makes editing the host in place work. The page sends the mask back;
    parse_endpoint reads it as the token, and the router swaps in the real one
    (app/routers/tasks.py::put_config) rather than saving eight bullets."""
    base, token = parse_endpoint(redact("https://meerato.example.com/api/create?token=abc"))

    assert base == "https://meerato.example.com"
    assert token == TOKEN_MASK


def test_nothing_saved_masks_to_nothing():
    assert redact("") == ""
    assert redact("not a url") == ""


def test_the_canonical_url_is_what_parse_endpoint_reads_back():
    """endpoint_url is parse_endpoint's inverse, and has to be: put_config
    rebuilds the string it stores out of the two halves."""
    url = endpoint_url("https://meerato.example.com", "abc123")

    assert parse_endpoint(url) == ("https://meerato.example.com", "abc123")
