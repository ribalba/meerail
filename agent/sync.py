"""Per-account sync: backfill, incremental, flags, vanished, extraction, IDLE.

The agent owns the whole write path. It fetches from IMAP and calls into
``core.ingest`` to parse, thread, store and index — writing directly to
Postgres. The web app never touches this; it only reads the result.
"""

from __future__ import annotations

import random
import time

from core import ingest
from core.database import SessionLocal
from core.models import utcnow

import commands
import log
from actions import drain_actions
from config import AccountConfig, AgentConfig
from imap import Bridge, Suspended


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# How hard to push a server that answers a FETCH without every UID in it. See
# _fetch_all: rounds of re-asking, and the seconds waited before each one.
_FETCH_ROUNDS = 3
_FETCH_BACKOFF = 2.0


def _fetch_all(fetch, uids: list[int], what: str, email: str | None = None,
               *, needs_body: bool = True) -> dict[int, dict]:
    """``fetch(uids)``, but insisting on the UIDs the server leaves out.

    A partial FETCH response is how Gmail says "not right now": it answers a
    large request with some UIDs simply absent rather than with an error. The
    size of the ask is the problem, so the ones that went missing are asked for
    again one at a time, after a pause.

    Anything still absent after ``_FETCH_ROUNDS`` raises, exactly as a partial
    fetch always did — the cursor must never step over mail that was not
    fetched. What changes is that a hiccup no longer throws away the pass: a
    multi-hour backfill used to restart from its last committed cursor because
    one UID out of tens of thousands came back empty.
    """
    got = fetch(uids)
    for round_no in range(1, _FETCH_ROUNDS + 1):
        missing = _missing(uids, got, needs_body)
        if not missing:
            return got
        log.warn(f"{what} omitted {len(missing)} of {len(uids)} UID(s) "
                 f"{missing[:5]}{'…' if len(missing) > 5 else ''} — "
                 f"refetching individually ({round_no}/{_FETCH_ROUNDS})", email)
        time.sleep(_FETCH_BACKOFF * round_no)
        for uid in missing:
            got.update(fetch([uid]))
    missing = _missing(uids, got, needs_body)
    if missing:
        raise RuntimeError(f"IMAP {what} omitted UIDs: {missing}")
    return got


def _missing(uids: list[int], got: dict[int, dict], needs_body: bool) -> list[int]:
    """UIDs the response left out — or answered with an empty body, which for a
    fetch that asked for one is the same thing."""
    return sorted(u for u in uids
                  if u not in got or (needs_body and not got[u].get("raw")))


class PassProgress:
    """Where this pass has got to, as a JSON blob for the status panel.

    The bar is per folder, with a folder counter beside it, rather than one bar
    across the whole pass. A pass-wide denominator would mean SELECTing and
    SEARCHing every folder up front just to total them — doubling the IMAP
    round trips for a number that is stale as soon as mail arrives. Per folder
    the total is already in hand: ``new_uids`` returns the complete UID list
    before the first chunk is fetched.

    ``walked`` counts UIDs looked at, not messages stored, and the two diverge
    by a lot: a Proton mailbox shows the same message under several labels, so
    most of a backfill's UIDs resolve to a placement row against content that is
    already held. Driving the bar off stored messages would leave it apparently
    frozen through exactly those stretches.
    """

    def __init__(self, folder_count: int):
        self.folder_count = folder_count
        self.started_at = utcnow()
        self.folder = None
        self.folder_index = 0      # 1-based, for display
        self.folder_done = 0
        self.folder_total = 0
        self.walked = 0            # UIDs examined across the whole pass
        self.stored = 0            # messages whose content was new
        self.active = True
        self.finished_at = None

    def enter_folder(self, name: str, index: int) -> None:
        """Move to folder ``index`` (0-based). The total lands later, from
        ``_sync_new``: it is the length of the UID list, which costs a SEARCH."""
        self.folder = name
        self.folder_index = index + 1
        self.folder_done = 0
        self.folder_total = 0

    def advance(self, walked: int, stored: int) -> None:
        self.folder_done += walked
        self.walked += walked
        self.stored += stored

    def finish(self) -> None:
        self.active = False
        self.finished_at = utcnow()

    def snapshot(self) -> dict:
        return {
            "active": self.active,
            "started_at": self.started_at.isoformat(),
            "updated_at": utcnow().isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "folder": self.folder,
            "folder_index": self.folder_index,
            "folder_count": self.folder_count,
            "folder_done": self.folder_done,
            "folder_total": self.folder_total,
            "walked": self.walked,
            "stored": self.stored,
        }


def _sync_new(db, bridge: Bridge, account, mailbox, batch: int,
              progress: PassProgress | None = None, cutoff=None) -> int:
    """Ingest UIDs above the folder's cursor. Returns how many were stored.

    The cursor only advances once a chunk is fully ingested, so an interrupted or
    partial IMAP fetch is retried next pass rather than silently skipped.

    ``cutoff`` is the content window's oldest date, or None to fetch everything.
    Mail sent before it gets its headers fetched and nothing else — the decision
    is made from the cheap header pass, so the body never crosses the wire.
    """
    new = bridge.new_uids(mailbox.last_uid)
    if progress is not None:
        progress.folder_total = len(new)
    if not new:
        return 0
    stored = 0
    email = getattr(account, "email", None)
    for chunk in _chunks(new, batch):
        headers = _fetch_all(bridge.fetch_headers, chunk, "header fetch", email,
                             needs_body=False)

        # Content we already hold (same Message-ID under another Proton label)
        # only needs a placement row; everything else needs fetching — in full,
        # or headers alone if it is older than the content window. A message
        # with no date at all is fetched in full: unknown age is not evidence
        # that mail is old, and guessing wrong here silently drops a body.
        need_raw, need_headers = [], []
        for uid, h in headers.items():
            if ingest.record_known(db, account, mailbox, uid, h["flags"], h["message_id"]):
                continue
            if cutoff is not None and h["date"] is not None and h["date"] < cutoff:
                need_headers.append(uid)
            else:
                need_raw.append(uid)

        chunk_stored = 0
        if need_raw:
            raws = _fetch_all(bridge.fetch_raw, need_raw, "raw fetch", email)
            for uid, r in raws.items():
                if r["raw"]:
                    ingest.store_message(db, account, mailbox, uid, r["flags"], r["raw"])
                    chunk_stored += 1

        if need_headers:
            blocks = _fetch_all(bridge.fetch_header_block, need_headers,
                                "header fetch", email)
            for uid, r in blocks.items():
                if r["raw"]:
                    ingest.store_headers(db, account, mailbox, uid, r["flags"], r["raw"],
                                         size_bytes=headers[uid]["size"])
                    chunk_stored += 1

        ingest.advance_cursor(db, mailbox, max(chunk))
        # Rides the cursor's own transaction on purpose. Progress that committed
        # separately could outrun a chunk that then rolled back, leaving the bar
        # claiming ground the next pass has to cover again.
        if progress is not None:
            progress.advance(len(chunk), chunk_stored)
            ingest.set_progress(db, account, progress.snapshot())
        db.commit()
        # Notify only after the batch is durable, so the UI never refreshes onto
        # rows that a later failure would roll back.
        ingest.note_ingested(account, mailbox, chunk_stored)
        stored += chunk_stored
    return stored


def _reconcile(db, bridge: Bridge, mailbox, batch: int) -> None:
    """Push flag changes for known UIDs and prune the ones that vanished."""
    uids = bridge.all_uids()
    for chunk in _chunks(uids, batch):
        flags = bridge.fetch_flags(chunk)
        ingest.update_flags(db, mailbox, [{"uid": u, "flags": f} for u, f in flags.items()])
    ingest.prune_vanished(db, mailbox, uids)
    db.commit()


def _extract_all(db, limit_batches: int = 200) -> int:
    """Drain pending attachment text extraction through Tika."""
    total = 0
    for _ in range(limit_batches):
        n = ingest.extract_pending(db)
        db.commit()
        if not n:
            break
        total += n
    return total


def _thumb_all(db, limit_batches: int = 200) -> int:
    """Drain pending attachment previews."""
    total = 0
    for _ in range(limit_batches):
        n = ingest.thumb_pending(db)
        db.commit()
        if not n:
            break
        total += n
    return total


def _prune_all(db, months: int, limit_batches: int = 200) -> int:
    """Strip content from stored mail that has slid out of the content window."""
    cutoff = ingest.content_cutoff(months)
    if cutoff is None:
        return 0
    total = 0
    for _ in range(limit_batches):
        n = ingest.prune_expired_content(db, cutoff)
        db.commit()
        if not n:
            break
        total += n
    return total


def index_once(months: int = 0) -> tuple[int, int, int]:
    """Drain the attachment queues once. Returns (extracted, previews, pruned)."""
    db = SessionLocal()
    try:
        return _extract_all(db), _thumb_all(db), _prune_all(db, months)
    finally:
        db.close()


def run_indexer_forever(cfg: AgentConfig) -> None:
    """Drain attachment text and previews, forever, on a thread of its own.

    Split out of the sync pass because the two have nothing to do with each
    other in practice: mail can be fully fetched while thousands of attachments
    are still queued, and folding the second into the first made every pass as
    slow as the backlog. One thread for all accounts — the queue is global, and
    parallel Tika drains would only contend on the same rows.
    """
    idle = 0
    log.info("indexer started", "indexer")
    if cfg.content_window_months:
        log.info(f"content window: {cfg.content_window_months} month(s) — older mail is "
                 "kept as headers only", "indexer")
    while True:
        try:
            # The window prune rides this thread rather than the sync pass: it
            # is database-only work over every account at once, and it has to
            # keep happening on a mailbox where no new mail is arriving — the
            # cutoff moves whether or not anything is being fetched.
            extracted, thumbed, pruned = index_once(cfg.content_window_months)
            if extracted or thumbed or pruned:
                idle = 0
                log.ok(f"{extracted} attachment(s) extracted, "
                       f"{thumbed} preview(s) rendered, "
                       f"{pruned} message(s) pruned to headers", "indexer")
            else:
                # Nothing queued. Back off to the poll interval rather than
                # spinning on an empty queue; new attachments arrive with new
                # mail, which is at most a poll interval away anyway.
                idle = min(idle + 1, 6)
            time.sleep(cfg.poll_interval if idle else 1)
        except Exception as e:  # noqa: BLE001
            # Never let the indexer die: mail sync is unaffected by it, and a
            # dead thread would silently stop every future extraction.
            log.error(f"indexing failed: {e!r}", "indexer")
            time.sleep(30)


def backfill_previews() -> int:
    """Render previews for attachments that predate the feature.

    Upgrading an existing database marks old attachments 'skipped' so that adding
    this feature does not silently kick off a full-mailbox render; this is the
    explicit opt-in. Queues and drains in chunks so progress is visible and the
    work can be interrupted without losing what it has already done.
    """
    db = SessionLocal()
    try:
        total = 0
        while True:
            queued = ingest.backfill_thumbs(db)
            db.commit()
            if not queued:
                break
            done = _thumb_all(db)
            total += done
            print(f"  ...rendered {total} previews")
        print(f"Done: {total} previews.")
        return 0
    finally:
        db.close()


def _report_error(email: str, message: str) -> None:
    """Record a failed pass so the UI can warn, on a session of its own.

    The caller's session is gone by the time we get here, and the failure may
    well *be* the database — so this opens its own and swallows anything it
    throws. Reporting an error must never become a second error that takes the
    retry loop down with it; the print above is the guaranteed record.
    """
    db = SessionLocal()
    try:
        ingest.record_agent_error(db, email, message)
        db.commit()
    except Exception as e:  # noqa: BLE001
        log.warn(f"could not record sync error in the database: {e!r}", email)
    finally:
        db.close()


def sync_once(account: AccountConfig, cfg: AgentConfig, reconcile: bool = True) -> None:
    """One full pass over every folder for an account.

    If the UI has raised a recheck request for this account, the pass rewinds
    every folder's UID cursor first so it re-walks the whole mailbox rather than
    only what is new — the repair path for a database that has lost messages the
    cursor would otherwise skip past.
    """
    started = time.monotonic()
    # Per-account batch size wins where it is set: what one server answers
    # comfortably, another truncates or drops the connection over.
    batch = getattr(account, "batch_size", None) or cfg.batch_size
    bridge = Bridge(account)
    bridge.connect()
    log.info(f"connected to {account.imap_host}:{account.imap_port} "
             f"({account.imap_security})", account.email)
    db = SessionLocal()
    account_row = None
    progress = None
    try:
        account_row = ingest.get_or_create_account(db, account.email)
        # Connect and login are behind us, so a failure recorded by an earlier
        # pass is over — say so now rather than at the end of the pass. The
        # initial backfill of a large mailbox runs for many minutes, and an
        # error left standing for that whole window reads as "still broken"
        # while the progress bar beside it visibly advances.
        ingest.clear_agent_error(db, account_row)
        # Read once, up front: a request arriving mid-pass must not be cleared
        # by this pass, which has already walked part of the mailbox without it.
        recheck_at = ingest.take_recheck(db, account_row)
        db.commit()
        if recheck_at:
            log.info("full recheck requested — rewinding all folder cursors", account.email)

        # Write-back first: apply any queued flag/move/delete/send actions.
        drained = drain_actions(db, bridge, account_row)
        if drained:
            log.info(f"applied {drained} queued action(s)", account.email)

        # Recomputed per pass, not per process: the window slides, and an agent
        # that has been up for weeks would otherwise still be fetching against
        # the cutoff it worked out at startup.
        cutoff = ingest.content_cutoff(cfg.content_window_months)
        ingest.record_content_window(db, cfg.content_window_months)

        folders = bridge.list_folders()
        progress = PassProgress(len(folders))
        for i, f in enumerate(folders):
            uidvalidity, uidnext = bridge.select(f["name"])
            mailbox = ingest.register_folder(
                db, account_row, f["name"], f["role_hint"], uidvalidity, uidnext, sort_order=i
            )
            if recheck_at:
                ingest.reset_cursor(db, mailbox)
            progress.enter_folder(f["name"], i)
            ingest.set_progress(db, account_row, progress.snapshot())
            db.commit()
            _sync_new(db, bridge, account_row, mailbox, batch, progress, cutoff)
            # A recheck reconciles unconditionally: flags and vanished messages
            # are as much a part of "is everything still right" as the bodies.
            if reconcile or recheck_at:
                _reconcile(db, bridge, mailbox, batch)

        # LIST completed and every returned folder synced successfully, so it
        # is now safe to treat absent rows as folders removed/renamed upstream.
        ingest.prune_mailboxes(db, account_row, {f["name"] for f in folders})
        db.commit()

        # Attachment text and previews are deliberately not done here. They are
        # not mail sync: a large Tika backlog would hold the pass open for
        # minutes after every message had landed, and the UI reads "a pass is
        # open" as "still fetching mail". run_indexer_forever drains them on its
        # own thread, and reports its own progress.
        ingest.record_sync(db, account_row, backfill_complete=True,
                           addresses=account.send_addresses())
        if recheck_at:
            ingest.clear_recheck(db, account_row, recheck_at)
        db.commit()

        # The one line that says the pass got all the way to the end. Without it
        # a healthy agent prints nothing at all, and "no output" is exactly what
        # a wedged one looks like too.
        log.ok(f"sync complete in {time.monotonic() - started:.1f}s — "
               f"{len(folders)} folders, {progress.walked} messages examined, "
               f"{progress.stored} new", account.email)
    finally:
        # Close the pass out on the way through, however it ended. A pass that
        # died mid-folder would otherwise leave 'active' set for good, and the
        # panel would show a bar creeping nowhere instead of the error the
        # retry loop is about to record.
        if progress is not None and account_row is not None:
            try:
                progress.finish()
                ingest.set_progress(db, account_row, progress.snapshot())
                db.commit()
            except Exception:  # noqa: BLE001
                # Advisory to the last: if the failure that got us here was the
                # database, this write fails too, and it must not replace the
                # real exception on its way up.
                db.rollback()
        db.close()
        bridge.logout()


def run_account_forever(account: AccountConfig, cfg: AgentConfig) -> None:
    """Continuous loop: initial backfill, then IDLE for changes."""
    backoff = 5
    wake = commands.wake_event(account.email)
    log.info("sync loop started", account.email)
    # Reconciling walks every UID in every folder to pull flags and prune
    # vanished mail. On a mailbox with many folders that costs far more than the
    # poll interval, so doing it every cycle leaves the account permanently
    # mid-pass — the panel then shows a spinner that never stops, and with
    # several accounts staggered it never stops for any of them. New mail still
    # arrives every cycle; only the sweep is put on a slower clock.
    last_reconcile = 0.0

    def reconcile_due() -> bool:
        return time.monotonic() - last_reconcile >= cfg.reconcile_interval

    while True:
        try:
            wake.clear()
            sync_once(account, cfg, reconcile=True)
            last_reconcile = time.monotonic()
            backoff = 5
            # Steady state: IDLE on INBOX, then re-sync on any change.
            bridge = Bridge(account)
            bridge.connect()
            try:
                while True:
                    bridge.select("INBOX")
                    try:
                        bridge.idle_wait(cfg.poll_interval, wake=wake)
                    except Suspended:
                        # The host slept through the IDLE wait; the socket is
                        # stale. Drop it without a blocking logout and break to
                        # the top for a fresh connect + sync, so mail that
                        # arrived while suspended lands seconds after wake rather
                        # than after the read timeout expires on the dead socket.
                        log.info("woke from suspend — reconnecting", account.email)
                        bridge.abort()
                        break
                    # Cleared before the pass, not after: a request arriving
                    # mid-sync then earns its own pass rather than being
                    # swallowed by the one already in flight.
                    wake.clear()
                    reconcile = reconcile_due()
                    sync_once(account, cfg, reconcile=reconcile)
                    if reconcile:
                        last_reconcile = time.monotonic()
            finally:
                bridge.logout()
        except Exception as e:  # noqa: BLE001
            # Jittered, not the flat backoff. Every account's thread starts at
            # the same instant and doubles on the same schedule, so on a cold
            # start — where Bridge is not up yet and every account fails — they
            # stay in lockstep and hit Bridge as a burst of simultaneous logins
            # forever after. Bridge answers a burst with "too many login
            # attempts", which fails the next pass, which widens the burst.
            # Spreading each account's retry over the window breaks the convoy.
            delay = random.uniform(backoff / 2, backoff)
            log.error(f"sync failed: {e!r}", account.email)
            advice = log.hint(e)
            if advice:
                log.warn(advice, account.email)
            log.info(f"retrying in {delay:.0f}s", account.email)
            _report_error(account.email, f"{e!r}")
            time.sleep(delay)
            # Capped at the poll interval, not minutes: new mail matters more
            # than sparing Bridge a retry, and after a suspend/resume the first
            # few passes routinely fail while Bridge reconnects upstream — a
            # long backoff there would leave the agent asleep well after the
            # host (and Bridge) are ready. The jitter above still staggers the
            # accounts so they don't retry Bridge in one burst.
            backoff = min(backoff * 2, cfg.poll_interval)
