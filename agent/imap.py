"""IMAP operations against Proton Bridge (or any IMAP server) via IMAPClient."""

from __future__ import annotations

import re
import ssl
import threading  # noqa: F401  (type annotation on Bridge.idle_wait)

import imaplib_compat  # noqa: F401  (patches imaplib for imapclient on 3.14+)
from imapclient import IMAPClient, SocketTimeout

from config import AccountConfig

_MSGID_RE = re.compile(rb"<[^>]+>")

# How long a single interruptible IDLE poll blocks before checking for a wake
# request. Short enough that refresh feels immediate, long enough that an idle
# agent isn't spinning.
_IDLE_SLICE = 5

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
        """Cheap pass: FLAGS + Message-ID header only (no body)."""
        resp = self.client.fetch(uids, [b"FLAGS", b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"])
        out = {}
        for uid, data in resp.items():
            hdr = _body_bytes(data)
            m = _MSGID_RE.search(hdr or b"")
            message_id = m.group(0)[1:-1].decode(errors="replace") if m else None
            out[uid] = {"message_id": message_id, "flags": flags_to_dict(data.get(b"FLAGS", ()))}
        return out

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
        """
        self.client.idle()
        try:
            if wake is None:
                return bool(self.client.idle_check(timeout=seconds))
            remaining = seconds
            while remaining > 0:
                if self.client.idle_check(timeout=min(_IDLE_SLICE, remaining)):
                    return True
                if wake.is_set():
                    return True
                remaining -= _IDLE_SLICE
            return False
        finally:
            self.client.idle_done()
