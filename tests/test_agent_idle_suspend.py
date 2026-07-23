"""Unit coverage for suspend/resume handling in the agent's IDLE wait.

Pure unit test: the IMAPClient is a fake and the clock is driven by hand, so
this runs without an IMAP server and without actually sleeping. It pins the
behaviour that matters after a laptop wakes from suspend — the agent must notice
the stale connection at once and not send DONE on the dead socket.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import imap as agent_imap  # noqa: E402
from imap import Bridge, Suspended, _SUSPEND_GAP, _IDLE_SLICE  # noqa: E402


class FakeClient:
    """Minimal IMAPClient stand-in that records the IDLE handshake calls."""

    def __init__(self, checks):
        # `checks` is a list of return values for successive idle_check calls.
        self._checks = list(checks)
        self.idle_started = False
        self.done_called = False

    def idle(self):
        self.idle_started = True

    def idle_check(self, timeout=None):
        return self._checks.pop(0) if self._checks else []

    def idle_done(self):
        self.done_called = True


def _bridge(client):
    b = Bridge.__new__(Bridge)   # skip __init__: no real AccountConfig needed
    b.client = client
    return b


def _fake_clock(monkeypatch, wall_readings):
    """Drive time.time() through `wall_readings` and freeze time.monotonic().

    monotonic() does not advance across a suspend on Linux, so freezing it while
    wall time jumps is exactly the signal the code keys off."""
    walls = iter(wall_readings)
    monkeypatch.setattr(agent_imap.time, "time", lambda: next(walls))
    monkeypatch.setattr(agent_imap.time, "monotonic", lambda: 0.0)


def test_suspend_mid_idle_raises_and_skips_done(monkeypatch):
    """A slice that burns _SUSPEND_GAP of wall time is a suspend: idle_wait must
    raise Suspended and NOT send DONE, which would block on the dead socket."""
    client = FakeClient(checks=[[]])
    # before-reading, then after-reading a full suspend later.
    _fake_clock(monkeypatch, [100.0, 100.0 + _SUSPEND_GAP + 300.0])

    wake = _StubEvent()
    with pytest.raises(Suspended):
        _bridge(client).idle_wait(30, wake=wake)

    assert client.idle_started is True
    assert client.done_called is False   # the whole point: never DONE a corpse


def test_normal_slice_does_not_look_like_suspend(monkeypatch):
    """A slice that returns in real time is not a suspend: a change is reported
    normally and DONE is sent to close IDLE cleanly."""
    client = FakeClient(checks=[["EXISTS"]])   # something changed on the folder
    _fake_clock(monkeypatch, [100.0, 100.0 + 0.2])

    changed = _bridge(client).idle_wait(30, wake=_StubEvent())

    assert changed is True
    assert client.done_called is True


class _StubEvent:
    def is_set(self):
        return False


def test_abort_closes_without_logout():
    """abort() shuts the socket down instead of logging out, so a stale socket
    can't park the caller in a blocking BYE read."""
    class C:
        def __init__(self):
            self.shutdown_called = False
            self.logged_out = False

        def shutdown(self):
            self.shutdown_called = True

        def logout(self):
            self.logged_out = True

    c = C()
    b = _bridge(c)
    b.abort()

    assert c.shutdown_called is True
    assert c.logged_out is False
    assert b.client is None
