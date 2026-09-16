"""The outbox staging area: files attached to mail that has not been sent yet.

Split out of `routers/compose.py` for the reason app/syncstate.py is split out
of its router. Compose pulls in the multipart parser and e-mail validation,
which the test venv does not carry (the server runs in Docker and the suite
talks to it over HTTP), and what lives here decides which of the user's files
get deleted. That is worth unit tests that need no stack behind them, so the
rules sit in a module they can import.

Nothing here answers a request. The HTTP side, turning a bad id into a 400,
stays in the router.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import get_settings
from core.database import SessionLocal
from core.models import Outbound

settings = get_settings()


def staged_path(staging_id) -> Path | None:
    """The file a staging id names, or None if it cannot name one in the
    staging area.

    A staging id is "<uuid hex>__<safe filename>", and anything that is not a
    bare basename is refused: the id arrives from the browser and from rows in
    the database, and is then used to read and to delete files. The except is
    wider than it looks like it needs to be because a NUL byte in a path is a
    ValueError from the OS layer rather than an OSError, and one odd id must not
    turn into a 500, or into a sweep that stops halfway.
    """
    if not isinstance(staging_id, str) or not staging_id:
        return None
    if staging_id != os.path.basename(staging_id) or ".." in staging_id:
        return None
    try:
        root = settings.outbox_dir.resolve()
        path = (settings.outbox_dir / staging_id).resolve()
    except (OSError, ValueError):
        return None
    return path if path.parent == root else None


def chip_ids(attachments) -> list[str]:
    """The staging ids an ``Outbound.attachments`` value refers to.

    Tolerant on purpose. Drafts store chip dicts and queued rows store bare ids,
    and the startup sweep deletes whatever this fails to name, so a shape it
    does not recognise is skipped rather than allowed to raise: one junk entry
    ignored is better than a sweep that cannot tell which files to spare.
    """
    if not isinstance(attachments, list):
        return []
    ids: list[str] = []
    for entry in attachments:
        sid = entry.get("id") if isinstance(entry, dict) else entry
        if isinstance(sid, str) and sid:
            ids.append(sid)
    return ids


# How long a staged file may sit unclaimed before it is swept.
#
# Staging is meant to be brief: a file is written when it is attached and
# removed when the message is sent (/send, which unlinks every path it baked in)
# or when the composer drops it (DELETE /attachments/{id}). Neither happens if
# the composer never finishes (a closed tab, a crashed browser, a laptop lid),
# and nothing else ever looked at the directory, so those files stayed for good.
#
# Forwarding is what makes that matter rather than merely being untidy.
# ``reply_context(mode="forward")`` stages every attachment of the message being
# forwarded *before* the user has decided to send anything, so opening a forward
# of a mail carrying a video and then closing the composer leaves the video on
# disk. Doing that a few times a week is a data directory that grows forever
# with files nothing can reach.
#
# A day, because the only thing the age has to clear is a composer someone left
# open, including one left open overnight. A staged file is written once and
# then only read at /send, so mtime is a fair reading of "nothing has claimed
# this".
#
# Drafts are the exception the age cannot see. A saved draft can sit for a week
# with its chips pointing at files staged the day it was written, and those are
# claimed even though nothing has touched them since. So the sweep is handed the
# ids every draft still references (draft_staging_ids) and leaves those alone
# whatever their age; they go when the draft is sent or discarded.
STAGING_TTL_SECONDS = 24 * 3600


def sweep_outbox_staging(ttl_seconds: int = STAGING_TTL_SECONDS,
                         keep: set[str] | frozenset[str] = frozenset()) -> int:
    """Delete staged attachments nothing came back for. Returns how many.

    `keep` is the set of staging ids (file names) to spare regardless of age,
    which in practice is every file a draft still references. The caller has to
    be sure of it: an id missing from it is an old file this deletes, and for a
    draft that is an attachment gone from under a message the user had not
    finished. sweep_at_startup therefore skips the sweep entirely when the set
    cannot be read, rather than calling this with an empty one.

    Called at startup rather than on a timer, after init_db so the drafts can be
    read. The files are only orphaned by a composer that never finished, and a
    sweep per process start is often enough to stop that growing: anything a
    composer still has open is younger than the TTL, and anything a draft still
    wants is in `keep`. It costs a directory listing on a directory that holds
    little beyond the attachments of saved drafts.

    Best-effort throughout: this runs before the app serves anything, and a
    permission error or a file that vanishes underneath us is not a reason to
    refuse to start. Anything it cannot deal with is simply left, and the next
    start tries again.
    """
    cutoff = time.time() - ttl_seconds
    swept = 0
    try:
        entries = list(settings.outbox_dir.iterdir())
    except OSError:
        return 0
    for path in entries:
        try:
            if path.name in keep:
                continue
            if not path.is_file() or path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
            swept += 1
        except OSError:
            continue
    return swept


def draft_staging_ids(db: Session) -> set[str]:
    """Every staging id a saved draft still references: the sweep's keep set.

    Lets a database error propagate, deliberately. When the drafts cannot be
    read, the only honest answer to "which files do they need" is "unknown", and
    an empty set would say "none" and let the sweep delete them.
    """
    rows = db.execute(select(Outbound.attachments).where(Outbound.state == "draft")).scalars()
    return {sid for attachments in rows for sid in chip_ids(attachments)}


def sweep_at_startup(session_factory=SessionLocal) -> int | None:
    """The startup sweep, sparing every file a draft needs. Returns how many
    files went, or None when the sweep was skipped.

    Skipped on a start where the drafts cannot be read, for any reason at all.
    Sweeping with an empty keep set would delete the attachments of every draft
    more than a day old, which is losing the user's work to tidy a directory;
    skipping costs a few stale files until the next start that can read them.
    """
    try:
        with session_factory() as db:
            keep = draft_staging_ids(db)
    except Exception as exc:  # noqa: BLE001 - any failure means "unknown", never "none"
        print(f"[startup] staging sweep skipped, could not read drafts: {exc!r}", flush=True)
        return None
    return sweep_outbox_staging(keep=keep)
