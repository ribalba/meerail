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
from datetime import datetime

from sqlalchemy import func, or_, select, update

from . import events
from .mail import thumbs, tika
from .mail.parse import strip_nuls
from .mail.store import (
    ingest_location_only,
    ingest_raw,
    rebuild_search_text,
    recompute_counts,
    strip_content,
)
from .models import Account, Attachment, Mailbox, Message, MessageLocation, Setting, utcnow

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


def _clear_mailbox(db, mailbox: Mailbox) -> int:
    """Remove every placement in a mailbox and its now-orphaned content."""
    locs = db.execute(
        select(MessageLocation).where(MessageLocation.mailbox_id == mailbox.id)
    ).scalars().all()
    affected = {loc.message_pk for loc in locs}
    for loc in locs:
        db.delete(loc)
    db.flush()
    _delete_orphans(db, affected)
    mailbox.last_uid = 0
    mailbox.total_count = 0
    mailbox.unread_count = 0
    return len(locs)


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
        # just the cursor. Reusing the old rows would silently point new UIDs at
        # old content until a later reconciliation happened to clean them up.
        if mb.uidvalidity is not None and uidvalidity is not None and mb.uidvalidity != uidvalidity:
            removed = _clear_mailbox(db, mb)
            events.publish({"type": "present", "folder": mb.imap_name,
                            "removed": removed, "total": 0})
        if mb.role == "custom":
            mb.role = derive_role(imap_name, role_hint)
    if uidvalidity is not None:
        mb.uidvalidity = uidvalidity
    if uidnext is not None:
        mb.uidnext = uidnext
    db.flush()
    return mb


def record_known(db, account: Account, mailbox: Mailbox, uid: int, flags: dict,
                 message_id: str | None) -> bool:
    """Record a placement for content we already have. True if it matched, in
    which case the raw bytes need not be fetched (Proton shows one message under
    several labels)."""
    if not message_id:
        return False
    return ingest_location_only(db, account, mailbox, uid, flags, message_id)


def store_message(db, account: Account, mailbox: Mailbox, uid: int, flags: dict,
                  raw: bytes) -> bool:
    """Parse and store raw MIME. Returns True if this created new content."""
    _msg, created = ingest_raw(db, account, mailbox, uid, flags, raw)
    return created


def store_headers(db, account: Account, mailbox: Mailbox, uid: int, flags: dict,
                  header_bytes: bytes, size_bytes: int | None = None) -> bool:
    """Store a message's headers with no content, for mail outside the window.

    The caller has decided (from the Date header, before spending a fetch on the
    body) that this message is too old to hold content for. What lands still
    lists, threads, sorts and answers a search for its subject or sender — it
    simply has no body to open. Returns True if this created new content.
    """
    _msg, created = ingest_raw(db, account, mailbox, uid, flags, header_bytes,
                               headers_only=True, size_bytes=size_bytes)
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

    Messages with no parseable Date are left alone. Their age is unknown, and
    the safe reading of "unknown" is to keep what we already have rather than
    throw away content on a guess.
    """
    stale = db.execute(
        select(Message)
        .where(
            Message.content_status == "full",
            Message.date_sent.is_not(None),
            Message.date_sent < cutoff,
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

    The app has no access to agent/config.toml — the two share nothing but the
    database — and it needs the number to tell someone *why* a body is missing.
    Writing it here keeps one source of truth: the agent's config, echoed into
    the database by the process that actually applies it.
    """
    value = str(max(0, int(months)))
    row = db.get(Setting, CONTENT_WINDOW_KEY)
    if row is None:
        db.add(Setting(key=CONTENT_WINDOW_KEY, value=value))
    elif row.value != value:
        row.value = value


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
    """Apply flag state for already-synced UIDs. items: [{uid, flags}]."""
    updated = 0
    for item in items:
        loc = db.execute(
            select(MessageLocation).where(
                MessageLocation.mailbox_id == mailbox.id,
                MessageLocation.imap_uid == item["uid"],
            )
        ).scalar_one_or_none()
        if loc is None:
            continue
        f = item["flags"]
        loc.seen = bool(f.get("seen"))
        loc.flagged = bool(f.get("flagged"))
        loc.answered = bool(f.get("answered"))
        loc.draft = bool(f.get("draft"))
        loc.deleted = bool(f.get("deleted"))
        loc.keywords = f.get("keywords") or []
        updated += 1
    recompute_counts(db, mailbox)
    if updated:
        events.publish({"type": "flags", "folder": mailbox.imap_name,
                        "updated": updated, "unread": mailbox.unread_count})
    return updated


def prune_vanished(db, mailbox: Mailbox, present_uids: list[int]) -> int:
    """Drop placements whose UID is gone from the folder, and any message left
    with no placement at all."""
    present = set(present_uids)
    locs = db.execute(
        select(MessageLocation).where(MessageLocation.mailbox_id == mailbox.id)
    ).scalars().all()
    affected: set[int] = set()
    removed = 0
    for loc in locs:
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


def prune_mailboxes(db, account: Account, present_names: set[str]) -> int:
    """Remove folders absent from the server's successful LIST response."""
    missing = db.execute(
        select(Mailbox).where(
            Mailbox.account_id == account.id,
            Mailbox.imap_name.not_in(present_names),
        )
    ).scalars().all()
    if not missing:
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
                addresses: list[str] | None = None) -> None:
    """Update per-account sync status and the agent-declared sender addresses."""
    if backfill_complete is not None:
        account.backfill_complete = backfill_complete
    if addresses is not None:
        seen: set[str] = set()
        ordered: list[str] = []
        for addr in [account.email, *addresses]:
            low = (addr or "").strip().lower()
            if low and low not in seen:
                seen.add(low)
                ordered.append(low)
        extras = ordered[1:]
        if extras != account.send_addresses:
            account.send_addresses = extras
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
