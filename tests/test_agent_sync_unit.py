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

    def rollback(self):
        pass

    def close(self):
        pass


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

    def set_progress(self, *_args):
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


# --- Full recheck ------------------------------------------------------------

REQUESTED_AT = "2026-04-01T09:00:00"


class RecheckBridge:
    """An IMAP server with two folders and nothing new in either."""

    def connect(self): pass
    def logout(self): pass
    def list_folders(self):
        return [{"name": "INBOX", "role_hint": "\\inbox"},
                {"name": "Archive", "role_hint": ""}]
    def select(self, _name):
        return (1, 1)
    def new_uids(self, _last):
        return []
    def all_uids(self):
        return []
    def fetch_flags(self, _uids):
        return {}


class RecheckIngest:
    """core.ingest stand-in that records the recheck handshake."""

    def __init__(self, pending):
        self.pending = pending
        self.reset = []          # folders rewound
        self.cleared = []         # timestamps handed back to clear_recheck
        self.progress = []        # every progress snapshot written
        self.pruned = []          # authoritative folder-name sets

    def get_or_create_account(self, _db, email):
        return type("Acc", (), {"email": email, "id": 1})()

    def take_recheck(self, _db, _account):
        return self.pending

    def register_folder(self, _db, _acc, name, *_a, **_kw):
        return type("MB", (), {"imap_name": name, "last_uid": 7})()

    def reset_cursor(self, _db, mailbox):
        self.reset.append(mailbox.imap_name)
        mailbox.last_uid = 0

    def clear_recheck(self, _db, _account, requested_at):
        self.cleared.append(requested_at)

    def set_progress(self, _db, _account, progress):
        self.progress.append(progress)

    def update_flags(self, *_a): pass
    def prune_vanished(self, *_a): pass
    def prune_mailboxes(self, _db, _account, names): self.pruned.append(names)
    def extract_pending(self, _db): return 0
    def thumb_pending(self, _db): return 0
    def record_sync(self, *_a, **_kw): pass


class Cfg:
    batch_size = 100
    poll_interval = 30


class AccountCfg:
    email = "user@example.com"
    # Endpoint fields a real AccountConfig always carries; sync_once logs them
    # when it connects.
    imap_host = "127.0.0.1"
    imap_port = 1143
    imap_security = "starttls"
    def send_addresses(self): return []


def _run_pass(monkeypatch, pending):
    spy = RecheckIngest(pending)
    monkeypatch.setattr(agent_sync, "ingest", spy)
    monkeypatch.setattr(agent_sync, "Bridge", lambda _a: RecheckBridge())
    monkeypatch.setattr(agent_sync, "SessionLocal", lambda: DB())
    monkeypatch.setattr(agent_sync, "drain_actions", lambda *_a: None)
    agent_sync.sync_once(AccountCfg(), Cfg())
    return spy


def test_recheck_rewinds_every_folder_then_clears_the_request(monkeypatch):
    """The point of a recheck: no folder keeps its cursor, so the next fetch
    re-walks the whole mailbox rather than only what is new."""
    spy = _run_pass(monkeypatch, REQUESTED_AT)

    assert spy.reset == ["INBOX", "Archive"]
    # Cleared with the timestamp the pass read, not unconditionally — that is
    # what stops it from swallowing a request raised while it was running.
    assert spy.cleared == [REQUESTED_AT]


def test_normal_pass_leaves_cursors_alone(monkeypatch):
    """Without a request, syncing stays incremental — a recheck is expensive and
    must never happen by accident."""
    spy = _run_pass(monkeypatch, None)

    assert spy.reset == []
    assert spy.cleared == []
    assert spy.pruned == [{"INBOX", "Archive"}]


# --- Progress reporting ------------------------------------------------------


def test_pass_reports_folder_position_and_closes_out(monkeypatch):
    """Every folder announces itself as it is entered, and the pass marks itself
    finished — otherwise the UI shows a bar that never stops moving."""
    spy = _run_pass(monkeypatch, None)

    entered = [(p["folder"], p["folder_index"]) for p in spy.progress if p["active"]]
    assert entered == [("INBOX", 1), ("Archive", 2)]
    assert all(p["folder_count"] == 2 for p in spy.progress)

    last = spy.progress[-1]
    assert last["active"] is False
    assert last["finished_at"] is not None


def test_progress_counts_uids_walked_not_messages_stored(monkeypatch):
    """A Proton mailbox resolves most backfill UIDs to content it already holds.
    If the bar counted stored messages it would sit still through exactly those
    stretches, so it counts UIDs examined instead."""
    spy = IngestSpy()
    monkeypatch.setattr(agent_sync, "ingest", spy)

    written = []
    spy.set_progress = lambda _db, _acc, p: written.append(p)

    class Deduped:
        """Two UIDs, both already held under another label — nothing to store."""
        def new_uids(self, _last): return [1, 2]
        def fetch_headers(self, _uids):
            return {1: {"message_id": "one", "flags": {}},
                    2: {"message_id": "two", "flags": {}}}

    spy.record_known = lambda *_a: True     # every UID is content we have

    progress = agent_sync.PassProgress(1)
    progress.enter_folder("Archive", 0)
    stored = agent_sync._sync_new(DB(), Deduped(), object(), Mailbox(), 100, progress)

    assert stored == 0                      # nothing new landed...
    assert written[-1]["folder_done"] == 2  # ...but the folder is fully walked
    assert written[-1]["folder_total"] == 2
    assert written[-1]["stored"] == 0
