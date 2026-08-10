"""Where the server is allowed to send a request it was told about by the UI.

Two integrations take a URL typed into Settings and then fetch it *from the
server*: the Meerato task tracker (app/meerato.py) and the LLM provider
(app/llm.py). The server sits on a network the browser cannot see — the compose
network with the database and Tika on it, the host's own loopback, a cloud
provider's metadata address on 169.254.169.254 — so an unrestricted URL field is
a request-forger. Both need the same check, and a second copy of it is a second
place for it to go subtly wrong, so it lives here once.

What differs between the two is only the escape hatch: each caller passes the
setting that says "this install really does keep that service on the LAN"
(``server.meerato_allow_private_hosts``, ``server.llm_allow_private_hosts``) and
the sentence to print when it is off. Nothing here reads configuration itself.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit


class BlockedHost(Exception):
    """This URL points somewhere the server will not fetch from.

    Raised for a destination that resolves onto a private, loopback, link-local,
    reserved or multicast address, and — just as importantly — for one that
    cannot be resolved at all. See ``guard_destination``.
    """


def reachable(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
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


def guard_destination(base: str, *, allow_private: bool, hint: str) -> str | None:
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
    ``pinned``, which is what every guarded request goes through).

    ``hint`` names the setting that turns this off, so the refusal points at the
    one thing that would make the destination allowed.
    """
    if allow_private:
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
        if not reachable(ip):
            raise BlockedHost(
                f"{host} resolves to {ip}, which is a private or local address. "
                f"meerail will not fetch from those unless the install says it may "
                f"({hint}).")
        allowed.append(str(ip))
    return allowed[0] if allowed else None


def pinned(base: str, *, allow_private: bool, hint: str) -> tuple[str, dict]:
    """The base URL to actually request, plus the per-request arguments that keep
    it pointed at the checked address.

    The URL's host is replaced by the address ``guard_destination`` approved, so
    no second name resolution stands between the check and the connection. Two
    things travel with it to keep that from breaking an ordinary server:

      * ``Host``, so a server behind name-based virtual hosting still knows which
        site is being asked for; and
      * ``sni_hostname``, which is both the name offered in the TLS handshake and
        the name the certificate is verified against — without it, pinning would
        turn every HTTPS request into a certificate mismatch.

    An install that has opted into private destinations gets the URL back
    unchanged: there is nothing to pin to.
    """
    ip = guard_destination(base, allow_private=allow_private, hint=hint)
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
