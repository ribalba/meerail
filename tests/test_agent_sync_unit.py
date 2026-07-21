"""Unit coverage for cursor safety in the agent's sync loop.

Pure unit test: `core.ingest` and the DB session are stubbed out, so this runs
without Postgres or an IMAP server.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import sync as agent_sync  # noqa: E402


class Bridge:
    """An IMAP server that reports two new UIDs but only returns one header."""

    def new_uids(self, _last):
        return [1, 2]

    def fetch_headers(self, _uids):
        return {1: {"message_id": "one", "flags": {}}}


class Mailbox:
    id = 1
    imap_name = "INBOX"
    last_uid = 0


class DB:
    committed = False

    def commit(self):
        self.committed = True


class IngestSpy:
    """Stands in for core.ingest, recording whether the cursor moved."""

    def __init__(self):
        self.advanced = False

    def record_known(self, *_args):
        return False

    def store_message(self, *_args):
        return True

    def advance_cursor(self, *_args):
        self.advanced = True

    def note_ingested(self, *_args):
        pass


def test_incomplete_header_fetch_does_not_advance_cursor(monkeypatch):
    """A partial IMAP fetch must abort the batch, so the UIDs are retried next
    pass instead of being silently skipped past the cursor."""
    spy = IngestSpy()
    monkeypatch.setattr(agent_sync, "ingest", spy)
    db = DB()

    with pytest.raises(RuntimeError, match="omitted UIDs"):
        agent_sync._sync_new(db, Bridge(), object(), Mailbox(), 100)

    assert spy.advanced is False
    assert db.committed is False
