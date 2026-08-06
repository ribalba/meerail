"""IMAP operations against Proton Bridge (or any IMAP server) via IMAPClient."""

from __future__ import annotations

import os
import re
import socket
import ssl
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import imaplib_compat  # noqa: F401  (patches imaplib for imapclient on 3.14+)
from imapclient import IMAPClient, SocketTimeout, tls

import log
from core.config import AccountConfig

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


# TCP keepalive for the IMAP socket. A connection can be black-holed rather than
# closed — the far end vanishes, or a suspend/network change leaves the socket
# bound to a local address that no longer routes. From this side it stays
# ESTABLISHED for good: no FIN, no RST, nothing to read and nothing to fail on.
# Probes give the kernel a reason to declare it dead, which is what turns a
# permanently parked read into an error the retry loop can act on.
_KEEPALIVE_IDLE = 60      # quiet seconds before the first probe
_KEEPALIVE_INTERVAL = 15  # seconds between probes
_KEEPALIVE_COUNT = 4      # unanswered probes before the socket is failed


def _tune_socket(sock: socket.socket, timeout: float | None) -> None:
    """Put the deadline and the keepalive on a socket before it is used.

    The timeout is set here as well as by imapclient because it is the one thing
    that must be true before the first byte moves: a socket that reaches the TLS
    handshake without one hands the handshake a blocking ``read()``, and a
    blocking read on a black-holed connection never returns — not after the
    connect timeout, not ever. That is not theory; it is what wedged an account's
    sync thread for eleven hours while the other four kept syncing.
    """
    sock.settimeout(timeout)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        return
    # TCP_KEEPALIVE is the macOS spelling of Linux's TCP_KEEPIDLE; the other two
    # are named the same on both. Any of them may be missing on a given platform,
    # and a socket with plain SO_KEEPALIVE (two hours to the first probe) is
    # still better than one without, so a rejected option is not fatal here.
    for name, value in (("TCP_KEEPIDLE", _KEEPALIVE_IDLE),
                        ("TCP_KEEPALIVE", _KEEPALIVE_IDLE),
                        ("TCP_KEEPINTVL", _KEEPALIVE_INTERVAL),
                        ("TCP_KEEPCNT", _KEEPALIVE_COUNT)):
        option = getattr(socket, name, None)
        if option is None:
            continue
        try:
            sock.setsockopt(socket.IPPROTO_TCP, option, value)
        except OSError:
            pass


class _TLSConnection(tls.IMAP4_TLS):
    """imapclient's TLS connection, with the socket built by us.

    Only ``open`` differs, and only so that the socket exists — tuned, and known
    to the Bridge — *before* the TLS handshake rather than after it. The handshake
    is the one part of connecting that imapclient does inside a constructor, so
    until the constructor returns there is no client object to abort, and a
    handshake that hangs has nothing watching it. Handing the socket over as soon
    as TCP is up is what gives the watchdog something to shut down.
    """

    def __init__(self, host, port, ssl_context, timeout, bridge: "Bridge"):
        # Before super().__init__, which reaches open() before it returns.
        self._bridge = bridge
        super().__init__(host, port, ssl_context, timeout)

    def open(self, host: str = "", port: int = 993, timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        limit = timeout if timeout is not None else self._timeout
        sock = socket.create_connection((host, port), limit)
        _tune_socket(sock, limit)
        # Handed over before the handshake, which is the only window that
        # matters: wrap_socket *detaches* this socket object, so from the next
        # line until the constructor returns the live descriptor is reachable
        # only through an SSLSocket nobody has a reference to yet. _hold keeps a
        # duplicate of the descriptor itself, which outlives the detach.
        self._bridge._hold(sock)
        self.sock = tls.wrap_socket(sock, self.ssl_context, host)
        # wrap_socket is documented to carry the timeout across, and normally
        # does. Re-asserting it costs nothing and closes the case where it did
        # not: the wrapped socket is the one every later read goes through.
        self.sock.settimeout(limit)
        self._bridge._hold(self.sock)
        self.file = self.sock.makefile("rb")


class _Client(IMAPClient):
    """IMAPClient that builds its TLS connection through ``_TLSConnection``."""

    def __init__(self, *args, bridge: "Bridge", **kwargs):
        self._bridge = bridge
        super().__init__(*args, **kwargs)

    def _create_IMAP4(self):
        if self.ssl and not self.stream:
            return _TLSConnection(self.host, self.port, self.ssl_context,
                                  getattr(self._timeout, "connect", None), self._bridge)
        return super()._create_IMAP4()


# How far past its own socket timeout a blocking IMAP call is allowed to run
# before the watchdog stops believing the timeout exists. Every call is already
# bounded twice over — by the socket read timeout and now by keepalive — so this
# fires only when both have failed, which is why it can afford to be generous:
# a single fetch of whole message bodies is one call that legitimately runs for
# minutes, and aborting a healthy backfill costs a pass.
_STALL_GRACE = 30
_STALL_FACTOR = 15
# How often the watchdog looks. Deadlines are minutes wide; this only decides
# how much longer than its deadline a wedged call is left sitting there.
_WATCHDOG_TICK = 5

_watched: "set[Bridge]" = set()
_watch_lock = threading.Lock()
_watcher: "threading.Thread | None" = None


def _start_watchdog() -> None:
    """Start the one watchdog thread, on the first connection that needs it."""
    global _watcher
    with _watch_lock:
        if _watcher is None:
            _watcher = threading.Thread(target=_watch_forever, name="imap-watchdog",
                                        daemon=True)
            _watcher.start()


def _watch_forever() -> None:
    """Drop the socket under any IMAP call that has outlived its deadline.

    The last line of defence for the agent's one unrecoverable failure mode. A
    sync thread that raises always recovers: run_account_forever catches it,
    records it and retries on a backoff. A sync thread that *blocks* recovers
    from nothing — it holds no session, writes no heartbeat, logs nothing, and
    leaves the account's last_error frozen at whatever happened before the block,
    which reads as a fault that keeps happening rather than one that never ended.

    Shutting the socket down from here turns that silence back into an exception
    on the thread that is stuck, which is all the retry loop ever needed.
    """
    while True:
        time.sleep(_WATCHDOG_TICK)
        now = time.monotonic()
        with _watch_lock:
            overdue = [b for b in _watched if b.overdue(now)]
        for bridge in overdue:
            what, over = bridge.stalled_for(now)
            log.error(f"IMAP {what} has been blocked for {over:.0f}s with no socket "
                      f"timeout in effect — dropping the connection so the pass can "
                      f"fail and be retried", getattr(bridge.acc, "email", None))
            bridge.abort()


class _Ops:
    """Attribute access on the IMAPClient, wrapped in the Bridge's deadline.

    Returned by ``Bridge.ops()``; see the docstring there.
    """

    def __init__(self, bridge: "Bridge"):
        self._bridge = bridge

    def __getattr__(self, name: str):
        bridge = self._bridge
        fn = getattr(bridge.client, name)
        if not callable(fn):
            return fn

        def guarded(*args, **kwargs):
            return bridge._call(name, bridge._command_limit(), fn, *args, **kwargs)

        return guarded


def refused(exc: BaseException) -> bool:
    """Did the server consider this command and say no?

    The distinction is between an answer and a silence. A tagged NO or BAD is
    the server's verdict on the command as sent — "operation not allowed",
    "over quota", "no such mailbox" — and asking again in fifteen minutes puts
    the same command to the same server for the same answer. A dropped socket, a
    read timeout, the watchdog shutting the connection down are not verdicts at
    all: nothing was decided, and the retry is the whole point.

    IMAPClient draws the same line with imaplib's own two classes, which is why
    this can be a type check rather than a search through the error text:
    ``Error`` for a command the server refused, its ``AbortError`` subclass for a
    conversation that broke. Anything else — OSError, socket.timeout — never got
    an answer either.
    """
    return isinstance(exc, IMAPClient.Error) and not isinstance(exc, IMAPClient.AbortError)


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
        # A duplicate handle on the live socket, held from the moment TCP is up
        # so that abort() has something to shut down even while the TLS
        # handshake is still running. See _hold.
        self._sock: socket.socket | None = None
        # What the watchdog reads: the call in flight and when it must be done
        # by, on the monotonic clock. None means nothing is blocking.
        self._what = ""
        self._deadline: float | None = None

    # ------------------------------------------------------------------ #
    # Watchdog plumbing
    # ------------------------------------------------------------------ #

    def _call(self, what: str, limit: float, fn, *args, **kwargs):
        """Run one blocking IMAP call with a deadline the watchdog enforces.

        Everything that touches the socket goes through here, so there is no
        call left that can park the sync thread for good. The deadline is
        cleared on the way out however the call ended — a raised call has
        already handed control back to the retry loop, which is the outcome the
        watchdog exists to produce.

        One deep: a nested call would clear its parent's deadline on the way
        out, so the callers below wrap the IMAP command itself and never each
        other.
        """
        self._what = what
        self._deadline = time.monotonic() + limit
        try:
            return fn(*args, **kwargs)
        finally:
            self._deadline = None

    def _command_limit(self) -> float:
        """For the calls whose answer is as large as the mailbox: FETCH, LIST,
        EXPUNGE. A chunk of whole message bodies off a slow server is one call
        that legitimately runs for minutes, so this has to be loose enough that
        a healthy backfill is never the thing that trips it."""
        return self.acc.imap_read_timeout * _STALL_FACTOR + _STALL_GRACE

    def _roundtrip_limit(self) -> float:
        """For the calls that are one request and one short answer: LOGIN,
        SELECT, SEARCH, LOGOUT, the IDLE handshake. Nothing here streams, so
        anything past a couple of read timeouts is a stall, and there is no
        reason to leave the account waiting a quarter of an hour to find out."""
        return self.acc.imap_read_timeout * 2 + _STALL_GRACE

    def overdue(self, now: float) -> bool:
        deadline = self._deadline
        return deadline is not None and now > deadline

    def stalled_for(self, now: float) -> tuple[str, float]:
        """The blocked call and how far past its deadline it is, for the log."""
        deadline = self._deadline
        return self._what, (now - deadline if deadline is not None else 0.0)

    def ops(self) -> "_Ops":
        """The raw IMAPClient, with every command under the same deadline.

        For the write-back path, which drives a dozen commands (COPY, EXPUNGE,
        CREATE, SUBSCRIBE…) that have no other reason to exist as methods here.
        Going through this rather than ``bridge.client`` is what keeps them from
        being the one way left to park the sync thread for good.
        """
        return _Ops(self)

    # ------------------------------------------------------------------ #

    def connect(self) -> None:
        acc = self.acc
        ctx = _ssl_context(acc.verify_cert)
        use_ssl = acc.imap_security == "ssl"
        # The timeout is the difference between a stalled Bridge surfacing as a
        # logged, recorded, retried error and it hanging the sync thread
        # silently forever. See AccountConfig.imap_read_timeout.
        timeout = SocketTimeout(connect=acc.imap_connect_timeout, read=acc.imap_read_timeout)
        # Registered before the first byte, not after login: connecting is
        # exactly where a socket has no client object behind it yet, and so is
        # the one phase that used to be watched by nothing at all.
        _start_watchdog()
        with _watch_lock:
            _watched.add(self)
        # Connect, the TLS handshake and the server greeting all happen inside
        # the constructor; login is one round trip after it.
        connect_limit = acc.imap_connect_timeout + acc.imap_read_timeout + _STALL_GRACE
        try:
            self.client = self._call(
                "connect", connect_limit, _Client,
                acc.imap_host, port=acc.imap_port, ssl=use_ssl,
                ssl_context=ctx if use_ssl else None, use_uid=True,
                timeout=timeout, bridge=self,
            )
            if not use_ssl:
                # The plain and STARTTLS paths build their socket inside
                # imapclient, so they are tuned once it exists rather than
                # before. The connect timeout has already done its job by then;
                # keepalive has not, and it is what kills a connection that goes
                # black-holed mid-fetch.
                _tune_socket(self.client.socket(), acc.imap_read_timeout)
                self._hold(self.client.socket())
            if acc.imap_security == "starttls":
                self._call("starttls", connect_limit, self.client.starttls, ctx)
                _tune_socket(self.client.socket(), acc.imap_read_timeout)
                self._hold(self.client.socket())
            self._call("login", self._roundtrip_limit(),
                       self.client.login, acc.username or acc.email, acc.password)
        except BaseException:
            self._unwatch()
            raise

    def _hold(self, sock: socket.socket) -> None:
        """Keep a duplicate descriptor for the connection currently being used.

        A duplicate rather than the socket object itself, because the objects
        this is handed do not survive: ``wrap_socket`` detaches the socket it is
        given, and imaplib owns the one it made. ``dup`` refers to the same
        kernel socket, so ``shutdown`` through it reaches the read that is
        blocked on the original — which is the entire mechanism the watchdog
        has, and it must work while an object is still half-built.
        """
        try:
            held = socket.socket(fileno=os.dup(sock.fileno()))
        except OSError:
            return
        self._release()
        self._sock = held

    def _release(self) -> None:
        """Close the duplicate. Never the connection — that is the point of it
        being a duplicate — so this is safe on any path out."""
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _unwatch(self) -> None:
        with _watch_lock:
            _watched.discard(self)
        self._deadline = None

    def logout(self) -> None:
        if self.client:
            try:
                self._call("logout", self._roundtrip_limit(), self.client.logout)
            except Exception:
                pass
            self.client = None
        self._release()
        self._unwatch()

    def abort(self) -> None:
        """Drop the connection without the LOGOUT handshake.

        Use this when the socket is presumed dead (e.g. after a host suspend): a
        clean logout() would send BYE and wait for a reply that never comes,
        blocking until imap_read_timeout. shutdown() just closes the socket.

        Also the watchdog's only lever, so it has to be safe to call from
        another thread while this Bridge's owner is blocked inside a read. The
        raw socket is shut down first and separately: shutdown(SHUT_RDWR) is
        what makes that read return, and it is reachable even when the failure
        happened before ``client`` was ever assigned.
        """
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._release()
        if self.client:
            try:
                self.client.shutdown()
            except Exception:
                pass
            self.client = None
        self._unwatch()

    def _list(self):
        return self._call("list", self._command_limit(), self.client.list_folders)

    def list_folders(self) -> list[dict]:
        out = []
        for flags, _delim, name in self._list():
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
        for flags, delim, name in self._list():
            lower = {f.lower() if isinstance(f, bytes) else str(f).lower().encode() for f in flags}
            if b"\\noselect" in lower and name.lower() == "folders":
                sep = delim.decode() if isinstance(delim, bytes) else (delim or "/")
                return name + sep
        return ""

    def select(self, name: str) -> tuple[int | None, int | None]:
        info = self._call("select", self._roundtrip_limit(),
                          self.client.select_folder, name, readonly=True)
        uidvalidity = info.get(b"UIDVALIDITY")
        uidnext = info.get(b"UIDNEXT")
        self._exists = int(info.get(b"EXISTS", 0) or 0)
        return (int(uidvalidity) if uidvalidity else None,
                int(uidnext) if uidnext else None)

    @property
    def exists(self) -> int:
        """Messages the last SELECT said this folder holds.

        The server's own count, from the same command that opened the folder —
        which makes it the one thing a SEARCH answer can be checked against
        before anything is deleted on the strength of it. See sync._reconcile.
        """
        return self._exists

    def new_uids(self, last_uid: int) -> list[int]:
        # `UID n:*` has no defined answer on a folder with no messages — there is
        # no highest UID for `*` to mean. Bridge replies "SEARCH failed: no such
        # message" rather than an empty set, which would fail the whole sync pass
        # for every empty folder, so don't ask the question.
        if not self._exists:
            return []
        uids = self._call("search", self._roundtrip_limit(),
                          self.client.search, [u"UID", f"{last_uid + 1}:*"])
        return sorted(u for u in uids if u > last_uid)

    def all_uids(self) -> list[int]:
        return sorted(self._call("search", self._roundtrip_limit(),
                                 self.client.search, ["ALL"]))

    def fetch_headers(self, uids: list[int]) -> dict[int, dict]:
        """Cheap pass: FLAGS, size, and the four headers that identify a message
        — no body.

        The date is here because the content window is decided before the body is
        ever asked for, and this pass already runs over every new UID: a few more
        header fields cost nothing next to a second round trip. RFC822.SIZE comes
        along for the same reason — for a message we only take the headers of, it
        is the only place the real size can come from.

        From and Subject are here because this pass is also where the agent
        decides *not* to fetch a message: one whose Message-ID it already holds
        is taken to be that message and never downloaded. A Message-ID is written
        by the sender, and two different messages can carry the same one — so the
        decision needs the same evidence the full ingest would use, and this is
        the only chance to gather it without paying for the body. The raw block
        is handed on rather than picked apart here: what those fields *mean* is
        core.mail.parse's business, and both sides of the comparison have to read
        them the same way.
        """
        resp = self._fetch(uids, [b"FLAGS", b"INTERNALDATE", b"RFC822.SIZE",
                                  b"BODY.PEEK[HEADER.FIELDS "
                                  b"(MESSAGE-ID DATE FROM SUBJECT)]"])
        out = {}
        for uid, data in resp.items():
            hdr = _body_bytes(data)
            m = _MSGID_RE.search(hdr or b"")
            message_id = m.group(0)[1:-1].decode(errors="replace") if m else None
            out[uid] = {
                "message_id": message_id,
                "flags": flags_to_dict(data.get(b"FLAGS", ())),
                "date": _sent_date(hdr, data.get(b"INTERNALDATE")),
                # When this server took delivery, which unlike the Date header
                # is not written by the sender. Everything about *retention*
                # reads this one: a message dated 1998 is displayed and sorted
                # as 1998, and is a message that arrived today.
                "received": _to_naive_utc(data.get(b"INTERNALDATE")),
                "size": int(data.get(b"RFC822.SIZE") or 0),
                "headers": hdr,
            }
        return out

    def fetch_header_block(self, uids: list[int]) -> dict[int, dict]:
        """Every header, still no body — what mail outside the window gets.

        BODY.PEEK[HEADER] is a few KB against a message that may be megabytes,
        which is the entire point of the window: the row that lands lists,
        threads and searches by subject and correspondent without the body
        having crossed the wire at all.
        """
        resp = self._fetch(uids, [b"FLAGS", b"BODY.PEEK[HEADER]"])
        return {
            uid: {"raw": _body_bytes(data), "flags": flags_to_dict(data.get(b"FLAGS", ()))}
            for uid, data in resp.items()
        }

    def _fetch(self, uids: list[int], what: list[bytes]) -> dict[int, dict]:
        """Every FETCH, under the watchdog. The deadline is the loosest of any
        call the agent makes: this is the one that legitimately runs for minutes
        against a slow server holding a chunk of whole message bodies."""
        return self._call("fetch", self._command_limit(), self.client.fetch, uids, what)

    def fetch_flags(self, uids: list[int]) -> dict[int, dict]:
        """The reconcile sweep's pass: FLAGS, and the size the server claims.

        RFC822.SIZE rides along because it is the one cheap way to notice that
        what we stored is not what the server holds — the sweep already walks
        every UID in the folder, and one more data item on a FETCH that was
        being sent anyway costs nothing next to the round trip.
        """
        resp = self._fetch(uids, [b"FLAGS", b"RFC822.SIZE"])
        return {
            uid: {"flags": flags_to_dict(data.get(b"FLAGS", ())),
                  "size": int(data.get(b"RFC822.SIZE") or 0)}
            for uid, data in resp.items()
        }

    def fetch_raw(self, uids: list[int]) -> dict[int, dict]:
        resp = self._fetch(uids, [b"FLAGS", b"BODY.PEEK[]"])
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
        self._call("idle", self._roundtrip_limit(), self.client.idle)
        if wake is None:
            try:
                return bool(self._idle_check(seconds))
            finally:
                self._call("idle done", self._roundtrip_limit(), self.client.idle_done)
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
                changed = self._idle_check(min(_IDLE_SLICE, remaining))
                if time.time() - before >= _SUSPEND_GAP:
                    # Frozen mid-slice: the connection did not survive the sleep.
                    # Leave IDLE hanging and let the caller abort() the socket.
                    clean = False
                    raise Suspended
                if changed or wake.is_set():
                    return True
        finally:
            if clean:
                self._call("idle done", self._roundtrip_limit(), self.client.idle_done)

    def _idle_check(self, seconds: float):
        """One IDLE slice, watched. Unlike every other call this one is *meant*
        to block, so its deadline is the slice it asked for rather than the
        command limit — a slice that outstays that is not waiting, it is stuck."""
        return self._call("idle check", seconds + _STALL_GRACE,
                          self.client.idle_check, timeout=seconds)
