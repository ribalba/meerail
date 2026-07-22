"""Unit coverage for how the status panel classifies an account's agent.

Pure unit test: `account_state` reads nothing but the fields on the row, so a
stub account stands in for the ORM object and no database is needed.
"""

from datetime import timedelta

from app.syncstate import STALE_AFTER_HEALTHY, account_state
from core.models import utcnow

NOW = utcnow()


class Acc:
    """Minimal stand-in for an Account row."""

    email = "didi@example.org"

    def __init__(self, **kw):
        self.last_agent_seen = NOW
        self.last_error = None
        self.last_error_at = None
        self.backfill_complete = True
        self.sync_progress = None
        self.__dict__.update(kw)


def progress(active=True, age_seconds=1):
    stamp = (NOW - timedelta(seconds=age_seconds)).isoformat()
    return {"active": active, "updated_at": stamp, "folder": "Archive",
            "folder_index": 11, "folder_count": 46}


def state(acc, last_ingest=None):
    return account_state(acc, last_ingest, NOW)[0]


def test_healthy_account_is_ok():
    assert state(Acc()) == "ok"


def test_never_seen():
    assert state(Acc(last_agent_seen=None)) == "never"


def test_recorded_error_with_no_open_pass_is_failing():
    acc = Acc(last_error="BrokenPipeError(32, 'Broken pipe')")
    assert state(acc) == "failing"


def test_live_backfill_outranks_a_stale_error():
    """The regression this pair of fixes is for.

    An account part-way through its first sync had hit a transient error, and
    the row still carried it because `last_error` was only cleared by a
    *completed* pass. On a large mailbox that left the panel reading "failing"
    for half an hour while the bar beside it advanced.
    """
    acc = Acc(last_error="BrokenPipeError(32, 'Broken pipe')",
              backfill_complete=False, sync_progress=progress())
    assert state(acc) == "backfilling"


def test_wedged_pass_does_not_suppress_the_error():
    """An open pass only outranks the error while it is still moving."""
    acc = Acc(last_error="BrokenPipeError(32, 'Broken pipe')",
              backfill_complete=False,
              sync_progress=progress(age_seconds=STALE_AFTER_HEALTHY + 60))
    assert state(acc) == "failing"


def test_finished_pass_does_not_suppress_the_error():
    acc = Acc(last_error="boom", sync_progress=progress(active=False))
    assert state(acc) == "failing"


def test_backfill_without_progress_blob_still_reads_backfilling():
    assert state(Acc(backfill_complete=False)) == "backfilling"


def test_malformed_progress_blob_is_ignored():
    for blob in ({"active": True}, {"active": True, "updated_at": "not a date"},
                 "not a dict", []):
        acc = Acc(last_error="boom", sync_progress=blob)
        assert state(acc) == "failing"


def test_offline_beats_everything():
    acc = Acc(last_agent_seen=NOW - timedelta(hours=2), sync_progress=progress())
    assert state(acc) == "offline"
