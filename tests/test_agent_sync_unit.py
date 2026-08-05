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

    def find_short_content(self, *_args):
        return []

    def restore_content(self, *_args):
        return True


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

    # What SELECT said the folder holds; nothing in it, and SEARCH agrees.
    exists = 0

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

    def record_presentation(self, _db, _account, values):
        self.presentation = values

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
    def unplaced_uids(self, *_a): return []
    def has_move_in_flight(self, *_a): return False
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
    def send_identities(self): return []
    def presentation(self): return {}


def _run_pass(monkeypatch, pending):
    spy = RecheckIngest(pending)
    monkeypatch.setattr(agent_sync, "ingest", spy)
    monkeypatch.setattr(agent_sync, "Bridge", lambda _a: RecheckBridge())
    monkeypatch.setattr(agent_sync, "SessionLocal", lambda: DB())
    monkeypatch.setattr(agent_sync, "drain_actions", lambda *_a: (0, 0, 0))
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
    monkeypatch.setattr(agent_sync, "drain_actions", lambda *_a: (0, 0, 0))
    batches = []
    monkeypatch.setattr(agent_sync, "_sync_new",
                        lambda _db, _b, _a, _mb, batch, *_r: batches.append(batch) or 0)
    monkeypatch.setattr(agent_sync, "_reconcile",
                        lambda _db, _b, _a, _mb, batch, *_r: batches.append(batch))

    account = AccountCfg()
    account.batch_size = 25
    agent_sync.sync_once(account, Cfg())

    assert batches and set(batches) == {25}     # not Cfg.batch_size


def test_batch_size_falls_back_to_the_global(monkeypatch):
    """Accounts that say nothing keep following the top-level setting."""
    monkeypatch.setattr(agent_sync, "ingest", RecheckIngest(None))
    monkeypatch.setattr(agent_sync, "Bridge", lambda _a: RecheckBridge())
    monkeypatch.setattr(agent_sync, "SessionLocal", lambda: DB())
    monkeypatch.setattr(agent_sync, "drain_actions", lambda *_a: (0, 0, 0))
    batches = []
    monkeypatch.setattr(agent_sync, "_sync_new",
                        lambda _db, _b, _a, _mb, batch, *_r: batches.append(batch) or 0)
    monkeypatch.setattr(agent_sync, "_reconcile",
                        lambda _db, _b, _a, _mb, batch, *_r: batches.append(batch))

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


def test_the_inbox_is_walked_first_but_keeps_its_listed_position(monkeypatch):
    """Servers routinely LIST the inbox somewhere in the middle. Walking folders
    in that order leaves the one folder the user is looking at waiting behind
    every archive folder on the account, so the walk starts at the inbox — while
    sort_order still records where LIST put it, because that is the order the UI
    reads back."""

    class Middling(RecheckBridge):
        def list_folders(self):
            return [{"name": "Archive", "role_hint": "\\Archive"},
                    {"name": "Work", "role_hint": ""},
                    {"name": "INBOX", "role_hint": ""},
                    {"name": "Sent", "role_hint": "\\Sent"}]

    class Recording(RecheckIngest):
        def __init__(self):
            super().__init__(None)
            self.registered = []

        def register_folder(self, db, acc, name, *a, **kw):
            self.registered.append((name, kw["sort_order"]))
            return super().register_folder(db, acc, name, *a, **kw)

    spy = Recording()
    monkeypatch.setattr(agent_sync, "ingest", spy)
    monkeypatch.setattr(agent_sync, "Bridge", lambda _a: Middling())
    monkeypatch.setattr(agent_sync, "SessionLocal", lambda: DB())
    monkeypatch.setattr(agent_sync, "drain_actions", lambda *_a: (0, 0, 0))
    agent_sync.sync_once(AccountCfg(), Cfg())

    # Inbox first, and the rest still in the order the server listed them.
    assert [name for name, _ in spy.registered] == ["INBOX", "Archive", "Work", "Sent"]
    # Display order is untouched: reordering the walk must not reshuffle the
    # folder list in the UI.
    assert dict(spy.registered) == {"Archive": 0, "Work": 1, "INBOX": 2, "Sent": 3}


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
    """A folder of ``count`` UIDs, whose flags are all it will ever be asked for.

    ``exists`` is what SELECT reported and ``all_uids`` what SEARCH answered;
    a server that is telling the truth returns the same number twice, and
    ``short`` is the one that does not.
    """

    def __init__(self, count, short=0):
        self.uids = list(range(1, count + 1))
        self._exists = count + short

    @property
    def exists(self):
        return self._exists

    def all_uids(self):
        return self.uids

    def fetch_flags(self, uids):
        return {u: {"flags": {"seen": True}, "size": 0} for u in uids}


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
    spy.unplaced_uids = lambda *_a: []
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
    agent_sync._reconcile(db, FlagBridge(10), account, Mailbox(), 2,
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
    agent_sync._reconcile(db, FlagBridge(10), account, Mailbox(), 2,
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


# --- Deleting local mail -----------------------------------------------------
#
# Pruning is the only place the agent removes stored mail on its own, and its
# whole evidence is "the server did not list this UID". The failure that matters
# is not a connection that drops — the pass dies and nothing is deleted — but
# one that stays up and answers short: Proton Bridge keeps serving while it
# cannot reach Proton, and a mailbox it has not finished loading answers SEARCH
# with a fraction of what it holds.


def _prune_spy(monkeypatch):
    spy = IngestSpy()
    spy.pruned = []
    spy.update_flags = lambda *_a: None
    spy.unplaced_uids = lambda *_a: []
    spy.prune_vanished = lambda _db, _mb, uids: spy.pruned.append(list(uids))
    monkeypatch.setattr(agent_sync, "ingest", spy)
    return spy


def test_a_server_that_answers_short_never_deletes_anything(monkeypatch, capsys):
    """SELECT says 500 messages, SEARCH lists 3. Whatever that is, it is not
    evidence that 497 messages were deleted."""
    spy = _prune_spy(monkeypatch)

    agent_sync._reconcile(CountingDB(), FlagBridge(3, short=497), object(), Mailbox(), 100,
                          email="user@example.com")

    assert spy.pruned == []
    out = capsys.readouterr().out
    assert "not removing anything" in out


def test_a_folder_that_really_is_empty_still_prunes(monkeypatch):
    """The guard must not become a reason to keep mail the user deleted
    elsewhere: an empty folder that the server agrees is empty still prunes."""
    spy = _prune_spy(monkeypatch)

    agent_sync._reconcile(CountingDB(), FlagBridge(0), object(), Mailbox(), 100)

    assert spy.pruned == [[]]


def test_a_complete_answer_prunes_as_before(monkeypatch):
    spy = _prune_spy(monkeypatch)

    agent_sync._reconcile(CountingDB(), FlagBridge(4), object(), Mailbox(), 100)

    assert spy.pruned == [[1, 2, 3, 4]]


def test_mail_arriving_mid_sweep_does_not_block_the_prune(monkeypatch):
    """SEARCH may legitimately return more than SELECT counted, if mail lands
    between the two commands. Only *fewer* is suspect."""
    spy = _prune_spy(monkeypatch)

    agent_sync._reconcile(CountingDB(), FlagBridge(4, short=-2), object(), Mailbox(), 100)

    assert spy.pruned == [[1, 2, 3, 4]]


# --- content that was stored short -------------------------------------------
#
# Content is fetched once and never read again. That is only sound while what
# the server answered was the whole message — and Proton, asked for a message it
# has created but not yet linked the attachments to, answers with the body
# alone. The mail lands looking complete and the attachment is gone from the
# mailbox for good. The sweep already walks every UID; RFC822.SIZE is what makes
# it noticeable, and this is the only place the agent goes back for bytes it has
# already fetched.


class ShortBridge:
    """A folder of three, where UID 2 is held locally at less than its size."""

    def __init__(self, sizes=None):
        self.sizes = sizes or {1: 100, 2: 745842, 3: 300}
        self.refetched = []

    exists = 3

    def all_uids(self):
        return [1, 2, 3]

    def fetch_flags(self, uids):
        return {u: {"flags": {"seen": True}, "size": self.sizes[u]} for u in uids}

    def fetch_raw(self, uids):
        self.refetched.append(list(uids))
        return {u: {"raw": b"the whole message", "flags": {"seen": True}} for u in uids}


def _short_spy(monkeypatch, short):
    spy = IngestSpy()
    spy.restored = []
    spy.pruned = []
    spy.update_flags = lambda *_a: None
    spy.unplaced_uids = lambda *_a: []
    spy.prune_vanished = lambda _db, _mb, uids: spy.pruned.append(list(uids))
    spy.find_short_content = lambda _db, _mb, _sizes: list(short)
    spy.restore_content = lambda _db, _mb, uid, raw: spy.restored.append((uid, raw)) or True
    monkeypatch.setattr(agent_sync, "ingest", spy)
    return spy


@pytest.fixture(autouse=True)
def _forget_refetches():
    """The memo below is module state, and a test that filled it must not decide
    what the next one sees."""
    agent_sync._refetched.clear()
    yield
    agent_sync._refetched.clear()


def test_a_message_stored_short_of_the_servers_size_is_fetched_again(monkeypatch, capsys):
    spy = _short_spy(monkeypatch, [2])
    bridge = ShortBridge()

    agent_sync._reconcile(CountingDB(), bridge, object(), Mailbox(), 100, email="user@example.com")

    assert bridge.refetched == [[2]]                    # only the short one
    assert spy.restored == [(2, b"the whole message")]
    assert "re-fetched 1 message" in capsys.readouterr().out


def test_the_same_short_message_is_not_fetched_again_every_sweep(monkeypatch):
    """The evidence is the server's own arithmetic, and some servers report a
    size they never quite hand over. Believing that one every reconcile interval
    would re-download the mailbox for good."""
    spy = _short_spy(monkeypatch, [2])
    bridge = ShortBridge()

    for _ in range(3):
        agent_sync._reconcile(CountingDB(), bridge, object(), Mailbox(), 100)

    assert bridge.refetched == [[2]]
    assert len(spy.restored) == 1


def test_a_message_that_changes_size_again_is_fetched_again(monkeypatch):
    """The memo is per size, not per UID: a message that really did change is
    still a message we do not hold."""
    _short_spy(monkeypatch, [2])
    bridge = ShortBridge()

    agent_sync._reconcile(CountingDB(), bridge, object(), Mailbox(), 100)
    bridge.sizes[2] += 1000
    agent_sync._reconcile(CountingDB(), bridge, object(), Mailbox(), 100)

    assert bridge.refetched == [[2], [2]]


def test_a_folder_the_server_reports_no_sizes_for_is_left_alone(monkeypatch):
    """A size of zero is a server that did not answer the question, not a
    message we are missing every byte of."""
    spy = _short_spy(monkeypatch, [1, 2, 3])
    spy.find_short_content = lambda _db, _mb, sizes: list(sizes)   # would take anything
    bridge = ShortBridge(sizes={1: 0, 2: 0, 3: 0})

    agent_sync._reconcile(CountingDB(), bridge, object(), Mailbox(), 100)

    assert bridge.refetched == []


def test_a_repair_that_cannot_fetch_does_not_fail_the_sweep(monkeypatch, capsys, no_backoff):
    """The sweep it rides on is doing the mailbox's real work — pulling flags and
    finding what vanished. A message that has been short for a week can stay
    short for another quarter of an hour."""
    spy = _short_spy(monkeypatch, [2])
    bridge = ShortBridge()
    bridge.fetch_raw = lambda _uids: {}          # answers, with nothing in it

    agent_sync._reconcile(CountingDB(), bridge, object(), Mailbox(), 100, email="user@example.com")

    assert spy.restored == []
    assert spy.pruned == [[1, 2, 3]]             # the sweep ran to the end
    assert "could not re-fetch" in capsys.readouterr().out
    # Nothing was remembered, so the next sweep is free to try again.
    assert agent_sync._refetched == {}


# --- placements the database lost --------------------------------------------
#
# The other half of prune_vanished, and the half that was missing for a long
# time: a pass only ever *read* below a folder's cursor, so a placement removed
# below it was removed for good. What that looked like in the field was one
# message sitting in an inbox on one machine and absent from the same inbox on
# another, both syncing the same account, neither of them wrong about anything
# it could see.


class GappyBridge:
    """A folder holding four messages, of which the database has lost two."""

    def __init__(self, missing=(2, 3)):
        self.missing = list(missing)
        self.exists = 4
        self.fetched = []

    def all_uids(self):
        return [1, 2, 3, 4]

    def fetch_flags(self, uids):
        return {u: {"flags": {"seen": True}, "size": 0} for u in uids}

    def fetch_headers(self, uids):
        self.fetched.append(list(uids))
        return {u: {"message_id": f"m{u}", "flags": {"seen": True}, "date": None, "size": 0}
                for u in uids}

    def fetch_raw(self, uids):
        return {u: {"raw": b"the whole message", "flags": {"seen": True}} for u in uids}


def _gap_spy(monkeypatch, missing=(2, 3), in_flight=()):
    spy = IngestSpy()
    spy.pruned = []
    spy.placed = []
    spy.update_flags = lambda *_a: None
    spy.prune_vanished = lambda _db, _mb, uids: spy.pruned.append(list(uids))
    spy.unplaced_uids = lambda _db, _mb, _uids: list(missing)
    spy.has_move_in_flight = lambda _db, _acc, message_id: message_id in in_flight
    spy.record_known = lambda _db, _acc, _mb, uid, _f, _mid: spy.placed.append(uid) or True
    monkeypatch.setattr(agent_sync, "ingest", spy)
    return spy


def test_a_message_the_server_lists_but_the_database_lost_is_fetched_back(monkeypatch, capsys):
    """The bug this exists for. A placement pruned during a moment when the
    server did not list it — a Bridge part way through loading the mailbox, a
    label cleared and put back — used to be invisible for good: it sits below
    the cursor, so nothing ever asks for that UID again."""
    spy = _gap_spy(monkeypatch)

    agent_sync._reconcile(CountingDB(), GappyBridge(), object(), Mailbox(), 100,
                          email="user@example.com")

    assert spy.placed == [2, 3]
    assert spy.pruned == [[1, 2, 3, 4]]          # the sweep still ran to the end
    assert "restored 2 message" in capsys.readouterr().out


def test_a_folder_with_nothing_missing_asks_the_server_for_nothing(monkeypatch):
    """The common case is a folder that is entirely fine, and it must not cost a
    fetch to find that out."""
    spy = _gap_spy(monkeypatch, missing=())
    bridge = GappyBridge()

    agent_sync._reconcile(CountingDB(), bridge, object(), Mailbox(), 100)

    assert bridge.fetched == []
    assert spy.placed == []


def test_a_message_being_moved_is_not_dragged_back_into_the_folder_it_left(monkeypatch):
    """A move the user has just made looks exactly like a gap from here: the
    source placement goes the moment the key is pressed, and the server goes on
    listing the UID until the agent applies the move and the server catches up.

    Restoring it would put the message straight back into the folder it was
    archived out of — the disappearance this whole mechanism exists to prevent,
    running backwards."""
    spy = _gap_spy(monkeypatch, missing=(2, 3), in_flight={"m2"})

    agent_sync._reconcile(CountingDB(), GappyBridge(), object(), Mailbox(), 100)

    assert spy.placed == [3]                     # 2 is still on its way out


def test_a_whole_chunk_being_moved_costs_no_write(monkeypatch):
    """Every UID in the chunk is in flight, so there is nothing left to store —
    and _store_chunk must not be called with an empty batch just to prove it."""
    spy = _gap_spy(monkeypatch, missing=(2, 3), in_flight={"m2", "m3"})
    calls = []
    monkeypatch.setattr(agent_sync, "_store_chunk",
                        lambda *a, **kw: calls.append(a) or (0, 0))

    agent_sync._reconcile(CountingDB(), GappyBridge(), object(), Mailbox(), 100)

    assert calls == []


def test_a_gap_bigger_than_one_sweep_is_spread_out_and_says_what_it_left(monkeypatch, capsys):
    """A gap this size is a database that has lost most of a mailbox, which the
    full recheck exists for. Doing it here instead would hold the sweep open for
    hours, every fifteen minutes — so it is capped, and the cap is logged rather
    than quietly applied."""
    monkeypatch.setattr(agent_sync, "_RESTORE_PER_PASS", 2)
    spy = _gap_spy(monkeypatch, missing=(2, 3, 4, 5, 6))

    agent_sync._reconcile(CountingDB(), GappyBridge(), object(), Mailbox(), 100,
                          email="user@example.com")

    assert spy.placed == [2, 3]
    assert "3 more message(s)" in capsys.readouterr().out


def test_a_restore_that_cannot_fetch_does_not_fail_the_sweep(monkeypatch, capsys, no_backoff):
    """Same bargain as the short-content repair: the sweep it rides on is doing
    the folder's real work, and a placement that has been missing for a week can
    stay missing for another quarter of an hour."""
    spy = _gap_spy(monkeypatch)
    bridge = GappyBridge()
    bridge.fetch_headers = lambda _uids: {}      # answers, with nothing in it

    agent_sync._reconcile(CountingDB(), bridge, object(), Mailbox(), 100,
                          email="user@example.com")

    assert spy.placed == []
    assert spy.pruned == [[1, 2, 3, 4]]          # the sweep ran to the end
    assert "could not restore missing messages" in capsys.readouterr().out


def test_a_pass_that_sent_mail_waits_before_reading_the_folders_back(monkeypatch):
    """The pass that sends is the pass that reads the copy back, seconds later,
    and Proton has not finished assembling it. Waiting is what keeps the sent
    mail in the mailbox whole."""
    order = []
    monkeypatch.setattr(agent_sync, "ingest", RecheckIngest(None))
    monkeypatch.setattr(agent_sync, "Bridge", lambda _a: RecheckBridge())
    monkeypatch.setattr(agent_sync, "SessionLocal", lambda: DB())
    monkeypatch.setattr(agent_sync.time, "sleep", lambda s: order.append(("slept", s)))
    monkeypatch.setattr(agent_sync, "_sync_new",
                        lambda *_a, **_kw: order.append(("walked",)) or 0)

    monkeypatch.setattr(agent_sync, "drain_actions", lambda *_a: (1, 0, 1))
    agent_sync.sync_once(AccountCfg(), Cfg(), reconcile=False)
    assert order[0] == ("slept", agent_sync._SEND_SETTLE_SECONDS)
    assert ("walked",) in order

    # And a pass that only pushed flags pays nothing: the wait is for mail that
    # has just gone out, not for every sync.
    order.clear()
    monkeypatch.setattr(agent_sync, "drain_actions", lambda *_a: (3, 0, 0))
    agent_sync.sync_once(AccountCfg(), Cfg(), reconcile=False)
    assert not [o for o in order if o[0] == "slept"]


class SilentBridge(RecheckBridge):
    """Bridge before it has loaded the account: connected, and listing nothing."""

    def list_folders(self):
        return []


def test_a_server_that_lists_no_folders_removes_no_folders(monkeypatch, capsys):
    """The worst version of the same mistake: an empty LIST would prune every
    folder for the account, and with the last placement of each message, the
    mail itself."""
    spy = RecheckIngest(None)
    monkeypatch.setattr(agent_sync, "ingest", spy)
    monkeypatch.setattr(agent_sync, "Bridge", lambda _a: SilentBridge())
    monkeypatch.setattr(agent_sync, "SessionLocal", lambda: DB())
    monkeypatch.setattr(agent_sync, "drain_actions", lambda *_a: (0, 0, 0))

    agent_sync.sync_once(AccountCfg(), Cfg())

    # The pass still calls through — ingest.prune_mailboxes is the one that
    # refuses an empty set (see core.ingest) — but it says what happened.
    assert spy.pruned == [set()]
    assert "listed no folders" in capsys.readouterr().out
