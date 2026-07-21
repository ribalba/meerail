"""Unit coverage for cursor safety in the stateless agent sync."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from sync import _sync_new  # noqa: E402


class Bridge:
    def new_uids(self, _last):
        return [1, 2]

    def fetch_headers(self, _uids):
        return {1: {"message_id": "one", "flags": {}}}


class Server:
    advanced = False

    def advance_cursor(self, *_args):
        self.advanced = True


def test_incomplete_header_fetch_does_not_advance_cursor():
    server = Server()
    with pytest.raises(RuntimeError, match="omitted UIDs"):
        _sync_new(Bridge(), server, "a@example.com", "INBOX", 1, 0, 100)
    assert server.advanced is False
