"""What the read APIs do with input nobody would type on purpose.

Every one of these used to be a 500. A 500 is the wrong answer twice over: it
tells the caller nothing about what to send instead, and it is indistinguishable
from the server being broken — which is the state an operator is trying to rule
out when they look at the log.
"""

import dbfixture
from helpers import SERVER, api


def test_a_page_size_has_a_floor_as_well_as_a_ceiling(account):
    """`limit` was capped and not floored, and a negative one is not a smaller
    page: SQLite reads it as no limit at all and Postgres refuses the query, so
    the same URL is an uncapped read on one database and an error on the other.
    """
    for path in ("/api/messages?scope=unified_inbox&limit=-1",
                 "/api/messages?scope=unified_inbox&limit=0",
                 "/api/search?q=anything&limit=-1",
                 "/api/contacts?q=a&limit=-1",
                 "/api/contacts/related?address=a@b.c&limit=-1"):
        assert api("GET", path)[0] == 422, path


def test_an_offset_cannot_be_negative(account):
    assert api("GET", "/api/messages?scope=unified_inbox&limit=5&offset=-1")[0] == 422
    assert api("GET", "/api/search?q=anything&offset=-1")[0] == 422


def test_a_search_window_is_a_number_of_years_not_a_number(account):
    """`years` becomes a date, and a big enough one overflows the arithmetic
    before it means anything — a 500 for a query that was only asking for
    everything, which `0` already says."""
    assert api("GET", f"/api/search?q=a&account_id={account['id']}&years=999999999")[0] == 422
    assert api("GET", f"/api/search?q=a&account_id={account['id']}&years=-1")[0] == 422
    assert api("GET", f"/api/search?q=a&account_id={account['id']}&years=0")[0] == 200


def test_ordinary_paging_still_works(account):
    code, body = api("GET", "/api/messages?scope=unified_inbox&limit=5&offset=0")
    assert code == 200 and isinstance(body["rows"], list)


def test_patching_an_account_with_null_says_so_rather_than_failing(account):
    """A PATCH field is optional, which is not the same as nullable. Omitting it
    leaves the value alone; sending null used to be assigned straight onto a
    NOT NULL column and died in the flush as a 500."""
    account_id = account["id"]
    for field, value in (("label", None), ("color", None), ("active", None), ("footer", None)):
        code, body = api("PATCH", f"/api/accounts/{account_id}", {field: value})
        assert code == 422, (field, code)
        assert "cannot be null" in str(body)

    # And the ordinary edits are untouched: one field set, the rest left alone.
    before = dbfixture.account_row(account["email"])
    assert api("PATCH", f"/api/accounts/{account_id}", {"label": "Renamed"})[0] == 200
    after = dbfixture.account_row(account["email"])
    assert after["label"] == "Renamed"
    assert after["color"] == before["color"]


def _raw_post(path: str, content_length: int, body: bytes = b"") -> tuple[int, bytes]:
    """POST with a Content-Length we do not intend to honour.

    Deliberately down at socket level: the assertion is that the server answers
    from the headers alone, having read none of the body — which is a thing you
    can only observe by not sending one. urllib would insist on the two agreeing.
    """
    import socket
    from urllib.parse import urlsplit

    url = urlsplit(SERVER)
    host, port = url.hostname, url.port or 80
    request = (
        f"POST {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {content_length}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()
    with socket.create_connection((host, port), timeout=15) as sock:
        sock.sendall(request + body)
        chunks = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
    raw = b"".join(chunks)
    return int(raw.split(b" ", 2)[1]), raw


def test_an_enormous_body_is_refused_without_being_read(require_server):
    """FastAPI reads and parses a request body *before* it resolves the
    dependency that would have said 401, so on a password-protected install a
    stranger got to choose how much memory and disk a request they were not
    allowed to make would cost. The ceiling has to be applied above all of that,
    which is why it is middleware and not a check in a route.

    Nothing is sent but the headers: a server that answers 413 here is one that
    decided before reading, which is the whole property.
    """
    status, _ = _raw_post("/api/compose/send", content_length=200 * 1024 * 1024)

    assert status == 413


def test_an_ordinary_body_is_not_touched_by_the_ceiling(require_server):
    """The floor under the guard: a normal request still reaches its route and
    gets that route's answer, not a 413."""
    body = b'{"nonsense": true}'
    status, _ = _raw_post("/api/compose/send", content_length=len(body), body=body)

    assert status == 422        # the route's own validation, so it got there
