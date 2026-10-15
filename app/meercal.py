"""Client for meercal's calendar import API.

meercal is the self-hosted calendar that sits beside meerail. Mail often
carries a calendar event (a meeting invitation, a booking confirmation, a
ticket), and "Add to meercal" hands that .ics file to meercal's own import
route, the one behind its "Import a calendar file" dialog. meercal parses it,
files it into the calendar the user picked, and pushes it on to a CalDAV
server when that calendar has one. Nothing about iCalendar is parsed here.
meercal already does that properly, and a second parser in this repository
would only disagree with it about time zones.

Three meercal routes are used:

  * ``GET /api/state`` for the list of calendars. There is no narrower route
    that lists them, and the accounts in the same answer are what name the
    group a calendar belongs to.
  * ``POST /api/import/preview``, which reads the file and writes nothing.
  * ``POST /api/import`` with a ``calendar_id``. It upserts by UID within that
    calendar, so importing the same invitation twice updates the event rather
    than doubling it. That is also why a timeout here is not the hazard it is
    for Meerato: trying again is safe.

meercal authenticates a script with ``Authorization: Bearer <its UI
password>``. It has no separate API token. An install with no password is
open, and then no header is sent. A password-protected meercal also refuses
anything but HTTPS or loopback, and says so in its 403. That answer is passed
on as it is, because it already tells the person what to change.

Kept free of FastAPI so it can be unit-tested with ``core``'s deps alone, the
same as app/meerato.py.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

import httpx

from core.config import get_settings
from . import nethost

# meercal is a peer service, usually on the same machine. An import is one
# small file, so there is no reason to wait longer than for any other request.
_TIMEOUT = httpx.Timeout(15.0)

# meercal refuses an import file over 12 MB (MAX_BYTES in its
# app/routers/imports.py). Checked here as well so a mislabelled 30 MB
# attachment called "something.ics" is refused before it is uploaded, not
# after.
MAX_ICS_BYTES = 12 * 1024 * 1024

# What a saved password shows instead of itself. Sent back unchanged it means
# "keep the one you have", so the address can be corrected without typing the
# password again. The same convention as Meerato's TOKEN_MASK.
PASSWORD_MASK = "•" * 8

# A calendar colour is drawn by the page. meercal only stores #rrggbb, but the
# value crosses from another program into our markup, so anything else is
# dropped rather than trusted.
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")


class MeercalError(Exception):
    """meercal refused, or could not be reached."""


class BlockedHost(MeercalError, nethost.BlockedHost):
    """This address points somewhere the server will not fetch from.

    The reasoning is Meerato's (see app/meerato.py::BlockedHost). The address is
    typed into Settings and requested by the server, so without the check it
    would be a way to aim this machine at its own network. The difference is
    only in how often the switch gets turned on. meercal nearly always runs on
    the same host or Docker network as meerail, which is why it has a setting
    of its own, ``server.meercal_allow_private_hosts``.
    """


class Refused(MeercalError):
    """meercal answered and said no to the request itself.

    Not a calendar file, no events in it, a read-only calendar, a file too
    large. These are 4xx answers about what was asked, not about reaching
    meercal, so the router reports them as a refused operation (400) rather
    than as a failed upstream (502). ``status`` is meercal's own code.
    """

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class NoAnswer(MeercalError):
    """The request went out and nothing came back in time."""


def parse_base(raw: str) -> str:
    """meercal's address as typed in Settings, reduced to the base URL.

    Accepts what people paste: the address bar of an open meercal, with or
    without a trailing slash or a ``#`` view in it. A sub-path mount stays,
    since everything else is built off this. Raises ValueError with a sentence
    meant for the settings field.
    """
    parsed = urlsplit((raw or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Enter meercal's address as a full http(s) URL, for example "
                         "http://meercal.example:8010")
    if parsed.username or parsed.password:
        # It would work, and it would also put the password in a column that
        # is stored in the clear and shown back on the page. It has a field.
        raise ValueError("Put meercal's password in the password field, not in the address")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


# The check itself is shared with Meerato and the LLM client (app/nethost.py).
# These two wrappers supply meercal's half of it, which is the setting that
# allows private destinations and meercal's own BlockedHost, so a caller can
# catch MeercalError and get everything this module can fail with.
_HINT = "server.meercal_allow_private_hosts"


def _allow_private() -> bool:
    return get_settings().meercal_allow_private_hosts


def pinned(base: str) -> tuple[str, dict]:
    """The URL to request and the arguments that keep it on the checked address.
    See nethost.pinned."""
    try:
        return nethost.pinned(base, allow_private=_allow_private(), hint=_HINT)
    except nethost.BlockedHost as exc:
        raise BlockedHost(str(exc)) from exc


def _detail(res: httpx.Response) -> str:
    """meercal's own explanation, which is FastAPI's ``{"detail": ...}``."""
    try:
        detail = res.json().get("detail")
    except Exception:
        detail = None
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    return res.reason_phrase or f"HTTP {res.status_code}"


def _translate(exc: Exception, base: str, *, importing: bool = False) -> MeercalError:
    """meercal's failure, said in our voice."""
    if isinstance(exc, MeercalError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        if importing:
            return NoAnswer("meercal did not answer in time. The event may have been added "
                            "anyway. Adding it again is safe, because meercal updates an event "
                            "it already has instead of adding a second one.")
        return NoAnswer("meercal did not answer in time.")
    if isinstance(exc, httpx.HTTPStatusError):
        res = exc.response
        code = res.status_code
        if 300 <= code < 400:
            # The usual cause is a proxy sending http:// on to https://. It is
            # not followed, because a redirect is a second destination that the
            # host check never saw. Saying where it pointed is the fix.
            where = res.headers.get("location", "")
            return MeercalError(f"meercal answered with a redirect to {where or 'another address'}. "
                                "Save that address in Settings instead.")
        if code == 401:
            if exc.request.headers.get("authorization"):
                return MeercalError("meercal did not accept the password saved in Settings.")
            return MeercalError("meercal is password-protected. Add its password in Settings.")
        detail = _detail(res)
        # Some of meercal's sentences name it already ("meercal is
        # password-protected, so ..."), and saying it twice reads as a glitch.
        said = detail if detail.lower().startswith("meercal") else f"meercal: {detail}"
        if 400 <= code < 500:
            return Refused(said, code)
        return MeercalError(said)
    if isinstance(exc, ValueError):
        # A 200 that is not JSON. Something answered at that address, and it
        # was not meercal's API.
        return MeercalError(f"{base} answered, but not the way meercal does. Check the address.")
    return MeercalError(f"Could not reach meercal at {base}.")


def _call(method: str, base: str, path: str, password: str, *,
          files: dict | None = None, data: dict | None = None, importing: bool = False):
    """One request to meercal, pinned to the address the host check approved."""
    target, pin = pinned(base)
    # The pin carries a Host header of its own when it rewrote the URL to an
    # address, so the password is added to that dict rather than replacing it.
    headers = dict(pin.get("headers") or {})
    if password:
        headers["Authorization"] = f"Bearer {password}"
    extra = {k: v for k, v in pin.items() if k != "headers"}
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=False) as client:
            res = client.request(method, f"{target}{path}", headers=headers,
                                 files=files, data=data, **extra)
            res.raise_for_status()
            return res.json()
    except Exception as exc:
        raise _translate(exc, base, importing=importing) from exc


def writable_calendars(state: dict) -> list[dict]:
    """The calendars an event can be added to, from meercal's ``/api/state``.

    Read-only ones are left out (an ``ics`` subscription, a shared calendar
    without write access), because meercal refuses an import into them and a
    choice that can only fail is not a choice. Each keeps the label of the
    account it belongs to, which is how meercal's own sidebar groups them and
    the only way to tell two calendars both called "Personal" apart.
    """
    accounts = {
        a.get("id"): a for a in (state.get("accounts") or []) if isinstance(a, dict)
    }
    out = []
    for cal in state.get("calendars") or []:
        if not isinstance(cal, dict) or cal.get("read_only"):
            continue
        try:
            cid = int(cal.get("id"))
        except (TypeError, ValueError):
            continue
        color = str(cal.get("color") or "")
        account = accounts.get(cal.get("account_id")) or {}
        out.append({
            "id": cid,
            "name": str(cal.get("name") or cal.get("server_name") or f"Calendar {cid}"),
            "color": color if _COLOR_RE.match(color) else "",
            "account": str(account.get("label") or ""),
        })
    return out


def fetch_calendars(base: str, password: str) -> list[dict]:
    """meercal's writable calendars. Also how a saved address is probed."""
    try:
        state = _call("GET", base, "/api/state", password)
    except Refused as exc:
        if exc.status == 404:
            raise MeercalError(f"{base} has no meercal API at /api/state. "
                               "Check the address.") from exc
        raise
    if not isinstance(state, dict) or not isinstance(state.get("calendars"), list):
        raise MeercalError(f"{base} answered, but not the way meercal does. Check the address.")
    return writable_calendars(state)


# What the dialog shows of meercal's preview. Named rather than passed through
# whole, so a field meercal adds later does not become part of this API by
# accident.
_PREVIEW_KEYS = ("events", "recurring", "all_day", "first", "last", "titles",
                 "outside_horizon", "horizon")


def preview(base: str, password: str, filename: str, ics: bytes) -> dict:
    """What meercal reads in the file, without writing anything."""
    res = _call("POST", base, "/api/import/preview", password,
                files={"file": (filename, ics, "text/calendar")})
    if not isinstance(res, dict):
        raise MeercalError(f"{base} answered, but not the way meercal does. Check the address.")
    return {k: res.get(k) for k in _PREVIEW_KEYS}


def import_ics(base: str, password: str, calendar_id: int, filename: str, ics: bytes) -> dict:
    """Import the file into one calendar. Returns meercal's
    ``{calendar, created, updated, queued}``."""
    try:
        res = _call("POST", base, "/api/import", password,
                    files={"file": (filename, ics, "text/calendar")},
                    data={"calendar_id": str(calendar_id)}, importing=True)
    except Refused as exc:
        if exc.status == 404:
            # The calendar was there when the dialog listed it. meercal answers
            # a bare "Not found" for it, which does not say what went missing.
            raise Refused("That calendar is not in meercal any more. "
                          "Open the dialog again to pick another one.", 404) from exc
        raise
    if not isinstance(res, dict):
        raise MeercalError(f"{base} answered, but not the way meercal does. Check the address.")
    return res
