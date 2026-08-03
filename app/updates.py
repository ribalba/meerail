"""Is a newer meerail out? — the one outbound call the server ever makes.

It fetches the ``VERSION`` file from the repository's default branch and
compares it with the version this process is running (core/version.py). That
file is the release itself: CI tags the pushed images with exactly what it
contains, so "what main says" and "what `docker pull` would get you" cannot
drift apart the way a hand-maintained latest.json does.

Three properties matter more than the feature does:

  * **It never blocks a request.** The endpoint answers from the cache and
    kicks off a refresh in the background if that cache is stale, so the first
    page load after a restart says "no idea yet" for a second rather than
    holding the UI open against a network that may be firewalled. The banner
    appears on the next poll.
  * **It never fails loudly.** A blocked network, a proxy serving an HTML
    error page, a rate limit — all of it lands in `error` and the UI simply
    shows nothing. An update check is not worth an error state in a mail app.
  * **It can be switched off.** ``server.update_check = false`` in
    meerail.toml means this module makes no request at all, ever. It is the
    only thing in the server that talks to the internet, and on a machine that
    holds your entire mail archive that deserves to be a decision rather than
    a default nobody was told about.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from core.config import get_settings
from core.version import VERSION, is_outdated

log = logging.getLogger(__name__)

# The default branch's VERSION file. Raw githubusercontent, not the API: no
# rate limit worth worrying about, no token, and the response is ~6 bytes.
LATEST_URL = "https://raw.githubusercontent.com/ribalba/meerail/main/VERSION"

# Where the UI sends someone who has just been told an update exists. Not the
# releases page: the question that follows the banner is "what do I type", not
# "what changed", and the answer differs between the meerail.sh install and a
# clone. The README section covers both, and says what happens to the database.
UPDATE_URL = "https://github.com/ribalba/meerail#how-to-update"

# A day between checks. Releases are not frequent enough for anything shorter
# to tell you something new, and this is a call to a third party from every
# install in existence — being a good citizen is free here.
CHECK_INTERVAL = 24 * 3600

# After a failure, retry sooner than a day but not so soon that a machine with
# no internet spends its life retrying.
RETRY_INTERVAL = 3600

# Short: this runs in the background, but a hung connection would otherwise pin
# the "refresh in flight" flag and block the next attempt for as long as the OS
# takes to give up.
TIMEOUT = 8.0

# A version number is short and boring. Anything longer is a captive-portal
# login page or a proxy error, and parsing it as a version would be a mistake.
MAX_BODY = 64


_lock = asyncio.Lock()
_refreshing = False
_state: dict[str, object] = {
    "latest": None,      # str | None — the version on main, once known
    "checked_at": 0.0,   # monotonic clock; 0 = never
    "error": None,       # str | None — last failure, for the log and /api/version
}


def _stale() -> bool:
    checked = float(_state["checked_at"] or 0)
    if not checked:
        return True
    interval = RETRY_INTERVAL if _state["error"] else CHECK_INTERVAL
    return (time.monotonic() - checked) >= interval


async def _fetch() -> None:
    global _refreshing
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(LATEST_URL)
            resp.raise_for_status()
            body = resp.text[: MAX_BODY + 1]
        if len(body) > MAX_BODY:
            raise ValueError("response is not a version number")
        latest = body.strip()
        if not latest:
            raise ValueError("empty response")
        _state["latest"] = latest
        _state["error"] = None
    except Exception as exc:  # noqa: BLE001 - every failure is the same failure here
        # debug, not warning: an install with no outbound internet is a
        # perfectly good install, and this would be the only thing in its log.
        log.debug("update check failed: %s", exc)
        _state["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        _state["checked_at"] = time.monotonic()
        async with _lock:
            _refreshing = False


async def _refresh_if_stale() -> None:
    """Start a background check, unless one is running or the cache is fresh."""
    global _refreshing
    async with _lock:
        if _refreshing or not _stale():
            return
        _refreshing = True
    # Held deliberately: the task clears the flag in its own `finally`, so the
    # flag is what serialises checks, not the lock.
    asyncio.create_task(_fetch())


async def status() -> dict:
    """What the UI needs to decide whether to show the update notice."""
    settings = get_settings()
    if not settings.update_check:
        return {
            "version": VERSION,
            "latest": None,
            "update_available": False,
            "check_enabled": False,
            "update_url": UPDATE_URL,
        }

    await _refresh_if_stale()
    latest = _state["latest"]
    return {
        "version": VERSION,
        "latest": latest,
        "update_available": bool(latest) and is_outdated(VERSION, str(latest)),
        "check_enabled": True,
        "update_url": UPDATE_URL,
    }
