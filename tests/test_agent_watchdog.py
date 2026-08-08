"""The agent's last line of defence against a socket that never answers.

Written after an account stopped syncing for eleven hours without logging a
thing. Its sync thread was parked in a blocking ``read()`` inside a TLS
handshake, on a TCP connection that was still ESTABLISHED but black-holed — the
local address it was bound to had stopped routing after a network change. No
exception was ever raised, so the retry loop never ran, and the account's
``last_error`` stayed frozen on the failure from *before* the block, reading as a
fault that kept happening rather than one that had never ended.

Everything here is local sockets and real threads; nothing talks to a mail
server. What is being pinned is not IMAP behaviour but the guarantee above it:
no blocking call can hold a sync thread forever.
"""

import socket
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import imap as agent_imap
from core.config import AccountConfig
from imap import Bridge, _tune_socket


@contextmanager
def silent_server():
    """A listener that accepts and then never says anything.

    The polite half of the failure: the connection completes, so nothing fails
    at connect time, and then the server simply does not speak. What is left is
    a read that only a timeout can end.
    """
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    held = []

    def accept():
        try:
            conn, _ = srv.accept()
            held.append(conn)      # kept open, deliberately mute
        except OSError:
            pass

    threading.Thread(target=accept, daemon=True).start()
    try:
        yield srv.getsockname()[1]
    finally:
        srv.close()
        for conn in held:
            conn.close()


@contextmanager
def tcp_pair():
    """A connected TCP socket pair on loopback.

    Not socketpair(), which is AF_UNIX and rejects the keepalive options — the
    very thing under test. Both ends are real TCP, as the agent's are.
    """
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    client = socket.create_connection(srv.getsockname())
    server, _ = srv.accept()
    srv.close()
    try:
        yield client, server
    finally:
        client.close()
        server.close()


def _account(port, **kw):
    kw.setdefault("imap_connect_timeout", 1)
    kw.setdefault("imap_read_timeout", 1)
    return AccountConfig(email="probe@example.com", imap_host="127.0.0.1",
                         imap_port=port, imap_security="ssl", smtp_security="ssl",
                         username="u", password="p", verify_cert=False, **kw)


def _fast_watchdog(monkeypatch):
    """Shrink the deadlines so a stall is reached in seconds, not minutes."""
    monkeypatch.setattr(agent_imap, "_STALL_GRACE", 1)
    monkeypatch.setattr(agent_imap, "_WATCHDOG_TICK", 0.1)


# --------------------------------------------------------------------------- #
# The socket, before anything is read through it
# --------------------------------------------------------------------------- #

def test_tune_socket_sets_a_deadline_and_keepalive():
    """Both halves matter: the timeout bounds a read that is waiting, keepalive
    kills a connection whose far end has gone without saying so."""
    with tcp_pair() as (sock, _peer):
        _tune_socket(sock, 12.5)
        assert sock.gettimeout() == 12.5
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0


def test_connect_bounds_the_tls_handshake(monkeypatch):
    """The ordinary path, with the timeout in force: a server that accepts and
    then goes quiet fails the connect rather than swallowing the thread."""
    _fast_watchdog(monkeypatch)
    with silent_server() as port:
        bridge = Bridge(_account(port))
        started = time.monotonic()
        with pytest.raises(Exception):
            bridge.connect()
        # Well inside the watchdog's deadline (1 + 1 + 1), so it was the socket
        # timeout that did this, not the abort. Both are tested; they are not
        # the same mechanism and only one of them is supposed to be routine.
        assert time.monotonic() - started < 2.5
        assert bridge.client is None or True   # nothing to assert but no hang


def test_watchdog_recovers_a_handshake_with_no_timeout(monkeypatch):
    """The eleven-hour hang, reproduced and then survived.

    ``_tune_socket`` is replaced with one that clears the timeout instead of
    setting it, which is the state the wedged agent's socket was found in: the
    handshake gets a blocking read, and no deadline anywhere in Python or
    OpenSSL will ever end it. The watchdog has to.
    """
    _fast_watchdog(monkeypatch)
    monkeypatch.setattr(agent_imap, "_tune_socket",
                        lambda sock, timeout: sock.settimeout(None))

    with silent_server() as port:
        bridge = Bridge(_account(port))
        started = time.monotonic()
        with pytest.raises(Exception):
            bridge.connect()          # would never return without the watchdog
        elapsed = time.monotonic() - started

    # Deadline is connect + read + grace = 3s; the tick adds a fraction.
    assert 2.0 < elapsed < 15.0, f"took {elapsed:.1f}s"
    assert bridge not in agent_imap._watched


def test_abort_unblocks_a_thread_parked_in_a_read():
    """Why the watchdog shuts the raw socket down rather than closing it.

    close() on a descriptor another thread is blocked reading does not reliably
    wake that thread; shutdown() does, by making the read return end-of-stream.
    The whole recovery rests on this, so it is asserted directly — and through
    the duplicate handle abort() actually uses, not the original socket, since
    a shutdown that only worked on the original would be no use during the
    handshake this exists to interrupt.
    """
    with tcp_pair() as (sock, _peer):
        bridge = Bridge(_account(0))
        bridge._hold(sock)
        result = []

        def parked():
            try:
                result.append(sock.recv(1))   # blocking: no timeout, no data coming
            except OSError as exc:
                result.append(exc)

        reader = threading.Thread(target=parked, daemon=True)
        reader.start()
        time.sleep(0.2)
        assert reader.is_alive(), "the read should be blocked at this point"

        bridge.abort()
        reader.join(timeout=5)

        assert not reader.is_alive(), "abort() did not unblock the parked read"
        assert result, "the read returned nothing at all"


# --------------------------------------------------------------------------- #
# Deadline bookkeeping
# --------------------------------------------------------------------------- #

def test_a_call_is_only_overdue_while_it_is_in_flight():
    """A Bridge sitting idle between passes must never be aborted: the deadline
    exists for the duration of one blocking call and no longer."""
    bridge = Bridge(_account(0))
    assert bridge.overdue(time.monotonic()) is False

    seen = {}

    def slow():
        now = time.monotonic()
        seen["during"] = bridge.overdue(now + 100)   # 100s into a 5s deadline
        seen["not_yet"] = bridge.overdue(now)

    bridge._call("fetch", 5, slow)

    assert seen["during"] is True
    assert seen["not_yet"] is False
    assert bridge.overdue(time.monotonic() + 10_000) is False   # cleared on exit


def test_deadline_is_cleared_when_the_call_raises():
    """A raised call has already handed control back to the retry loop, so it
    must not leave a deadline behind for the watchdog to act on."""
    bridge = Bridge(_account(0))

    def boom():
        raise ConnectionResetError(54, "Connection reset by peer")

    with pytest.raises(ConnectionResetError):
        bridge._call("fetch", 0.01, boom)

    assert bridge.overdue(time.monotonic() + 10_000) is False


def test_ops_puts_write_back_commands_under_the_same_deadline():
    """actions.py drives COPY/EXPUNGE/CREATE through ops(), not through the raw
    client, so the write-back half of a pass cannot park the thread either."""
    bridge = Bridge(_account(0))
    seen = {}

    class FakeClient:
        def expunge(self):
            seen["overdue_far_future"] = bridge.overdue(time.monotonic() + 10_000)
            seen["what"] = bridge._what
            return "done"

    bridge.client = FakeClient()

    assert bridge.ops().expunge() == "done"
    assert seen["overdue_far_future"] is True    # a deadline was armed
    assert seen["what"] == "expunge"
    assert bridge.overdue(time.monotonic() + 10_000) is False
