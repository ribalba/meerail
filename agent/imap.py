"""IMAP operations against Proton Bridge (or any IMAP server) via IMAPClient."""

from __future__ import annotations

import re
import ssl
import threading  # noqa: F401  (type annotation on Bridge.idle_wait)
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import imaplib_compat  # noqa: F401  (patches imaplib for imapclient on 3.14+)
from imapclient import IMAPClient, SocketTimeout

from config import AccountConfig

_MSGID_RE = re.compile(rb"<[^>]+>")
# The Date line out of a header block. Unfolded values only: a Date split across
# lines is malformed, and _sent_date has INTERNALDATE to fall back on.
_DATE_RE = re.compile(rb"^Date:[ \t]*(.+?)\r?$", re.IGNORECASE | re.MULTILINE)

# How long a single interruptible IDLE poll blocks before checking for a wake
# request. Short enough that refresh feels immediate, long enough that an idle
# agent isn't spinning.
_IDLE_SLICE = 5

# If a single IDLE slice takes this much wall-clock time or more, the host was
# almost certainly suspended mid-wait (laptop lid closed). A slice is only meant
# to block for _IDLE_SLICE seconds; anything near this threshold means the clock
# jumped while the thread was frozen. See Bridge.idle_wait / Suspended.
_SUSPEND_GAP = 15


class Suspended(Exception):
    """Raised out of idle_wait when the host slept through the IDLE wait.

    Not an error: the wait itself completed. It signals that the connection is
    presumed dead (the TCP session went stale while the machine was suspended)
    and the caller should reconnect and re-sync immediately rather than trust
    the socket or back off. Mail may have arrived while asleep, so the sooner
    the reconnect the better."""

# IMAP SPECIAL-USE flags we care about for role hints.
_SPECIAL = {b"\\sent", b"\\drafts", b"\\junk", b"\\trash", b"\\archive", b"\\all", b"\\flagged"}


def _ssl_context(verify: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def flags_to_dict(flags: tuple) -> dict:
    known = {b"\\seen": "seen", b"\\flagged": "flagged", b"\\answered": "answered",
             b"\\draft": "draft", b"\\deleted": "deleted"}
    out = {"seen": False, "flagged": False, "answered": False, "draft": False, "deleted": False}
    keywords: list[str] = []
    for f in flags or ():
        key = f.lower() if isinstance(f, bytes) else str(f).lower().encode()
        if key in known:
            out[known[key]] = True
        else:
            keywords.append(f.decode() if isinstance(f, bytes) else str(f))
    out["keywords"] = keywords
    return out


def _sent_date(hdr: bytes, internal) -> "datetime | None":
    """When a message was sent, as naive UTC, from the cheap header pass.

    The Date header first, because that is what the rest of meerail sorts and
    filters on (Message.date_sent), and a window decided on a different clock
    than the one the reader sees would strip content off mail that still looks
    in-window in the list. INTERNALDATE is the fallback for the mail that has no
    parseable Date at all — better a server timestamp than no age to judge by.
    """
    m = _DATE_RE.search(hdr or b"")
    if m:
        try:
            return _to_naive_utc(parsedate_to_datetime(m.group(1).decode(errors="replace").strip()))
        except (TypeError, ValueError, IndexError):
            pass
    return _to_naive_utc(internal) if isinstance(internal, datetime) else None


def _to_naive_utc(dt: "datetime | None") -> "datetime | None":
    if dt is not None and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _body_bytes(data: dict) -> bytes:
    for key, val in data.items():
        if isinstance(key, bytes) and key.startswith(b"BODY[") and val:
            return val
    return b""


class Bridge:
    def __init__(self, account: AccountConfig):
        self.acc = account
        self.client: IMAPClient | None = None
        # Message count of the currently-selected folder; see new_uids.
        self._exists = 0

    def connect(self) -> None:
        acc = self.acc
        ctx = _ssl_context(acc.verify_cert)
        use_ssl = acc.imap_security == "ssl"
        # The timeout is the difference between a stalled Bridge surfacing as a
        # logged, recorded, retried error and it hanging the sync thread
        # silently forever. See AccountConfig.imap_read_timeout.
        timeout = SocketTimeout(connect=acc.imap_connect_timeout, read=acc.imap_read_timeout)
        self.client = IMAPClient(acc.imap_host, port=acc.imap_port, ssl=use_ssl,
                                 ssl_context=ctx if use_ssl else None, use_uid=True,
                                 timeout=timeout)
        if acc.imap_security == "starttls":
            self.client.starttls(ctx)
        self.client.login(acc.username or acc.email, acc.password)

    def logout(self) -> None:
        if self.client:
            try:
                self.client.logout()
            except Exception:
                pass
            self.client = None

    def abort(self) -> None:
        """Drop the connection without the LOGOUT handshake.

        Use this when the socket is presumed dead (e.g. after a host suspend): a
        clean logout() would send BYE and wait for a reply that never comes,
        blocking until imap_read_timeout. shutdown() just closes the socket.
        """
        if self.client:
            try:
                self.client.shutdown()
            except Exception:
                pass
            self.client = None

    def list_folders(self) -> list[dict]:
        out = []
        for flags, _delim, name in self.client.list_folders():
            lower = {f.lower() if isinstance(f, bytes) else str(f).lower().encode() for f in flags}
            if b"\\noselect" in lower:
                continue
            hint = next((f.decode() for f in flags
                         if (f.lower() if isinstance(f, bytes) else b"") in _SPECIAL), "")
            out.append({"name": name, "role_hint": hint})
        return out

    def user_folder_parent(self) -> str:
        """Prefix new user folders must be created under; "" means the root.

        Proton Bridge refuses CREATE at the IMAP root ("invalid mailbox name
        […]: operation not allowed"): user folders belong under a \\Noselect
        "Folders" node, labels under "Labels". Those parents are exactly the
        ones list_folders() drops, so this reads the raw LIST instead. A plain
        IMAP server has no such node and takes the bare name, which is what the
        empty return gives.
        """
        for flags, delim, name in self.client.list_folders():
            lower = {f.lower() if isinstance(f, bytes) else str(f).lower().encode() for f in flags}
            if b"\\noselect" in lower and name.lower() == "folders":
                sep = delim.decode() if isinstance(delim, bytes) else (delim or "/")
                return name + sep
        return ""

    def select(self, name: str) -> tuple[int | None, int | None]:
        info = self.client.select_folder(name, readonly=True)
        uidvalidity = info.get(b"UIDVALIDITY")
        uidnext = info.get(b"UIDNEXT")
        self._exists = int(info.get(b"EXISTS", 0) or 0)
        return (int(uidvalidity) if uidvalidity else None,
                int(uidnext) if uidnext else None)

    def new_uids(self, last_uid: int) -> list[int]:
        # `UID n:*` has no defined answer on a folder with no messages — there is
        # no highest UID for `*` to mean. Bridge replies "SEARCH failed: no such
        # message" rather than an empty set, which would fail the whole sync pass
        # for every empty folder, so don't ask the question.
        if not self._exists:
            return []
        uids = self.client.search([u"UID", f"{last_uid + 1}:*"])
        return sorted(u for u in uids if u > last_uid)

    def all_uids(self) -> list[int]:
        return sorted(self.client.search(["ALL"]))

    def fetch_headers(self, uids: list[int]) -> dict[int, dict]:
        """Cheap pass: FLAGS, Message-ID, Date and size — no body.

        The date is here because the content window is decided before the body
        is ever asked for, and this pass already runs over every new UID: two
        more header fields cost nothing next to a second round trip. RFC822.SIZE
        comes along for the same reason — for a message we only take the headers
        of, it is the only place the real size can come from.
        """
        resp = self.client.fetch(
            uids,
            [b"FLAGS", b"INTERNALDATE", b"RFC822.SIZE",
             b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID DATE)]"],
        )
        out = {}
        for uid, data in resp.items():
            hdr = _body_bytes(data)
            m = _MSGID_RE.search(hdr or b"")
            message_id = m.group(0)[1:-1].decode(errors="replace") if m else None
            out[uid] = {
                "message_id": message_id,
                "flags": flags_to_dict(data.get(b"FLAGS", ())),
                "date": _sent_date(hdr, data.get(b"INTERNALDATE")),
                "size": int(data.get(b"RFC822.SIZE") or 0),
            }
        return out

    def fetch_header_block(self, uids: list[int]) -> dict[int, dict]:
        """Every header, still no body — what mail outside the window gets.

        BODY.PEEK[HEADER] is a few KB against a message that may be megabytes,
        which is the entire point of the window: the row that lands lists,
        threads and searches by subject and correspondent without the body
        having crossed the wire at all.
        """
        resp = self.client.fetch(uids, [b"FLAGS", b"BODY.PEEK[HEADER]"])
        return {
            uid: {"raw": _body_bytes(data), "flags": flags_to_dict(data.get(b"FLAGS", ()))}
            for uid, data in resp.items()
        }

    def fetch_flags(self, uids: list[int]) -> dict[int, dict]:
        resp = self.client.fetch(uids, [b"FLAGS"])
        return {uid: flags_to_dict(data.get(b"FLAGS", ())) for uid, data in resp.items()}

    def fetch_raw(self, uids: list[int]) -> dict[int, dict]:
        resp = self.client.fetch(uids, [b"FLAGS", b"BODY.PEEK[]"])
        out = {}
        for uid, data in resp.items():
            out[uid] = {"raw": _body_bytes(data), "flags": flags_to_dict(data.get(b"FLAGS", ()))}
        return out

    def idle_wait(self, seconds: int, wake: "threading.Event | None" = None) -> bool:
        """IDLE on the currently-selected folder; return True if something changed.

        With a ``wake`` event, the wait is served in short slices and checked
        between them, so a refresh requested from the UI is picked up within a
        few seconds instead of after the full ``poll_interval``. The event stays
        set for the caller to see and clear.

        Raises ``Suspended`` if a slice is seen to take far longer in wall-clock
        time than it asked for: the host slept mid-wait and the socket is stale.
        In that case DONE is deliberately *not* sent — it would block on the dead
        socket until the read timeout — and the caller reconnects instead.
        """
        self.client.idle()
        if wake is None:
            try:
                return bool(self.client.idle_check(timeout=seconds))
            finally:
                self.client.idle_done()
        # Drive the wait off the monotonic clock rather than counting fixed-size
        # slices: it stays accurate if a slice returns early, and pairs with the
        # wall-clock reading below to tell a normal slice apart from a suspend.
        deadline = time.monotonic() + seconds
        clean = True
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                before = time.time()
                changed = self.client.idle_check(timeout=min(_IDLE_SLICE, remaining))
                if time.time() - before >= _SUSPEND_GAP:
                    # Frozen mid-slice: the connection did not survive the sleep.
                    # Leave IDLE hanging and let the caller abort() the socket.
                    clean = False
                    raise Suspended
                if changed or wake.is_set():
                    return True
        finally:
            if clean:
                self.client.idle_done()
