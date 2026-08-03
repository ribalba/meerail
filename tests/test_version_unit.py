"""The version number, and the "you are running an old one" check built on it.

Two things are being pinned here, and they fail in opposite directions:

  * **Version comparison must not produce false positives.** The whole feature
    is a banner that says "update available". One wrong comparison and every
    install in existence nags forever about an update that does not exist, with
    no way to tell it it is wrong. So anything unparseable compares as "no
    opinion" rather than as old.
  * **The check must never break the app.** It reaches out to the internet from
    a process whose job is showing mail. A refused connection, an HTML error
    page from a captive portal, a firewall that blackholes the request — none
    of it may raise, and none of it may claim an update.

Pure unit test: no containers, no network. The one test that exercises the
fetch path stubs httpx.
"""

from __future__ import annotations

import asyncio

import pytest

from core import version as V


# --- parsing ------------------------------------------------------------------

@pytest.mark.parametrize(
    "text, expected",
    [
        ("1.2.3", (1, 2, 3)),
        ("0.3.0", (0, 3, 0)),
        # Tags and stamps carry decoration the number does not.
        ("v1.2.3", (1, 2, 3)),
        ("1.2", (1, 2)),
        ("10.0.1", (10, 0, 1)),
        # The pre-release/build suffix stops the parse at the component it is
        # attached to, rather than being read as part of the number.
        ("1.2.3-rc1", (1, 2, 3)),
        ("1.2.3+dirty", (1, 2, 3)),
        # Nothing numeric at the head: no opinion, which is what "unknown" and
        # a bare git sha both have to be.
        ("unknown", None),
        ("", None),
        ("deadbeef", None),
        ("latest", None),
    ],
)
def test_parse(text, expected):
    assert V.parse(text) == expected


# --- comparison ---------------------------------------------------------------

@pytest.mark.parametrize(
    "current, latest",
    [
        ("0.3.0", "0.4.0"),
        ("0.3.0", "1.0.0"),
        ("0.3.9", "0.3.10"),   # not a string comparison
        ("1.2", "1.2.1"),      # shorter tuples zero-extend
    ],
)
def test_outdated(current, latest):
    assert V.is_outdated(current, latest) is True


@pytest.mark.parametrize(
    "current, latest",
    [
        ("0.3.0", "0.3.0"),    # equal is not outdated
        ("1.2.0", "1.2"),      # same, written differently
        ("0.4.0", "0.3.0"),    # running ahead of main, as a developer does
        ("0.3.10", "0.3.9"),
        # Every uncertainty resolves to silence.
        ("unknown", "0.4.0"),
        ("0.3.0", "unknown"),
        ("0.3.0", ""),
        ("", ""),
    ],
)
def test_not_outdated(current, latest):
    assert V.is_outdated(current, latest) is False


def test_version_file_is_a_version():
    """The shipped VERSION file has to parse, or every install's check is dead.

    It is also what the images are tagged with, so a typo here is a typo in a
    published tag.
    """
    assert V.parse(V.VERSION) is not None, f"VERSION file reads {V.VERSION!r}"


# --- the check itself ---------------------------------------------------------

def _status(monkeypatch, **settings_overrides):
    """Run app.updates.status() with a fresh cache and stubbed settings."""
    from app import updates

    class _Settings:
        update_check = settings_overrides.get("update_check", True)

    monkeypatch.setattr(updates, "get_settings", lambda: _Settings)
    monkeypatch.setattr(updates, "_state",
                        {"latest": None, "checked_at": 0.0, "error": None})
    monkeypatch.setattr(updates, "_refreshing", False)
    return asyncio.run(updates.status())


def test_check_disabled_makes_no_request(monkeypatch):
    """update_check = false means no outbound call at all, not a hidden banner."""
    from app import updates

    called = False

    async def _boom() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(updates, "_refresh_if_stale", _boom)
    payload = _status(monkeypatch, update_check=False)

    assert called is False
    assert payload["check_enabled"] is False
    assert payload["update_available"] is False
    assert payload["latest"] is None


def test_first_call_answers_from_cache_without_waiting(monkeypatch):
    """The endpoint must not block on the network — the banner can wait a poll.

    A cold cache answers "no update" and schedules the fetch; if this ever
    started awaiting the request instead, a firewalled install would hold every
    page load open for the timeout.
    """
    payload = _status(monkeypatch)
    assert payload["update_available"] is False
    assert payload["version"] == V.VERSION


def test_fetch_failure_is_silent(monkeypatch):
    """A dead network leaves the state usable and claims nothing."""
    from app import updates

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url): raise OSError("Network is unreachable")

    monkeypatch.setattr(updates.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(updates, "_state",
                        {"latest": None, "checked_at": 0.0, "error": None})
    asyncio.run(updates._fetch())

    assert updates._state["latest"] is None
    assert updates._state["error"]          # recorded, for the About line
    assert updates._state["checked_at"] > 0  # and not retried in a tight loop


def test_oversized_response_is_rejected(monkeypatch):
    """A captive portal's HTML must not be parsed as a version number."""
    from app import updates

    class _Resp:
        text = "<html><body>Sign in to continue" + "x" * 500
        def raise_for_status(self): pass

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url): return _Resp()

    monkeypatch.setattr(updates.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(updates, "_state",
                        {"latest": None, "checked_at": 0.0, "error": None})
    asyncio.run(updates._fetch())

    assert updates._state["latest"] is None
    assert updates._state["error"]


def test_successful_fetch_records_the_version(monkeypatch):
    from app import updates

    class _Resp:
        text = "9.9.9\n"
        def raise_for_status(self): pass

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url): return _Resp()

    monkeypatch.setattr(updates.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(updates, "_state",
                        {"latest": None, "checked_at": 0.0, "error": None})
    asyncio.run(updates._fetch())

    assert updates._state["latest"] == "9.9.9"
    assert updates._state["error"] is None
    assert V.is_outdated(V.VERSION, "9.9.9") is True
