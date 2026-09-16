"""Unit coverage for the outbox staging area: which files the startup sweep
deletes, and which ids are allowed to name a file at all.

Pure unit test: app/staging.py reads nothing but a directory and whatever a
session hands back, so the module's settings are swapped for a stand-in pointing
at a temporary directory and the session for a stub. No server and no database.

The sweep is the one piece of meerail that deletes the user's files on its own
initiative, and drafts made that dangerous. A draft can sit for a week with its
attachments staged on the day it was written, so age alone would take them; what
these pin down is that the keep set spares them, and that a start which cannot
read the drafts does not sweep at all rather than sweeping with nothing kept.
"""

import os
import time
from types import SimpleNamespace

import pytest

from app import staging

DAY = 24 * 3600


@pytest.fixture
def area(tmp_path, monkeypatch):
    """A staging directory of our own, where the module will look for it."""
    monkeypatch.setattr(staging, "settings", SimpleNamespace(outbox_dir=tmp_path))
    return tmp_path


def stage(directory, name: str, age_seconds: float):
    path = directory / name
    path.write_bytes(b"staged bytes")
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


class Session:
    """Stands in for a SQLAlchemy session: a context manager whose execute()
    yields the `attachments` values of the draft rows."""

    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, _statement):
        return SimpleNamespace(scalars=lambda: iter(self.rows))


# --- sweep_outbox_staging ----------------------------------------------------


def test_an_old_file_a_draft_keeps_survives_the_sweep(area):
    kept = stage(area, "aaaa__draft.pdf", age_seconds=7 * DAY)
    orphan = stage(area, "bbbb__abandoned.pdf", age_seconds=2 * DAY)
    fresh = stage(area, "cccc__just-attached.pdf", age_seconds=60)

    swept = staging.sweep_outbox_staging(keep={kept.name})

    assert swept == 1
    assert kept.exists()
    assert not orphan.exists()
    assert fresh.exists()


def test_without_a_keep_set_the_sweep_goes_by_age_alone(area):
    old = stage(area, "dddd__old.txt", age_seconds=DAY + 60)
    young = stage(area, "eeee__young.txt", age_seconds=DAY - 60)

    assert staging.sweep_outbox_staging() == 1
    assert not old.exists() and young.exists()


def test_the_sweep_leaves_directories_alone(area):
    sub = area / "not-a-staged-file"
    sub.mkdir()
    stamp = time.time() - 3 * DAY
    os.utime(sub, (stamp, stamp))

    assert staging.sweep_outbox_staging() == 0
    assert sub.is_dir()


def test_a_missing_staging_directory_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(staging, "settings", SimpleNamespace(outbox_dir=tmp_path / "gone"))
    assert staging.sweep_outbox_staging() == 0


# --- sweep_at_startup --------------------------------------------------------


def test_startup_spares_every_file_the_drafts_reference(area):
    chip = stage(area, "1111__chip.pdf", age_seconds=5 * DAY)
    bare = stage(area, "2222__bare-id.pdf", age_seconds=5 * DAY)
    orphan = stage(area, "3333__orphan.pdf", age_seconds=5 * DAY)
    rows = [
        [{"id": chip.name, "filename": "chip.pdf", "size": 12, "content_type": ""}],
        [bare.name],
        # Junk a draft should never hold, and the sweep must step around.
        None, "not a list", [{"filename": "no id"}, {"id": 42}, 7, None],
    ]

    swept = staging.sweep_at_startup(session_factory=lambda: Session(rows))

    assert swept == 1
    assert chip.exists() and bare.exists()
    assert not orphan.exists()


def test_startup_does_not_sweep_when_the_drafts_cannot_be_read(area):
    """An empty keep set would read as "no draft needs anything" and delete
    every draft attachment older than a day. Unknown is not none."""
    old = stage(area, "4444__draft-attachment.pdf", age_seconds=30 * DAY)

    def broken():
        raise RuntimeError("database is not there")

    assert staging.sweep_at_startup(session_factory=broken) is None
    assert old.exists()


def test_startup_does_not_sweep_when_the_query_fails(area):
    old = stage(area, "5555__draft-attachment.pdf", age_seconds=30 * DAY)

    class Failing(Session):
        def execute(self, _statement):
            raise RuntimeError("relation does not exist")

    assert staging.sweep_at_startup(session_factory=lambda: Failing([])) is None
    assert old.exists()


# --- ids ---------------------------------------------------------------------


def test_a_staging_id_names_a_file_directly_inside_the_area(area):
    assert staging.staged_path("abcd__report.pdf") == (area / "abcd__report.pdf").resolve()


@pytest.mark.parametrize("bad", [
    "../escape", "..", ".", "", "sub/dir.txt", "/etc/passwd", "a\x00b",
    None, 42, {"id": "abcd__x"},
])
def test_anything_else_names_nothing(area, bad):
    assert staging.staged_path(bad) is None


def test_chip_ids_reads_both_shapes_and_skips_junk():
    assert staging.chip_ids([
        {"id": "a__one", "filename": "one"}, "b__two", {"id": ""}, {"id": None},
        {"filename": "no id"}, 3, None, ["nested"],
    ]) == ["a__one", "b__two"]
    assert staging.chip_ids(None) == []
    assert staging.chip_ids({"id": "a__one"}) == []
