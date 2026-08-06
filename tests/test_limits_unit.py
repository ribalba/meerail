"""Unit coverage for the ceiling on request bodies.

Pure unit test: the middleware is plain ASGI and is driven with a scope and a
list of body chunks, so there is no server, no FastAPI and no disk.

What it is guarding is an ordering inside FastAPI that is easy to miss and
impossible to fix from inside a route. `get_request_handler` reads and parses
the body *before* it resolves dependencies — so `require_ui_auth` on a router
runs after the upload has been spooled to a temporary file, and the per-
attachment cap inside the handler runs after that. An unauthenticated stranger
therefore got to choose how much of this server's disk and memory a request
they were not allowed to make would cost, and only then be told 401.
"""

import asyncio
import json

from app.limits import MaxBodySize


def run(middleware, chunks=(b"",), path="/api/x", content_length=None, method="POST"):
    """One request through the middleware; hand back (status, body, reached).

    `reached` is whether the app behind it ever ran — for the Content-Length
    case that is the whole assertion, since the point is that nothing is read.
    """
    headers = []
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    scope = {"type": "http", "method": method, "path": path, "headers": headers}

    pending = list(chunks)
    reached = {"app": False, "bytes": 0}
    sent = []

    async def receive():
        if not pending:
            return {"type": "http.request", "body": b"", "more_body": False}
        body = pending.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(pending)}

    async def app(_scope, recv, send):
        reached["app"] = True
        while True:
            message = await recv()
            if message["type"] == "http.disconnect":
                # What Starlette turns into ClientDisconnect. A real app unwinds
                # here; this one reports the same way a 500 would have.
                await send({"type": "http.response.start", "status": 500, "headers": []})
                await send({"type": "http.response.body", "body": b"disconnected"})
                return
            reached["bytes"] += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message):
        sent.append(message)

    asyncio.run(MaxBodySize(app, **middleware)(scope, receive, send))
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body, reached


SMALL = {"default_bytes": 100}


def test_an_oversized_body_is_refused_from_the_headers_alone():
    """Nothing is read, which is the entire value of being this early: the cost
    of a 10 GB upload from a stranger is one response."""
    status, body, reached = run(SMALL, content_length=10_000)

    assert status == 413
    assert reached["app"] is False
    assert reached["bytes"] == 0
    assert "too large" in json.loads(body)["detail"]


def test_a_body_within_the_limit_is_passed_straight_through():
    status, body, reached = run(SMALL, chunks=[b"x" * 50], content_length=50)

    assert status == 200
    assert body == b"ok"
    assert reached["bytes"] == 50


def test_a_chunked_body_is_counted_as_it_arrives():
    """No Content-Length to answer from, so the cap has to be enforced against
    the bytes themselves — otherwise omitting the header is the way past it."""
    status, _, reached = run(SMALL, chunks=[b"x" * 60, b"x" * 60])

    assert status == 413
    # The first chunk was under the cap and the second crossed it; nothing after
    # that point reaches the app.
    assert reached["bytes"] <= 100


def test_a_chunked_body_within_the_limit_still_arrives_whole():
    status, body, reached = run(SMALL, chunks=[b"x" * 40, b"x" * 40])

    assert status == 200
    assert reached["bytes"] == 80


def test_the_upload_route_gets_its_own_ceiling():
    """One cap for both would be wrong in both directions: sized for uploads it
    lets every JSON route buffer 100 MB, and sized for JSON it breaks the one
    endpoint whose job is receiving large files."""
    middleware = {"default_bytes": 100, "overrides": {"/api/compose/attachments": 5_000}}

    assert run(middleware, content_length=4_000, path="/api/compose/attachments")[0] != 413
    assert run(middleware, content_length=4_000, path="/api/compose/send")[0] == 413
    assert run(middleware, content_length=9_000, path="/api/compose/attachments")[0] == 413


def test_zero_means_no_ceiling():
    """The escape hatch for an install that has a proxy doing this properly."""
    status, _, reached = run({"default_bytes": 0}, chunks=[b"x" * 10_000],
                             content_length=10_000)

    assert status == 200
    assert reached["bytes"] == 10_000


def test_a_refusal_says_413_even_though_the_app_saw_a_disconnect():
    """The app is told the client hung up, because that is the only thing ASGI
    has for "stop reading". What goes back to the client must still be the real
    reason, not the 500 the app would have written about a broken connection.
    """
    status, body, _ = run(SMALL, chunks=[b"x" * 200])

    assert status == 413
    assert b"disconnected" not in body


def test_a_websocket_connection_is_not_touched():
    """/api/stream is a long-lived connection with no body to measure."""
    seen = {}

    async def app(scope, _receive, _send):
        seen["type"] = scope["type"]

    asyncio.run(MaxBodySize(app, default_bytes=1)({"type": "websocket"}, None, None))
    assert seen["type"] == "websocket"
