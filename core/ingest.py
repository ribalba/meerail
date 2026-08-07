"""Mail ingest orchestration — owned by the agent, executed against the DB.

This is the whole write path for incoming mail, in one place: discover folders,
work out which UIDs are new, parse and store them, reconcile flags, prune what
vanished, and extract attachment text via Tika. It used to be split across an
HTTP protocol (agent -> server) plus a background worker; now the agent calls
these functions directly and the web app only reads what they produce.

Every function takes a Session and leaves committing to the caller, so a whole
folder pass can be one transaction. The two exceptions are extract_pending and
thumb_pending, which commit once internally so that their slow per-attachment
work does not run inside a transaction; see _release_before_slow_work.
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from . import events
from .mail import thumbs, tika
from .mail.parse import canonical_message_id, header_identity, strip_nuls
from .mail import store
from .mail.store import (
    find_message_by_message_id,
    ingest_location_only,
    ingest_raw,
    is_pending,
    move_in_flight,
    rebuild_search_text,
    recompute_counts,
    replace_content,
    strip_content,
)
from .models import (
    Account, Attachment, Mailbox, Message, MessageLocation, PendingAction, Setting, utcnow,
)

# Map an IMAP SPECIAL-USE flag / folder name to a meerail mailbox role.
_ROLE_BY_FLAG = {
    "\\sent": "sent",
    "\\drafts": "drafts",
    "\\junk": "junk",
    "\\trash": "trash",
    "\\archive": "archive",
    "\\all": "all",
    "\\flagged": "flagged",
}

EXTRACT_BATCH = 8

# Smaller than EXTRACT_BATCH: rendering is CPU-bound and in-process, where Tika
# calls are network waits on another container.
THUMB_BATCH = 4

# Larger than either: this is SQL only — no Tika, no rendering — and the first
# run after a window is configured has a whole mailbox's backlog to walk.
PRUNE_BATCH = 200

# Where the agent publishes its content window for the app to read.
CONTENT_WINDOW_KEY = "content_window_months"


def derive_role(imap_name: str, role_hint: str = "") -> str:
    hint = (role_hint or "").strip().lower()
    if hint in _ROLE_BY_FLAG:
        return _ROLE_BY_FLAG[hint]
    if imap_name.upper() == "INBOX":
        return "inbox"
    leaf = imap_name.rsplit("/", 1)[-1].lower()
    return {"sent": "sent", "drafts": "drafts", "draft": "drafts", "trash": "trash",
            "junk": "junk", "spam": "junk", "archive": "archive"}.get(leaf, "custom")


def _leaf(imap_name: str) -> str:
    return imap_name.rsplit("/", 1)[-1]


def get_or_create_account(db, email: str) -> Account:
    """Look up an account by address, registering it on first sight so a newly
    configured agent shows up in the UI without a manual add."""
    normalized = email.strip().lower()
    acc = db.execute(select(Account).where(Account.email == normalized)).scalar_one_or_none()
    if acc is None:
        acc = Account(email=normalized, label=normalized.split("@")[0])
        db.add(acc)
        db.flush()
        events.publish({"type": "accounts", "account": normalized})
    acc.last_agent_seen = utcnow()
    return acc


def record_presentation(db, account: Account, values: dict[str, str]) -> None:
    """Apply the display fields the agent's config pins, and record which they are.

    `values` is ``AccountConfig.presentation()``: only the fields actually
    written in meerail.toml. They are stamped onto the row on every pass — the
    file is the source of truth for what it names, so an edit there wins over
    whatever Settings last saved — and their names go into ``config_fields``,
    which is what the web app reads to lock them (it may be on another machine
    and never see the file).

    Dropping a key from the file drops it from ``config_fields`` on the next
    pass and nothing else: the field becomes editable again, holding the value
    the file last gave it.
    """
    managed = sorted(values)
    changed = False
    for field, value in values.items():
        if getattr(account, field) != value:
            setattr(account, field, value)
            changed = True
    if list(account.config_fields or []) != managed:
        account.config_fields = managed
        changed = True
    # A pinned footer opts out of the default-footer backfill exactly as a saved
    # one does. Without this, `footer = ""` in the file would have DEFAULT_FOOTER
    # written back over it at every server start, and it would stand there until
    # the agent's next pass undid it.
    if "footer" in values and not account.footer_customized:
        account.footer_customized = True
    if changed:
        events.publish({"type": "accounts", "account": account.email})


def _delete_orphans(db, message_pks: set[int]) -> None:
    """Delete content rows left with no folder placement."""
    for pk in message_pks:
        remaining = db.scalar(
            select(MessageLocation.id).where(MessageLocation.message_pk == pk).limit(1)
        )
        if not remaining:
            msg = db.get(Message, pk)
            if msg is not None:
                db.delete(msg)


def register_folder(db, account: Account, imap_name: str, role_hint: str = "",
                    uidvalidity: int | None = None, uidnext: int | None = None,
                    sort_order: int = 0) -> Mailbox:
    """Upsert a mailbox row and return it (carrying the UID cursor)."""
    mb = db.execute(
        select(Mailbox).where(Mailbox.account_id == account.id, Mailbox.imap_name == imap_name)
    ).scalar_one_or_none()
    if mb is None:
        mb = Mailbox(
            account_id=account.id,
            imap_name=imap_name,
            display_name=_leaf(imap_name),
            role=derive_role(imap_name, role_hint),
            sort_order=sort_order,
        )
        db.add(mb)
        # Mirrors prune_mailboxes' removal event, so a folder appearing on the
        # server — including one the UI just asked the agent to create — reaches
        # open sidebars without waiting for the next unrelated event.
        events.publish({"type": "folders", "account": account.email, "added": 1})
    else:
        # A UIDVALIDITY change invalidates every UID-to-message placement, not
        # just the cursor: the numbers we hold no longer refer to the messages
        # they were recorded against.
        #
        # What that does NOT license is deleting the mail. This used to wipe the
        # folder — every placement, and with the last placement of each message
        # its content — and then re-fetch from scratch. Bridge changes
        # UIDVALIDITY for its own reasons (a re-login, a reinstall, a rebuilt
        # local cache), so an ordinary Tuesday could empty a mailbox and refill
        # it over the next hour. On a machine that then went offline, or where
        # the fetch failed, or where the mail had since been deleted upstream,
        # it did not refill at all.
        #
        # Rewinding the cursor gets to the same place without the hole. The pass
        # re-walks every UID; content already held is matched by Message-ID and
        # only gains a placement, so nothing is re-downloaded and nothing is
        # gone in the meantime. The stale placements are removed by the ordinary
        # vanished sweep, which deletes only what the server has positively
        # confirmed is not there (see sync._uid_list_is_trustworthy). Until then
        # a message can appear twice in one folder, which is the right way round
        # for this trade: a duplicate is visible and self-correcting, a deletion
        # is neither.
        if mb.uidvalidity is not None and uidvalidity is not None and mb.uidvalidity != uidvalidity:
            mb.last_uid = 0
        if mb.role == "custom":
            mb.role = derive_role(imap_name, role_hint)
    if uidvalidity is not None:
        mb.uidvalidity = uidvalidity
    if uidnext is not None:
        mb.uidnext = uidnext
    db.flush()
    return mb


def record_known(db, account: Account, mailbox: Mailbox, uid: int, flags: dict,
                 message_id: str | None, size: int | None = None, date=None,
                 headers: bytes | None = None, content_wanted: bool = False) -> bool:
    """Record a placement for content this account already holds, without
    fetching the message. True if it matched.

    No longer used by a sync pass, and kept for what it documents as much as for
    what it does. It was the shortcut that made a label server's backfill cheap —
    the same mail arrives once per label, and matching it by Message-ID meant the
    body crossed the wire once. The trouble is that every fact it can match on is
    a header, and headers are written by whoever sent the message: two different
    mails agreeing on all of them were taken to be one, and the second body was
    then never fetched by anything.

    A pass now fetches every UID and lets the bytes decide (core.mail.store's
    same_message, which compares content hashes). What that costs is bandwidth,
    not storage: mail already held still gains only a placement row.
    """
    if not message_id:
        return False
    return ingest_location_only(db, account, mailbox, uid, flags, message_id,
                                size=size, date=date, headers=headers,
                                content_wanted=content_wanted)


def store_message(db, account: Account, mailbox: Mailbox, uid: int, flags: dict,
                  raw: bytes, received=None) -> bool:
    """Parse and store raw MIME. Returns True if this created new content.

    ``received`` is the server's own delivery time for this UID (INTERNALDATE),
    which is what the content window is measured from — see
    prune_expired_content for why it is not the Date header.
    """
    _msg, created = ingest_raw(db, account, mailbox, uid, flags, raw, received=received)
    return created


def store_headers(db, account: Account, mailbox: Mailbox, uid: int, flags: dict,
                  header_bytes: bytes, size_bytes: int | None = None,
                  received=None) -> bool:
    """Store a message's headers with no content, for mail outside the window.

    The caller has decided (from the Date header, before spending a fetch on the
    body) that this message is too old to hold content for. What lands still
    lists, threads, sorts and answers a search for its subject or sender — it
    simply has no body to open. Returns True if this created new content.
    """
    _msg, created = ingest_raw(db, account, mailbox, uid, flags, header_bytes,
                               headers_only=True, size_bytes=size_bytes,
                               received=received)
    return created


def content_cutoff(months: int) -> datetime | None:
    """The oldest date whose content is still kept, or None for "keep it all".

    Calendar months rather than a fixed number of days, because that is what
    "keep two years" means to the person who typed 24 — and a day-count answer
    drifts against the calendar by nearly a week a year.
    """
    if months <= 0:
        return None
    now = utcnow()
    total = now.year * 12 + (now.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    # Clamp: the 31st of a month the target does not have (31 Mar, 6 months back).
    day = min(now.day, calendar.monthrange(year, month)[1])
    return now.replace(year=year, month=month, day=day)


def prune_expired_content(db, cutoff: datetime, limit: int = PRUNE_BATCH) -> int:
    """Strip the content of stored messages that have aged out of the window.

    Returns how many were stripped, so callers can loop until it returns 0. The
    window slides, so this has to keep running — it is not a one-off migration:
    every day moves the cutoff forward over another day's worth of mail.

    Age is measured from when the mail *arrived* (Message.date_received, which
    the agent fills in from the server's INTERNALDATE), not from its Date header.
    The header is written by whoever sent the message, so a window that read it
    could be aimed: a sender who dates a message 1998 gets its body stripped on
    the pass that stores it, and the recipient has a mail they can list and never
    open. Nobody can backdate the moment their message reached the server.

    Messages with no arrival time are left alone. Their age is unknown, and the
    safe reading of "unknown" is to keep what we already have rather than throw
    away content on a guess.
    """
    stale = db.execute(
        select(Message)
        .where(
            Message.content_status == "full",
            Message.date_received.is_not(None),
            Message.date_received < cutoff,
        )
        .limit(limit)
    ).scalars().all()
    for msg in stale:
        strip_content(db, msg)
    if stale:
        events.publish({"type": "pruned", "messages": len(stale)})
    return len(stale)


def record_content_window(db, months: int) -> None:
    """Publish the agent's window setting for the web app to read.

    A split deployment puts the app on another machine, where it has no copy of
    meerail.toml — the two share nothing but the database — and it needs the
    number to tell someone *why* a body is missing. Writing it here keeps one
    source of truth: agent.content_window_months, echoed into the database by
    the process that actually applies it.
    """
    value = str(max(0, int(months)))
    # Upsert rather than get-then-add: every account thread calls this once per
    # pass with its own session, and on a fresh database they all read "no row"
    # and all insert. One wins, the rest die on settings_pkey and get retried as
    # a sync failure. The WHERE keeps updated_at still when nothing changed.
    stmt = pg_insert(Setting).values(key=CONTENT_WINDOW_KEY, value=value, updated_at=utcnow())
    db.execute(stmt.on_conflict_do_update(
        index_elements=[Setting.key],
        set_={"value": value, "updated_at": utcnow()},
        where=Setting.value != value,
    ))


def note_ingested(account: Account, mailbox: Mailbox, stored: int) -> None:
    """Tell the UI new mail landed. Called once per batch, not per message, so a
    large backfill doesn't flood the notification channel."""
    if stored:
        events.publish({"type": "messages", "account": account.email,
                        "folder": mailbox.imap_name, "stored": stored})


def touch_agent(db, account: Account) -> None:
    """Mark the agent as alive, without claiming a pass got anywhere.

    ``get_or_create_account`` stamps this when a pass opens and ``record_sync``
    again when it closes, which is all a pass measured in seconds ever needed.
    A pass that runs for hours has to say so in between: the status panel
    (app/syncstate.py) judges liveness by this column alone, and calls an
    account offline after three minutes of silence — so without a stamp from
    inside the long loops, the agent working hardest is the one reported dead.
    """
    account.last_agent_seen = utcnow()


def set_progress(db, account: Account, progress: dict | None) -> None:
    """Record how far the agent has got in this pass.

    Assigns a whole dict rather than mutating in place: SQLAlchemy does not
    track in-place changes to a plain JSONB value, so a mutated dict would sit
    in the session looking saved and never reach Postgres.

    Deliberately publishes no event. This is written once per batch during a
    backfill, which on a large mailbox is often enough that a NOTIFY per call
    would be a meaningful share of the channel's traffic — and the only reader
    is the status panel, which already polls while it is open.
    """
    account.sync_progress = progress


def advance_cursor(db, mailbox: Mailbox, last_uid: int) -> None:
    if last_uid > mailbox.last_uid:
        mailbox.last_uid = last_uid
    recompute_counts(db, mailbox)
    events.publish({"type": "cursor", "folder": mailbox.imap_name,
                    "last_uid": mailbox.last_uid, "total": mailbox.total_count,
                    "unread": mailbox.unread_count})


def update_flags(db, mailbox: Mailbox, items: list[dict]) -> int:
    """Apply flag state for already-synced UIDs. items: [{uid, flags}].

    Returns how many placements actually changed — not how many the folder held
    a row for, which is what it used to count and what made a quiet mailbox
    expensive. The reconcile sweep calls this for every UID in the folder every
    time it runs, so on a mailbox nobody has touched since the last sweep the
    honest answer is zero for every chunk; counting matches instead published a
    "flags" event per chunk with nothing behind it, and each of those costs
    every connected client a full reload. A 35k-message folder did that ~1400
    times a sweep.

    Both writes below hang off the same count. With nothing changed there is
    nothing to recount either, and recompute_counts is two aggregates over the
    whole folder — the sweep's largest per-chunk cost once the event is gone.

    Keywords compare as a set: servers are free to list them in any order, and
    treating a reordering as a change would put the storm straight back.
    """
    if not items:
        return 0
    locs = db.execute(
        select(MessageLocation).where(
            MessageLocation.mailbox_id == mailbox.id,
            MessageLocation.imap_uid.in_([item["uid"] for item in items]),
        )
    ).scalars().all()
    by_uid = {loc.imap_uid: loc for loc in locs}

    changed = 0
    for item in items:
        loc = by_uid.get(item["uid"])
        if loc is None:
            continue
        f = item["flags"]
        keywords = f.get("keywords") or []
        after = (bool(f.get("seen")), bool(f.get("flagged")), bool(f.get("answered")),
                 bool(f.get("draft")), bool(f.get("deleted")))
        before = (loc.seen, loc.flagged, loc.answered, loc.draft, loc.deleted)
        if before == after and sorted(loc.keywords or []) == sorted(keywords):
            continue
        (loc.seen, loc.flagged, loc.answered, loc.draft, loc.deleted) = after
        loc.keywords = keywords
        changed += 1

    if not changed:
        return 0
    recompute_counts(db, mailbox)
    events.publish({"type": "flags", "folder": mailbox.imap_name,
                    "updated": changed, "unread": mailbox.unread_count})
    return changed


# --- content that is stored, but short ---------------------------------------
#
# Content is fetched once and never looked at again: the cursor moves past the
# UID and the reconcile sweep reads flags. That holds as long as what the server
# answered was the whole message, and once it was not.
#
# Proton creates a message before it links the attachments to it, and /send asks
# the agent to run immediately so the outbox empties promptly — so the pass that
# sends your mail can read the copy of it back within the same second and get a
# body with the attachment missing. It stores as complete, because nothing about
# those bytes says otherwise, and the attachment is gone from the mailbox for
# good while sitting in the account the whole time.
#
# RFC822.SIZE is the tell, and the sweep is already fetching it. Only *short* is
# suspect: a server that reports a size a little under what it hands over is
# doing arithmetic we should not read as damage.


def find_short_content(db, mailbox: Mailbox, sizes: dict[int, int]) -> list[int]:
    """Which of these UIDs hold less content than the server says they should.

    Only messages whose content we believe we hold in full are candidates.
    Headers-only mail (outside the content window when it was seen) and pruned
    mail (the window slid past it) are *meant* to be short of the server's size,
    and re-fetching either would be a way to quietly undo the window.
    """
    if not sizes:
        return []
    rows = db.execute(
        select(MessageLocation.imap_uid, Message.size_bytes)
        .join(Message, Message.id == MessageLocation.message_pk)
        .where(
            MessageLocation.mailbox_id == mailbox.id,
            MessageLocation.imap_uid.in_(list(sizes)),
            Message.content_status == "full",
        )
    ).all()
    return sorted(uid for uid, stored in rows if sizes.get(uid, 0) > (stored or 0))


def restore_content(db, mailbox: Mailbox, uid: int, raw: bytes) -> bool:
    """Re-store the content of this placement's message. False if it is gone."""
    msg = db.execute(
        select(Message)
        .join(MessageLocation, MessageLocation.message_pk == Message.id)
        .where(MessageLocation.mailbox_id == mailbox.id, MessageLocation.imap_uid == uid)
    ).scalars().first()
    if msg is None:
        return False
    replace_content(db, msg, raw)
    return True


def unplaced_uids(db, mailbox: Mailbox, present_uids: list[int]) -> list[int]:
    """Which of the UIDs the server lists this folder holds no placement for.

    The mirror image of prune_vanished, and the half that was missing. A pass
    only ever *reads* below a folder's cursor — new mail is fetched above
    last_uid, update_flags skips a UID it has no row for, and the sweep's one
    write is the prune, which only deletes. So a placement lost below the cursor
    stayed lost: nothing went back for it.

    Reads the whole folder's UIDs rather than filtering by present_uids in SQL.
    The comparison set is the same one prune_vanished builds a row at a time,
    and an IN list of forty thousand parameters is the slower way to ask.
    """
    # autoflush is off and this is a raw SELECT: a caller that has just written
    # placements (the flag sweep runs before this) would otherwise be compared
    # against the rows as they were before it.
    db.flush()
    placed = set(db.scalars(
        select(MessageLocation.imap_uid).where(MessageLocation.mailbox_id == mailbox.id)
    ))
    return [uid for uid in present_uids if uid not in placed]


def has_move_in_flight(db, account: Account, message_id: str | None,
                       headers: bytes | None = None, date=None) -> bool:
    """Is this message — one the folder lists and we hold no placement for — in
    the middle of being moved?

    Restoring a placement for one would undo the move: the source placement is
    deleted the moment the key is pressed, and the server goes on listing the
    UID until the agent applies the move and the server catches up.

    Usually the Message-ID answers it. Where there is none — a stripped header, a
    mailer that never wrote one, and both do occur — "no id" used to be read as
    "never seen before, so never moved", which is a guess about mail we can
    perfectly well identify by other means. The repair then put the message
    straight back in the folder it had just been archived out of, and the user
    got a message that would not stay filed.

    So the queue is asked instead. Only mail with a move already queued is a
    candidate, which is a handful of rows, and among those a match on sender,
    subject and send time is as much as this file claims anywhere else (see
    core.mail.store.same_message). ``headers`` is the header block the caller
    already fetched to get here, and ``date`` the send time from the same pass.

    Every row that could be the message is asked, not the first one found. An id
    can be worn by two messages, and "is the one I happened to pick being moved"
    is not the question — a move in flight on either of them is a reason to leave
    this UID alone, since putting a placement back on the wrong guess undoes a
    move the user made.
    """
    if message_id:
        rows = db.execute(
            select(Message.id).where(Message.account_id == account.id,
                                     Message.message_id == canonical_message_id(message_id))
        ).scalars().all()
        return any(move_in_flight(db, pk) for pk in rows)

    identity = header_identity(headers)
    if identity is None:
        return False                      # nothing to compare; nothing to protect
    from_addr, subject_norm = identity
    q = (
        select(Message.id)
        .join(PendingAction, PendingAction.message_pk == Message.id)
        .where(
            Message.account_id == account.id,
            Message.message_id.is_(None),
            Message.from_addr == from_addr,
            Message.subject_norm == subject_norm,
            PendingAction.type.in_(("move", "delete")),
            # The same rule move_in_flight applies above, written out for the
            # join: queued or being applied, and not so far into the refusals
            # that the local view has stopped being the one believed. A row the
            # agent dropped, or one that has been refused for a quarter of an
            # hour, is not a move that is about to land — and treating it as one
            # is what left a placement unrepairable for good.
            PendingAction.status.not_in(("done",) + store.SETTLED),
            PendingAction.attempts < store.STUCK_AFTER,
        )
    )
    if date is not None:
        q = q.where(Message.date_sent == date)
    return db.scalar(q.limit(1)) is not None


def prune_vanished(db, mailbox: Mailbox, present_uids: list[int]) -> int:
    """Drop placements whose UID is gone from the folder, and any message left
    with no placement at all.

    Placements the user has made but the server has not seen yet are exempt.
    They carry a UID the server was never told about (see store.pending_uid), so
    "the server did not list it" says nothing about them — and reading it as a
    deletion would take the archived message straight back out of the folder it
    was just filed into, which is the disappearance this whole mechanism exists
    to prevent. The move that created them is still in the queue; when it lands,
    the real placement replaces them.
    """
    present = set(present_uids)
    locs = db.execute(
        select(MessageLocation).where(MessageLocation.mailbox_id == mailbox.id)
    ).scalars().all()
    affected: set[int] = set()
    removed = 0
    for loc in locs:
        if is_pending(loc):
            continue
        if loc.imap_uid not in present:
            affected.add(loc.message_pk)
            db.delete(loc)
            removed += 1
    db.flush()
    _delete_orphans(db, affected)
    recompute_counts(db, mailbox)
    if removed:
        events.publish({"type": "present", "folder": mailbox.imap_name,
                        "removed": removed, "total": mailbox.total_count})
    return removed


# How long a folder has to be absent from the server's LIST before its mail is
# removed here. Not politeness — evidence. See prune_mailboxes.
MISSING_GRACE = timedelta(hours=1)


def deferred_folders(db, account: Account) -> list[str]:
    """Folders the server has stopped listing that are being held rather than
    removed. What the agent warns about — see prune_mailboxes."""
    return list(db.execute(
        select(Mailbox.imap_name).where(Mailbox.account_id == account.id,
                                        Mailbox.missing_since.is_not(None))
        .order_by(Mailbox.imap_name)
    ).scalars().all())


def prune_mailboxes(db, account: Account, present_names: set[str], now=None) -> int:
    """Remove folders the server has stopped listing — once it has stopped
    listing them for long enough to be believed.

    An empty LIST is never taken as evidence at all. Proton Bridge answers LIST
    from whatever it has loaded, and a Bridge that is still starting, signed out,
    or running on a machine that has been offline for days answers it with
    nothing — preflight already warns about exactly that ("the server listed no
    folders"). Acting on that answer would delete every folder for the account
    and, with the last placement of each message, the mail itself: a local
    mailbox wiped because a laptop was opened on a train. There is no reading of
    "the server told me about no folders at all" that means "the user deleted all
    their folders".

    Neither is there such a reading of a *partial* answer, and that is what this
    grace period is for. The same Bridge that answers LIST with nothing while it
    is starting answers it with what it has loaded so far while it is still
    loading — three folders out of twelve, a complete and successful response
    containing a fraction of the mailbox. Treating the other nine as deleted took
    every message that lived only there with them. Nothing about the response
    says which kind it is; the only thing that tells a folder somebody deleted
    from one Bridge has not finished waking up is that the first stays gone.

    So a folder that goes missing is marked and left alone, with all its mail,
    and only removed once it has been absent for MISSING_GRACE — an hour, which
    at a thirty-second poll is a hundred passes agreeing. Coming back clears the
    mark. Being wrong in this direction leaves a folder on screen for an hour
    after it was deleted elsewhere; being wrong in the other direction is mail
    nobody can get back.
    """
    if not present_names:
        return 0
    now = now or utcnow()

    # Anything the server listed is here, whatever it did last pass.
    db.execute(
        update(Mailbox)
        .where(Mailbox.account_id == account.id,
               Mailbox.imap_name.in_(present_names),
               Mailbox.missing_since.is_not(None))
        .values(missing_since=None)
    )

    absent = db.execute(
        select(Mailbox).where(
            Mailbox.account_id == account.id,
            Mailbox.imap_name.not_in(present_names),
        )
    ).scalars().all()

    missing = []
    for mailbox in absent:
        if mailbox.missing_since is None:
            # First pass without it. Nothing is removed on the strength of one
            # answer; the clock starts here.
            mailbox.missing_since = now
        elif now - mailbox.missing_since >= MISSING_GRACE:
            missing.append(mailbox)
    if not missing:
        db.flush()
        return 0

    affected: set[int] = set()
    for mailbox in missing:
        locs = db.execute(
            select(MessageLocation).where(MessageLocation.mailbox_id == mailbox.id)
        ).scalars().all()
        affected.update(loc.message_pk for loc in locs)
        for loc in locs:
            db.delete(loc)
        db.delete(mailbox)
    db.flush()
    _delete_orphans(db, affected)
    events.publish({"type": "folders", "account": account.email,
                    "removed": len(missing)})
    return len(missing)


# How long a message with no placement anywhere is kept before it is collected.
# The window is not for the database's sake — it is the difference between "no
# folder holds this" and "no folder holds this *yet*", and a pass is the unit of
# time in which that changes.
ORPHAN_GRACE = timedelta(hours=6)


def delete_orphan_messages(db, account: Account, now=None, limit: int = 1000) -> int:
    """Collect stored mail that no folder points at any more.

    ``_delete_orphans`` catches the messages whose last placement a prune took,
    which is most of them. The one it cannot see is the placement that was
    *repointed*: a UIDVALIDITY change hands the same numbers out again, the walk
    that follows binds uid 4051 to whatever message now holds it, and the message
    it used to mean is left behind with its body, its attachments and no folder —
    invisible in the UI, counted by nothing, and still occupying the disk it took
    when it arrived. On a mailbox that has been through a few Bridge re-logins
    that is a slow leak of whole messages.

    Deliberately not done at the moment the placement moves. A message can be
    between folders for perfectly good reasons — mid-pass, with the placement
    that will hold it not walked yet; mid-move, though those carry an optimistic
    placement of their own — and deleting on the first look would turn a gap of
    seconds into mail nobody can get back. So this runs at the end of a pass that
    completed, over messages that have been unplaced since well before it
    started, and skips anything a queued action still names: a move waiting for
    the agent is a message on its way somewhere, and the row it names has to be
    there when it arrives.
    """
    now = now or utcnow()
    placed = select(MessageLocation.id).where(MessageLocation.message_pk == Message.id).exists()
    queued = select(PendingAction.id).where(
        PendingAction.message_pk == Message.id, PendingAction.status != "done").exists()
    stale = db.execute(
        select(Message.id).where(
            Message.account_id == account.id,
            Message.updated_at < now - ORPHAN_GRACE,
            ~placed,
            ~queued,
        ).limit(limit)
    ).scalars().all()
    if not stale:
        return 0
    for pk in stale:
        msg = db.get(Message, pk)
        if msg is not None:
            db.delete(msg)
    db.flush()
    events.publish({"type": "pruned", "messages": len(stale)})
    return len(stale)


def request_recheck(db, email: str | None = None) -> list[str]:
    """Flag accounts for a full recheck. Returns the addresses actually flagged.

    ``email`` of None means every account. Raised by the web app; the agent
    picks it up on its next pass and clears it once that pass completes, so the
    request is safe against the agent being down or mid-restart.
    """
    stmt = select(Account)
    if email:
        stmt = stmt.where(Account.email == email.strip().lower())
    accounts = db.execute(stmt).scalars().all()
    now = utcnow()
    for acc in accounts:
        acc.recheck_requested = True
        acc.recheck_requested_at = now
    return [acc.email for acc in accounts]


def take_recheck(db, account: Account) -> datetime | None:
    """The pending recheck request for this account, or None.

    Returns the request's timestamp rather than a bool so the caller can hand it
    back to :func:`clear_recheck` and only clear the request it actually served.
    """
    return account.recheck_requested_at if account.recheck_requested else None


def reset_cursor(db, mailbox: Mailbox) -> None:
    """Rewind a folder so the next pass re-walks it from UID 1.

    Re-ingesting is idempotent — messages dedupe on (account, dedup_key) and
    known content only gains a placement row — so this repairs gaps without
    duplicating anything that survived.
    """
    mailbox.last_uid = 0


def clear_recheck(db, account: Account, requested_at) -> None:
    """Mark a served recheck done. Only called after a full pass has succeeded,
    so a pass that dies partway leaves the request standing and it runs again.

    The timestamp guard matters: a request raised while this pass was already
    walking the mailbox has not been served by it (the folders behind the cursor
    were rewound before that request existed), so it must survive to earn a pass
    of its own.
    """
    db.execute(
        update(Account)
        .where(Account.id == account.id, Account.recheck_requested_at == requested_at)
        .values(recheck_requested=False, recheck_requested_at=None)
    )
    db.expire(account, ["recheck_requested", "recheck_requested_at"])
    events.publish({"type": "agent", "account": account.email, "recheck": "done"})


def clear_agent_error(db, account: Account) -> None:
    """Drop a recorded failure once the agent demonstrably works again.

    Called from two places, and deliberately so. record_sync calls it when a
    pass completes, but a completed pass is a slow proof: the initial backfill
    of a large mailbox runs for many minutes, so an error cleared only there
    stays on screen long after the fault is gone. The agent therefore also calls
    it the moment a pass has connected and logged in, which is the earliest
    point the previous failure is known to be over.
    """
    if account.last_error is None:
        return
    account.last_error = None
    account.last_error_at = None
    events.publish({"type": "agent", "account": account.email, "ok": True})


def record_sync(db, account: Account, backfill_complete: bool | None = None,
                identities: list[tuple[str, str]] | None = None) -> None:
    """Update per-account sync status and the agent-declared sender identities.

    `identities` is (display name, address) pairs — see
    ``AccountConfig.send_identities``. The address is lower-cased for storage
    because everything downstream compares it against message addresses, which
    are lower-cased at parse time; the *name* is stored as written, because it
    is prose shown to a recipient.
    """
    if backfill_complete is not None:
        account.backfill_complete = backfill_complete
    if identities is not None:
        ordered: list[str] = []
        names: dict[str, str] = {}
        for name, addr in [("", account.email), *identities]:
            low = (addr or "").strip().lower()
            if not low:
                continue
            if low not in names:
                ordered.append(low)
            # A later pair naming an address the primary already contributed is
            # how the primary itself gets a name.
            names[low] = (name or "").strip() or names.get(low, "")
        extras = ordered[1:]
        names = {a: n for a, n in names.items() if n}
        if extras != account.send_addresses or names != account.send_names:
            account.send_addresses = extras
            account.send_names = names
            events.publish({"type": "accounts", "account": account.email})
    account.last_sync_at = utcnow()
    # A pass got all the way here, so whatever failed last time is over. Usually
    # already cleared at connect time; this is the backstop for a pass that
    # started before that call existed, or an error recorded mid-pass.
    clear_agent_error(db, account)


def record_agent_error(db, email: str, error: str) -> None:
    """Persist a failed sync pass against an account, for the UI to warn on.

    Called from the agent's retry loop, which has already lost its session, so
    this takes an address rather than a row. Unknown addresses are ignored: an
    account that has never synced has nothing to attach the failure to, and
    inventing one here would put a phantom in the sidebar.

    ``last_agent_seen`` is stamped too. The process is demonstrably running — it
    is the sync that is broken — and keeping the two apart is what lets the UI
    say "failing" instead of the much vaguer "offline".
    """
    acc = db.execute(
        select(Account).where(Account.email == email.strip().lower())
    ).scalar_one_or_none()
    if acc is None:
        return
    now = utcnow()
    acc.last_agent_seen = now
    # The caller passes repr(), which escapes NULs, but this write must never be
    # the thing that fails -- a poisoned error string would roll back the
    # last_agent_seen stamp too and downgrade the UI from "failing" to "offline",
    # blaming the wrong process for a fault we had correctly diagnosed.
    acc.last_error = strip_nuls(error)[:2000]
    acc.last_error_at = now
    events.publish({"type": "agent", "account": acc.email, "ok": False})


def _release_before_slow_work(db) -> None:
    """End the read transaction so the slow phase holds no locks.

    Reading a batch takes ACCESS SHARE on attachments and holds it until the
    transaction ends. Tika calls and thumbnail renders are seconds each, so
    doing them mid-transaction pinned that lock for the length of a whole batch.
    Nothing conflicts with ACCESS SHARE except DDL — which is exactly what
    init_db runs at server startup, and it waits only 5s before giving up. The
    server could not start while the agent was draining a backlog.

    The batch is already materialised as plain tuples by this point, so ending
    the transaction costs nothing: no lazy loads follow, and no ORM state is
    expired out from under the caller.
    """
    db.commit()


_PENDING_QUEUES = {"extract": Attachment.extract_status, "thumb": Attachment.thumb_status}


def pending_attachment_count(db, queue: str) -> int:
    """How many attachments are queued for 'extract' text or 'thumb' previews.

    Only used to say how much work a drain has left. A first run over a real
    mailbox queues thousands of attachments and takes many minutes, and a
    number to count down is the difference between "working" and "wedged".
    """
    return db.scalar(
        select(func.count()).select_from(Attachment)
        .where(_PENDING_QUEUES[queue] == "pending")
    ) or 0


def extract_pending(db, limit: int = EXTRACT_BATCH) -> int:
    """Run Tika over a batch of pending attachments and refresh search text.

    Returns how many were processed, so callers can loop until it returns 0.

    Exception to the module's commit-in-the-caller rule: this commits once
    internally, between reading the batch and doing the slow work. See
    _release_before_slow_work — callers must have no uncommitted changes pending.
    """
    pending = db.execute(
        select(
            Attachment.id,
            Attachment.message_pk,
            Attachment.content,
            Attachment.content_type,
        )
        .where(Attachment.extract_status == "pending")
        .limit(limit)
    ).all()
    if not pending:
        return 0

    _release_before_slow_work(db)

    # Tika round trips happen here, with no transaction open and no lock held.
    extracted: list[tuple[int, int, str]] = []
    rejected: list[tuple[int, int]] = []
    for att_id, message_pk, content, content_type in pending:
        body = tika.extract_text(content or b"", content_type)
        if body is tika.TIMEOUT:
            # Tika took the bytes and never came back. Ask whether the service
            # is still answering at all: if it is, this file is the problem and
            # burning it is the same call as UNPROCESSABLE below — a payload
            # that times out once times out every pass, and leaving it pending
            # parks it at the head of every future batch forever. If Tika is
            # genuinely down, it is not this file's fault, so leave the queue
            # alone and let a later pass retry it.
            if tika.health():
                rejected.append((att_id, message_pk))
                continue
            break
        if body is None:
            # Tika is unavailable. Leave this and the remainder pending so the
            # next sync pass can retry them.
            break
        if body is tika.UNPROCESSABLE:
            # Tika read the bytes and refused them — a truncated or mislabelled
            # file. Retrying is guaranteed to fail again, and leaving it pending
            # parks it at the head of every future batch and stalls the whole
            # queue behind it, so burn it and keep going.
            rejected.append((att_id, message_pk))
            continue
        extracted.append((att_id, message_pk, body))

    if not extracted and not rejected:
        return 0

    touched: set[int] = set()
    for att_id, message_pk in rejected:
        att = db.get(Attachment, att_id)
        if att is None or att.extract_status != "pending":
            continue
        att.extract_status = "error"
        # Still counts as touched: the message's remaining attachments may all
        # be resolved now, and its rollup below needs to see that.
        touched.add(message_pk)

    for att_id, message_pk, body in extracted:
        att = db.get(Attachment, att_id)
        # Re-check rather than trusting the batch: the row was unlocked during
        # extraction, so it may have been pruned as vanished in the meantime.
        if att is None or att.extract_status != "pending":
            continue
        att.extracted_text = body or None
        att.extract_status = "done"
        touched.add(message_pk)

    # autoflush is off on these sessions, so push extracted_text to the DB before
    # rebuild_search_text re-reads it, or it sees stale NULLs.
    db.flush()

    for message_pk in touched:
        msg = db.get(Message, message_pk)
        if msg is None:
            continue
        rebuild_search_text(db, msg)
        still_pending = db.scalar(
            select(Attachment.id)
            .where(Attachment.message_pk == message_pk, Attachment.extract_status == "pending")
            .limit(1)
        )
        if not still_pending:
            msg.extract_status = "done"
    # Count what Tika actually resolved, not what survived to be written: rows
    # that vanished mid-batch are still progress, and reporting 0 would stop the
    # caller's drain loop with real work left pending. Rejections count too —
    # they leave the queue just as permanently as a successful extraction.
    processed = len(extracted) + len(rejected)
    events.publish({"type": "extract", "processed": processed})
    return processed


def thumb_pending(db, limit: int = THUMB_BATCH) -> int:
    """Render previews for a batch of pending attachments.

    Same shape as extract_pending: returns how many were processed, so callers
    loop until it returns 0, and commits once internally before the slow work.
    Unlike extraction this touches no message-level state — a preview is
    per-attachment and feeds nothing downstream.
    """
    # Without the imaging libraries every render would "fail" and burn the whole
    # backlog to 'error', which a later install would not undo. Leave the rows
    # pending instead, so installing the deps is all it takes to pick them up.
    if not thumbs.available():
        return 0

    pending = db.execute(
        select(Attachment.id, Attachment.content, Attachment.content_type)
        .where(Attachment.thumb_status == "pending")
        .limit(limit)
    ).all()
    if not pending:
        return 0

    _release_before_slow_work(db)

    # Rendering happens here, with no transaction open and no lock held.
    rendered = [
        (att_id, thumbs.make_thumb(content or b"", content_type))
        for att_id, content, content_type in pending
    ]

    made = 0
    for att_id, data in rendered:
        att = db.get(Attachment, att_id)
        # See extract_pending: unlocked during rendering, so the row may be gone.
        if att is None or att.thumb_status != "pending":
            continue
        att.thumb = data
        # 'error' rather than 'skipped': should_thumb already said this type was
        # renderable, so a None here means the payload itself was unusable.
        att.thumb_status = "done" if data else "error"
        if data:
            made += 1

    events.publish({"type": "thumb", "processed": len(rendered), "made": made})
    return len(rendered)


def backfill_thumbs(db, limit: int = 5000) -> int:
    """Queue existing attachments for preview rendering. Returns how many.

    Upgrading marks old rows 'skipped' so that turning this feature on does not
    silently kick off a full-mailbox render; this is the opt-in that queues them.
    Bounded per call so a large mailbox can be worked through in chunks.
    """
    # The allowlist has to be part of the query, not a filter applied afterwards:
    # LIMIT over all skipped rows could return a batch that is entirely
    # unrenderable (a mailbox full of .txt), read as "nothing left to do", and
    # stop before reaching the PDFs further down the table.
    # LIKE 'image/png%' rather than equality so parameterised types
    # ("image/png; name=x") still match, mirroring thumbs._norm.
    renderable = or_(*[
        func.lower(Attachment.content_type).like(f"{ct}%") for ct in sorted(thumbs.THUMBABLE_TYPES)
    ])
    queue = db.execute(
        select(Attachment.id)
        .where(
            Attachment.thumb_status == "skipped",
            Attachment.is_inline.is_(False),
            Attachment.content.is_not(None),
            renderable,
        )
        .limit(limit)
    ).scalars().all()
    if not queue:
        return 0

    db.execute(
        update(Attachment)
        .where(Attachment.id.in_(queue))
        .values(thumb_status="pending")
    )
    return len(queue)
