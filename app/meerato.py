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

import ipaddress
import socket
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx

from core.config import get_settings

# Meerato is a peer service on the user's own network, not something we hold a
# request open waiting for. Uploads get longer — attachments run to 25 MB.
_TIMEOUT = httpx.Timeout(15.0)
_UPLOAD_TIMEOUT = httpx.Timeout(60.0)


class MeeratoError(Exception):
    """Meerato refused, or could not be reached."""


class BlockedHost(MeeratoError):
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


def _reachable(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Is this an ordinary address on the public internet?

    Everything else is refused: loopback, the RFC 1918 ranges, link-local (which
    is where the cloud metadata services live), the reserved and multicast
    blocks, and the unspecified address. IPv4 written as an IPv6 address is
    unwrapped first, or ::ffff:127.0.0.1 would walk straight past every one of
    those checks.
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def guard_destination(base: str) -> str | None:
    """Check where this URL leads, and hand back the address it may be fetched
    from. None means "no pinning" — see BlockedHost for what is being stopped.

    Resolved here rather than trusted from the URL, and resolved again for every
    call rather than once when the URL was saved: a name that answered publicly
    on Tuesday can answer with 127.0.0.1 today, and it is the address at the
    moment of the request that decides where the request goes.

    Every answer is checked, not just the first, and the one that comes back is
    then the one connected to. That last part is the difference between a check
    and a guarantee: a name whose records are swapped between this lookup and the
    connection — the whole of DNS rebinding, and cheap to arrange, since the
    attacker owns the zone and picks the TTL — passes a check that only looks and
    then lands the request on 127.0.0.1 anyway. Resolving once and connecting to
    what we resolved leaves the second lookup with nothing to decide (see
    ``pinned``, which is what every request here goes through).
    """
    if get_settings().meerato_allow_private_hosts:
        return None
    host = urlsplit(base).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        # A lookup that fails is not permission to try anyway. It reads like
        # "there is nothing there to reach", and it is not: the request that
        # follows does its *own* lookup, and a name whose answer this call missed
        # — a transient SERVFAIL, an NXDOMAIN from one resolver, an attacker
        # returning failure on the first query and 127.0.0.1 on the second — is a
        # name that then goes unchecked. That is the whole check, undone by an
        # error it was never meant to be about. So a destination that cannot be
        # checked is a destination that is not visited.
        raise BlockedHost(
            f"Could not resolve {host}, so there is no way to tell where this would "
            f"go. Nothing was requested. ({exc})") from exc
    allowed = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if not _reachable(ip):
            raise BlockedHost(
                f"{host} resolves to {ip}, which is a private or local address. "
                "meerail will not fetch from those unless the install says it may "
                "(server.meerato_allow_private_hosts).")
        allowed.append(str(ip))
    return allowed[0] if allowed else None


def pinned(base: str) -> tuple[str, dict]:
    """The base URL to actually request, plus the per-request arguments that keep
    it pointed at the checked address.

    The URL's host is replaced by the address ``guard_destination`` approved, so
    no second name resolution stands between the check and the connection. Two
    things travel with it to keep that from breaking an ordinary Meerato:

      * ``Host``, so a server behind name-based virtual hosting still knows which
        site is being asked for; and
      * ``sni_hostname``, which is both the name offered in the TLS handshake and
        the name the certificate is verified against — without it, pinning would
        turn every HTTPS request into a certificate mismatch.

    An install that has opted into private destinations, or a hostname that does
    not resolve, gets the URL back unchanged: there is nothing to pin to.
    """
    ip = guard_destination(base)
    if ip is None:
        return base, {}
    parts = urlsplit(base)
    host = parts.hostname or ""
    literal = f"[{ip}]" if ":" in ip else ip
    authority = f"[{host}]" if ":" in host else host
    if parts.port:
        literal = f"{literal}:{parts.port}"
        authority = f"{authority}:{parts.port}"
    return (urlunsplit((parts.scheme, literal, parts.path, "", "")),
            {"headers": {"Host": authority}, "extensions": {"sni_hostname": host}})


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
