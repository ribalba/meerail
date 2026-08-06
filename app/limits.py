"""A ceiling on request bodies, applied before anything has read one.

FastAPI resolves a route's dependencies *after* it has read and parsed the
body: `get_request_handler` calls `await request.form()` or `await
request.body()` and only then runs `solve_dependencies`, which is where
`require_ui_auth` lives. So on every POST the whole body is already in memory —
or, for a multipart upload, already spooled to a temporary file — before this
server has decided whether the sender is allowed to talk to it at all. The
per-attachment cap in app/routers/compose.py runs later still: it counts bytes
back out of a file Starlette has finished writing.

That is what let a stranger fill the disk of a password-protected install, and
it is what this middleware is for. It answers from Content-Length where there is
one, so an oversized upload costs one response and reads nothing; where there is
none (a chunked body) it counts as the bytes arrive and cuts the request off at
the cap.

Two limits, because the two kinds of request are nothing alike. A JSON body here
is a composed mail's text and a handful of ids; /api/compose/attachments is
deliberately allowed whatever `server.max_attachment_bytes` says. One cap sized
for the second would leave every other route free to buffer 100 MB of nothing.

A reverse proxy in front should have a limit of its own — it can refuse the
connection without waking Python at all, and it is the only thing that helps a
server already busy with the last one. This is the floor for when it does not,
and for the install that has no proxy. See COOLIFY.md.

No FastAPI import: this is plain ASGI, and tests/test_limits_unit.py drives it
without a server.
"""

from __future__ import annotations

import json


def declared_length(scope) -> int | None:
    """The request's Content-Length, or None when it did not send a usable one."""
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _refuse(send, limit: int) -> None:
    body = json.dumps({
        "detail": f"Request body too large — this endpoint accepts at most {limit} bytes"
    }).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": 413,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            # The rest of the body is still on its way up the socket. Answering
            # without closing would leave the connection out of step with the
            # client for whatever it sends next.
            (b"connection", b"close"),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class MaxBodySize:
    """Refuse an over-large request body before anything downstream reads it.

    `overrides` maps an exact path to its own limit, which is how the one
    endpoint that exists to receive large files gets to. A limit of 0 — for a
    path or as the default — means no ceiling.
    """

    def __init__(self, app, *, default_bytes: int, overrides: dict[str, int] | None = None):
        self.app = app
        self.default_bytes = default_bytes
        self.overrides = dict(overrides or {})

    def limit_for(self, path: str) -> int:
        return self.overrides.get(path, self.default_bytes)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        limit = self.limit_for(scope.get("path", ""))
        if limit <= 0:
            return await self.app(scope, receive, send)

        declared = declared_length(scope)
        if declared is not None and declared > limit:
            # Decided from the headers alone: not a byte of this has been read,
            # which is the whole point of being this early.
            return await _refuse(send, limit)

        seen = 0
        over = False        # the cap was passed mid-body
        started = False     # the app got a real response out before that
        answered = False    # our 413 has gone

        async def counted_receive():
            nonlocal seen, over
            message = await receive()
            if message.get("type") == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    over = True
                    # Starlette reads this as ClientDisconnect: the request
                    # unwinds where it stands, and the bytes still arriving are
                    # written nowhere.
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message) -> None:
            nonlocal started, answered
            if over and not started:
                # Whatever the app is about to say about a client that hung up,
                # this is what actually happened to it.
                if not answered:
                    answered = True
                    await _refuse(send, limit)
                return
            if message.get("type") == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted_receive, guarded_send)
        except Exception:
            # A request we cut off short is expected to fall over somewhere;
            # anything else is a real error and still belongs upstairs.
            if not over:
                raise
        if over and not started and not answered:
            await _refuse(send, limit)
