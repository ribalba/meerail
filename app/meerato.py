"""Client for Meerato's external task API.

Meerato hands out one "private URL" per user — `https://host/api/create?token=…`
— which both lists the buckets/statuses (GET) and creates a task (POST). This
module owns everything that knows about Meerato's shapes; the router above it
only does auth, the database, and turning `MeeratoError` into a response.

Kept free of FastAPI so it can be unit-tested with `core`'s deps alone (see
tests/README.md) — the URL parsing has to be right before anything is stored,
since every other call builds its URL off what it returns.

Attachments take a detour. The token endpoint creates the task but has no
attachment route; the only one reachable without a Meerato session cookie is the
task's own share token, which comes back on the create response. So: create,
then push each file at `/api/public/{public_token}/attachments`.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx

from core.config import get_settings
from . import nethost

# Meerato is a peer service on the user's own network, not something we hold a
# request open waiting for. Uploads get longer — attachments run to 25 MB.
_TIMEOUT = httpx.Timeout(15.0)
_UPLOAD_TIMEOUT = httpx.Timeout(60.0)


class MeeratoError(Exception):
    """Meerato refused, or could not be reached."""


class BlockedHost(MeeratoError, nethost.BlockedHost):
    """This URL points somewhere the server will not fetch from.

    The Meerato URL is a string typed into Settings and then requested by the
    *server*, which sits on a network the browser cannot see: the compose
    network with the database and Tika on it, the host's own loopback, a cloud
    provider's metadata address on 169.254.169.254. Left unrestricted, "Add
    Task" is a request-forger — whoever can open Settings can aim the server at
    those addresses and read back status codes and error text from services that
    were never exposed on purpose.

    So destinations are checked against where they actually resolve, not against
    how they are spelled: a hostname that answers with 127.0.0.1 is refused just
    as a literal one is. An install whose Meerato really is on the LAN says so
    with `server.meerato_allow_private_hosts`, which is the deployment this was
    written for and now an explicit choice rather than the default.

    Both a MeeratoError and a `nethost.BlockedHost`: the check itself lives in
    app/nethost.py and is shared with the LLM client, but a caller of this module
    catches Meerato's own exception hierarchy and must not have to know that.
    """


class OptionsUnsupported(MeeratoError):
    """This Meerato has no GET /api/create — the discovery endpoint that lists
    buckets and statuses is newer than the deployment we are pointed at.

    Not a configuration error: POST still works, so the URL is worth keeping and
    tasks can still be filed. They just land wherever Meerato's own defaults put
    them, because there is nothing to offer the user a choice from.
    """


def parse_endpoint(raw: str) -> tuple[str, str]:
    """Split the pasted private URL into (base, token).

    Accepts the URL exactly as Meerato's API page shows it, and also a bare
    origin with the token attached, since that is the other shape people paste.
    Raises ValueError with a message meant for the settings field.
    """
    parsed = urlsplit((raw or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Enter a full http(s) URL")
    token = parse_qs(parsed.query).get("token", [""])[0].strip()
    if not token:
        raise ValueError("That URL has no ?token=… — copy the whole URL from Meerato's API page")
    # Strip the endpoint back to Meerato's root so the attachment route can be
    # built off it too. Anything else in the path is a sub-path mount and stays.
    path = parsed.path.rstrip("/")
    if path.endswith("/api/create"):
        path = path[: -len("/api/create")]
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")), token


# What a saved URL shows instead of its token. This module exists so that the
# token stays on the server — every Meerato call is proxied from here rather
# than made by the browser — and a settings page that displayed the string gave
# that away for nothing. Sent back to the server it means "keep the one you
# have", which is what still allows the host to be corrected in place.
TOKEN_MASK = "•" * 8


def endpoint_url(base: str, token: str) -> str:
    """The canonical private URL for a (base, token) — parse_endpoint's inverse."""
    return f"{base}/api/create?token={token}"


def redact(raw: str) -> str:
    """A saved URL with its token masked, or "" if there is nothing parseable."""
    try:
        base, _ = parse_endpoint(raw)
    except ValueError:
        return ""
    return endpoint_url(base, TOKEN_MASK)


# The check itself is in app/nethost.py, shared with the LLM client. These two
# wrappers are what supplies Meerato's half of it: the setting that opts this
# install into private destinations, and Meerato's own BlockedHost — so a caller
# can go on catching `meerato.MeeratoError` and get everything this module can
# fail with, the refused destination included.
_HINT = "server.meerato_allow_private_hosts"


def _allow_private() -> bool:
    return get_settings().meerato_allow_private_hosts


def guard_destination(base: str) -> str | None:
    """Where this URL may be fetched from, or None for "no pinning".
    See nethost.guard_destination — this only chooses the policy."""
    try:
        return nethost.guard_destination(base, allow_private=_allow_private(), hint=_HINT)
    except nethost.BlockedHost as exc:
        raise BlockedHost(str(exc)) from exc


def pinned(base: str) -> tuple[str, dict]:
    """The URL to actually request plus the arguments that keep it pointed at the
    address `guard_destination` approved. See nethost.pinned."""
    try:
        return nethost.pinned(base, allow_private=_allow_private(), hint=_HINT)
    except nethost.BlockedHost as exc:
        raise BlockedHost(str(exc)) from exc


class MaybeCreated(MeeratoError):
    """The request went out and no answer came back.

    Only ever raised for creating a task, and it exists because "failed" is not
    what happened: Meerato may have created it and lost the reply, or never
    received the request at all. Retrying is the obvious move and it is the one
    that produces two tasks, so this says which kind of failure it was and lets
    the person decide — there is no idempotency key to offer instead, since the
    key would have to be honoured at Meerato's end and this client cannot put it
    there.
    """


def _translate(exc: Exception) -> MeeratoError:
    """Meerato's failure, said in our voice. Its own `detail` is the useful part
    (bad token, unknown bucket); everything else collapses to unreachable."""
    if isinstance(exc, httpx.TimeoutException):
        return MaybeCreated("Meerato did not answer in time. The task may still have been "
                            "created — check Meerato before sending it again.")
    if isinstance(exc, httpx.HTTPStatusError):
        # 405 is what a FastAPI route registered for POST only answers to a GET;
        # 404 covers a Meerato old enough not to have the path at all.
        if exc.response.status_code in (404, 405) and exc.request.method == "GET":
            return OptionsUnsupported("This Meerato cannot list buckets or statuses")
        if exc.response.status_code == 401:
            return MeeratoError("Meerato rejected the token — the URL may have been regenerated")
        detail = exc.response.reason_phrase
        try:
            detail = exc.response.json().get("detail") or detail
        except Exception:
            pass
        return MeeratoError(f"Meerato: {detail}")
    return MeeratoError("Could not reach Meerato")


def fetch_options(base: str, token: str) -> dict:
    """Meerato's buckets + statuses: `{statuses, buckets, default_bucket_id}`."""
    target, pin = pinned(base)
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=False) as client:
            res = client.get(f"{target}/api/create", params={"token": token}, **pin)
            res.raise_for_status()
            return res.json()
    except Exception as exc:
        raise _translate(exc) from exc


def create_task(base: str, token: str, title: str, text: str,
                bucket_id: str | None = None, status: str | None = None,
                schedule_date: str | None = None, schedule_status: str = "on_list") -> dict:
    """Create the task; returns Meerato's `TodoOut` (id, public_token, …).

    `schedule_date` (ISO `YYYY-MM-DD`) asks Meerato to flip the task to
    `schedule_status` on that day — which is how a task is parked in the Backlog
    now and surfaces on the list later, rather than sitting in the way until then.
    """
    target, pin = pinned(base)
    body: dict = {"title": title[:500], "text": text}
    if bucket_id:
        body["bucket_id"] = bucket_id
    if status:
        body["status"] = status
    if schedule_date:
        body["schedule"] = {"date": schedule_date, "status": schedule_status}
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=False) as client:
            res = client.post(f"{target}/api/create", params={"token": token},
                              json=body, **pin)
            res.raise_for_status()
            return res.json()
    except Exception as exc:
        raise _translate(exc) from exc


def upload_attachments(base: str, task: dict, files: list[tuple[str, bytes, str]]
                       ) -> tuple[list[str], list[str]]:
    """Push `(filename, content, content_type)` triples at the new task.

    Returns (uploaded, failed) filenames rather than raising: the task already
    exists by this point, and a file that would not go up is worth reporting
    alongside it — not as a failure that implies nothing was created.
    """
    if not files:
        return [], []
    try:
        # Checked and pinned again rather than taken on trust from the create
        # that just succeeded: this is a second request, and it is the address at
        # the moment of the request that decides where it goes.
        target, pin = pinned(base)
    except MeeratoError as exc:
        return [], [f"{name} ({exc})" for name, _content, _type in files]
    public_token, todo_id = task.get("public_token"), task.get("id")
    if not public_token or not todo_id:
        return [], ["Meerato returned no share token — files were not attached"]

    uploaded: list[str] = []
    failed: list[str] = []
    url = f"{target}/api/public/{public_token}/attachments"
    params = {"owner_type": "todo", "owner_id": todo_id}
    with httpx.Client(timeout=_UPLOAD_TIMEOUT, follow_redirects=False) as client:
        for filename, content, content_type in files:
            try:
                client.post(url, params=params,
                            files={"file": (filename, content, content_type)},
                            **pin).raise_for_status()
                uploaded.append(filename)
            except Exception:
                failed.append(filename)
    return uploaded, failed
