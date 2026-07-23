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
    """An IMAP server that reports two new UIDs but only ever returns one
    header, however many times it is asked."""

    calls = 0

    def new_uids(self, _last):
        return [1, 2]

    def fetch_headers(self, _uids):
        self.calls += 1
        return {1: {"message_id": "one", "flags": {}, "date": None, "size": 0}}


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

    def touch_agent(self, *_args):
        pass


@pytest.fixture
def no_backoff(monkeypatch):
    """Run the refetch rounds without their waits."""
    monkeypatch.setattr(agent_sync, "_FETCH_BACKOFF", 0)


def test_incomplete_header_fetch_does_not_advance_cursor(monkeypatch, no_backoff):
    """A fetch that stays incomplete must abort the batch, so the UIDs are
    retried next pass instead of being silently skipped past the cursor."""
    spy = IngestSpy()
    monkeypatch.setattr(agent_sync, "ingest", spy)
    db = DB()
    bridge = Bridge()

    with pytest.raises(RuntimeError, match="omitted UIDs"):
        agent_sync._sync_new(db, bridge, object(), Mailbox(), 100)

    # It did not give up on the first partial answer.
    assert bridge.calls > 1
    assert spy.advanced is False
    assert db.committed is False


class FlakyBridge:
    """Omits a UID from the bulk fetch, but hands it over when asked alone.

    This is Gmail under load: the size of the ask is the problem, not the
    message. One such UID used to end a pass that had been running for hours.
    """

    def __init__(self):
        self.asked = []

    def new_uids(self, _last):
        return [1, 2]

    def fetch_headers(self, uids):
        return {u: {"message_id": str(u), "flags": {}, "date": None, "size": 0}
                for u in uids}

    def fetch_raw(self, uids):
        self.asked.append(list(uids))
        return {u: {"raw": b"From: a@b\r\n\r\nhi", "flags": {}}
                for u in uids if not (len(uids) > 1 and u == 2)}


def test_partial_raw_fetch_is_refetched_rather_than_failing_the_pass(monkeypatch,
                                                                    no_backoff):
    """The UID the bulk fetch left out is asked for on its own, and the batch
    completes — the whole point, since the alternative restarts the backfill."""
    spy = IngestSpy()
    monkeypatch.setattr(agent_sync, "ingest", spy)
    db = DB()
    bridge = FlakyBridge()

    stored = agent_sync._sync_new(db, bridge, object(), Mailbox(), 100)

    assert bridge.asked == [[1, 2], [2]]   # bulk, then the straggler alone
    assert stored == 2
    assert spy.advanced is True            # cursor moves: nothing was skipped


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
        self.errors_cleared = 0   # calls to clear_agent_error
        self.touched = []         # every liveness stamp written

    def get_or_create_account(self, _db, email):
        return type("Acc", (), {"email": email, "id": 1})()

    def clear_agent_error(self, _db, _account):
        self.errors_cleared += 1

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

    def touch_agent(self, _db, account):
        self.touched.append(account)

    def update_flags(self, *_a): pass
    def prune_vanished(self, *_a): pass
    def prune_mailboxes(self, _db, _account, names): self.pruned.append(names)
    def extract_pending(self, _db): return 0
    def thumb_pending(self, _db): return 0
    def record_sync(self, *_a, **_kw): pass
    def content_cutoff(self, _months): return None
    def record_content_window(self, _db, months): self.window = months


class Cfg:
    batch_size = 100
    poll_interval = 30
    content_window_months = 0


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


def test_account_batch_size_overrides_the_global(monkeypatch):
    """What one server answers comfortably another truncates or disconnects
    over, so an account that asks for less must get to."""
    monkeypatch.setattr(agent_sync, "ingest", RecheckIngest(None))
    monkeypatch.setattr(agent_sync, "Bridge", lambda _a: RecheckBridge())
    monkeypatch.setattr(agent_sync, "SessionLocal", lambda: DB())
    monkeypatch.setattr(agent_sync, "drain_actions", lambda *_a: None)
    batches = []
    monkeypatch.setattr(agent_sync, "_sync_new",
                        lambda _db, _b, _a, _mb, batch, *_r: batches.append(batch) or 0)
    monkeypatch.setattr(agent_sync, "_reconcile",
                        lambda _db, _b, _mb, batch, *_r: batches.append(batch))

    account = AccountCfg()
    account.batch_size = 25
    agent_sync.sync_once(account, Cfg())

    assert batches and set(batches) == {25}     # not Cfg.batch_size


def test_batch_size_falls_back_to_the_global(monkeypatch):
    """Accounts that say nothing keep following the top-level setting."""
    monkeypatch.setattr(agent_sync, "ingest", RecheckIngest(None))
    monkeypatch.setattr(agent_sync, "Bridge", lambda _a: RecheckBridge())
    monkeypatch.setattr(agent_sync, "SessionLocal", lambda: DB())
    monkeypatch.setattr(agent_sync, "drain_actions", lambda *_a: None)
    batches = []
    monkeypatch.setattr(agent_sync, "_sync_new",
                        lambda _db, _b, _a, _mb, batch, *_r: batches.append(batch) or 0)
    monkeypatch.setattr(agent_sync, "_reconcile",
                        lambda _db, _b, _mb, batch, *_r: batches.append(batch))

    agent_sync.sync_once(AccountCfg(), Cfg())

    assert batches and set(batches) == {Cfg.batch_size}


def test_pass_clears_a_recorded_error_up_front(monkeypatch):
    """Not at the end, where a completed pass would be the only proof.

    The initial backfill of a large mailbox runs for many minutes, so an error
    cleared only on completion leaves the panel reading "failing" long after the
    agent has reconnected and started working again. Connect and login having
    succeeded is the earliest point the previous failure is known to be over.
    """
    spy = _run_pass(monkeypatch, None)

    assert spy.errors_cleared == 1


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


# --- Liveness ----------------------------------------------------------------


class FlagBridge:
    """A folder of ``count`` UIDs, whose flags are all it will ever be asked for."""

    def __init__(self, count):
        self.uids = list(range(1, count + 1))

    def all_uids(self):
        return self.uids

    def fetch_flags(self, uids):
        return {u: {"seen": True} for u in uids}


class CountingDB(DB):
    """A session that remembers how often it was told to commit."""

    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


def _liveness_spy(monkeypatch):
    spy = IngestSpy()
    spy.touched = []
    spy.touch_agent = lambda _db, account: spy.touched.append(account)
    spy.update_flags = lambda *_a: None
    spy.prune_vanished = lambda *_a: None
    monkeypatch.setattr(agent_sync, "ingest", spy)
    return spy


def test_a_long_flag_sweep_says_it_is_alive_before_it_ends(monkeypatch):
    """The sweep walks every UID in the folder and commits once, at the end.

    On a large mailbox against a server that answers a command a second — Gmail
    charges some accounts about ten seconds, whatever they asked for — that is
    hours in which nothing reaches the database. The status panel reads
    last_agent_seen and nothing else, so for all of it the agent working hardest
    is the one reported offline. The stamp has to come from inside the loop.
    """
    monkeypatch.setattr(agent_sync, "_HEARTBEAT_INTERVAL", 0)   # every chunk is due
    spy = _liveness_spy(monkeypatch)

    db, account = CountingDB(), object()
    agent_sync._reconcile(db, FlagBridge(10), Mailbox(), 2,
                          agent_sync.Heartbeat(db, account))

    assert spy.touched == [account] * 5   # one per chunk, not one at the end
    assert db.commits == 6                # ...and each is committed, plus the sweep's own
    # A stamp still sitting in the session is invisible to the panel, which is a
    # different process reading a different connection.


def test_the_heartbeat_is_rate_limited_rather_than_per_chunk(monkeypatch):
    """Against a fast server the same loop runs a chunk in milliseconds, and a
    commit apiece would be write amplification for a column the UI reads once a
    poll. Until the interval is up the sweep must be left exactly as it was."""
    monkeypatch.setattr(agent_sync, "_HEARTBEAT_INTERVAL", 3600)
    spy = _liveness_spy(monkeypatch)

    db, account = CountingDB(), object()
    agent_sync._reconcile(db, FlagBridge(10), Mailbox(), 2,
                          agent_sync.Heartbeat(db, account))

    assert spy.touched == []
    assert db.commits == 1                # the sweep's own commit, and no other


def test_ingesting_a_chunk_also_stamps_liveness(monkeypatch):
    """A chunk here fetches whole message bodies, so it can run for minutes on a
    slow link. Unlike the flag sweep this loop already commits once per chunk,
    so the stamp rides that write rather than paying for one of its own."""
    spy = _liveness_spy(monkeypatch)
    spy.record_known = lambda *_a: True    # every UID is content we already hold

    class Deduped:
        def new_uids(self, _last): return [1, 2]
        def fetch_headers(self, uids):
            return {u: {"message_id": str(u), "flags": {}, "date": None, "size": 0}
                    for u in uids}

    db, account = CountingDB(), object()
    agent_sync._sync_new(db, Deduped(), account, Mailbox(), 1, None, None,
                         agent_sync.Heartbeat(db, account))

    assert spy.touched == [account] * 2    # one per chunk...
    assert db.commits == 2                 # ...and no commit beyond the loop's own


def test_every_folder_the_pass_enters_stamps_liveness(monkeypatch):
    """Selecting and searching a folder is two round trips before either loop
    below gets a chance to stamp. On a slow server a pass over many folders must
    not be able to age out in the gaps between them."""
    spy = _run_pass(monkeypatch, None)

    assert len(spy.touched) == 2           # one per folder entered
