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

import actions
import commands
import log
from actions import drain_actions
from core.config import AccountConfig, Settings
from imap import Bridge, Suspended


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# How hard to push a server that answers a FETCH without every UID in it. See
# _fetch_all: rounds of re-asking, and the seconds waited before each one.
_FETCH_ROUNDS = 3
_FETCH_BACKOFF = 2.0

# How long a pass that sent mail waits before reading the folders back. See the
# call in sync_once for what it is waiting for.
_SEND_SETTLE_SECONDS = 10.0


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


# How long a loop inside a pass may go without telling the database it is still
# working. The status panel calls an account offline after 180s of silence
# (app/syncstate.STALE_AFTER_HEALTHY), so this leaves room for several beats to
# be missed — to a slow write, or to a single IMAP round trip that outruns the
# interval — before a live agent is mistaken for a dead one.
_HEARTBEAT_INTERVAL = 30


class Heartbeat:
    """Proof, written as the pass runs, that a long loop is still working.

    A pass stamps ``last_agent_seen`` once when it opens and once when it
    closes. That was enough while every pass was seconds long, and it stops
    being enough the moment one is not: reconciling a folder is a single loop of
    IMAP round trips with one commit at the end, and against a server that
    answers slowly — Gmail charges some accounts about ten seconds per command,
    whatever it was asked for — a large folder holds that loop open for hours.
    For all of that time the panel has nothing newer than the stamp from when
    the pass opened, so it reports an agent that is visibly working as offline.

    ``mark`` rides a commit the caller was making anyway; ``beat`` is for a loop
    that has none of its own and is rate-limited, because against a fast server
    the same loop runs a chunk in milliseconds and a commit apiece would be pure
    write amplification.
    """

    def __init__(self, db, account, progress: "PassProgress | None" = None):
        self.db = db
        self.account = account
        self.progress = progress
        self._last = time.monotonic()

    def mark(self) -> None:
        """Stamp onto the open transaction. The caller commits."""
        self._last = time.monotonic()
        ingest.touch_agent(self.db, self.account)

    def beat(self) -> None:
        """Stamp, refresh the progress blob and commit — at most every interval.

        The blob is rewritten unchanged rather than left alone: ``pass_advancing``
        reads its ``updated_at`` and documents the assumption that a pass rewrites
        it as it goes, and a loop that quietly broke that assumption is what let a
        working pass be classified from a stale error instead.

        Committing takes whatever the loop has done so far with it. For the flag
        sweep that calls this, that is safe by construction: the next reconcile
        re-reads every flag from the server regardless, so a partially applied
        sweep costs nothing and a wholly invisible one costs a heartbeat.
        """
        if time.monotonic() - self._last < _HEARTBEAT_INTERVAL:
            return
        self.mark()
        if self.progress is not None:
            ingest.set_progress(self.db, self.account, self.progress.snapshot())
        self.db.commit()


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
              progress: PassProgress | None = None, cutoff=None,
              beat: "Heartbeat | None" = None) -> int:
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
        # Rides the commit below rather than making one of its own: this loop
        # already writes once per chunk, and the stamp is a column on a row it
        # is writing anyway. Unlike the flag sweep, the reason a chunk here can
        # take minutes is that it is fetching whole message bodies.
        if beat is not None:
            beat.mark()
        db.commit()
        # Notify only after the batch is durable, so the UI never refreshes onto
        # rows that a later failure would roll back.
        ingest.note_ingested(account, mailbox, chunk_stored)
        stored += chunk_stored
    return stored


# A UID we have already re-fetched once for a given server size, per folder.
#
# The evidence for re-fetching is "the server says this message is bigger than
# what we hold", and on a server whose RFC822.SIZE is an estimate rather than a
# count that can be true of a message we hold perfectly. Without a memo the
# sweep would then pull those bodies again every reconcile interval, for good —
# a re-download of the mailbox on a fifteen-minute clock. Trying once per size
# the server reports keeps the repair and drops the loop: a size that changes
# again is a message that really did change, and earns another attempt.
_refetched: dict[tuple[int, int], int] = {}
_REFETCH_MEMO_MAX = 20_000      # a wrong-by-default server must not grow this forever


def _repair_short_content(db, bridge: Bridge, mailbox, sizes: dict[int, int],
                          email: str | None = None) -> int:
    """Re-fetch messages whose stored content is short of the server's size.

    The one place the agent goes back for bytes it has already fetched. See
    ``ingest.find_short_content`` for why a stored message can be short in the
    first place — in a word, a send that was read back before the server had
    finished putting the message together.
    """
    sizes = {uid: size for uid, size in sizes.items() if size}
    short = [uid for uid in ingest.find_short_content(db, mailbox, sizes)
             if _refetched.get((mailbox.id, uid)) != sizes[uid]]
    if not short:
        return 0

    if len(_refetched) > _REFETCH_MEMO_MAX:
        _refetched.clear()

    try:
        fetched = _fetch_all(bridge.fetch_raw, short, "content refetch", email)
    except Exception as e:  # noqa: BLE001
        # Not a reason to fail the pass. This rides on the flag sweep, which is
        # doing the mailbox's actual work, and the short copy has been sitting
        # there long enough already — the next sweep can try again, which is why
        # nothing is written to the memo on the way out.
        log.warn(f"could not re-fetch {len(short)} short message(s) in "
                 f"{mailbox.imap_name}: {e!r}", email)
        return 0

    repaired = 0
    for uid, r in fetched.items():
        _refetched[(mailbox.id, uid)] = sizes[uid]
        if r["raw"] and ingest.restore_content(db, mailbox, uid, r["raw"]):
            repaired += 1
    if repaired:
        log.info(f"re-fetched {repaired} message(s) in {mailbox.imap_name} that were "
                 f"stored short of the size the server reports", email)
    return repaired


def _reconcile(db, bridge: Bridge, mailbox, batch: int,
               beat: "Heartbeat | None" = None, email: str | None = None) -> None:
    """Push flag changes for known UIDs and prune the ones that vanished.

    The one loop in a pass that reports nothing on its own: it walks every UID
    in the folder in fixed chunks and commits once, at the end. On a mailbox of
    tens of thousands against a server that answers a command a second, that is
    hours of silence from a thread that is working the whole time — hence the
    heartbeat, which is the only thing here the UI can see until the sweep ends.
    """
    uids = bridge.all_uids()
    for chunk in _chunks(uids, batch):
        rows = bridge.fetch_flags(chunk)
        ingest.update_flags(db, mailbox,
                            [{"uid": u, "flags": r["flags"]} for u, r in rows.items()])
        _repair_short_content(db, bridge, mailbox,
                              {u: r["size"] for u, r in rows.items()}, email)
        if beat is not None:
            beat.beat()
    if _uid_list_is_trustworthy(bridge, uids, mailbox, email):
        ingest.prune_vanished(db, mailbox, uids)
    db.commit()


def _uid_list_is_trustworthy(bridge: Bridge, uids: list[int], mailbox,
                             email: str | None) -> bool:
    """Is this SEARCH answer solid enough to delete local mail on the strength of?

    Pruning is the only place the agent removes stored mail on its own, and its
    entire evidence is "the server did not list this UID". That is a sound
    argument only when the server actually answered — and the failure that
    matters is not a connection that drops (the pass dies and nothing is
    deleted) but one that stays up and answers short.

    Proton Bridge is a local server that keeps serving while it cannot reach
    Proton, and a mailbox it has not finished loading answers SEARCH with a
    fraction of what it holds, or with nothing. Taking that at face value
    deletes mail that exists, on a machine that is merely offline.

    SELECT's own EXISTS count is the check: it came from the same server moments
    earlier and it is the server's own statement of how many messages the folder
    holds. A SEARCH that returns fewer UIDs than that has not finished
    answering, whatever the reason, and this pass does not get to delete
    anything. More is fine — mail can arrive between the two commands.

    The genuinely empty folder still prunes: EXISTS is 0, SEARCH returns
    nothing, 0 >= 0, and the placements go.
    """
    exists = bridge.exists
    if len(uids) >= exists:
        return True
    log.warn(f"{mailbox.imap_name}: the server listed {len(uids)} message(s) but says it "
             f"holds {exists} — not removing anything from this folder until it answers "
             f"in full", email)
    return False


def _extract_all(db, limit_batches: int = 200, on_batch=None) -> int:
    """Drain pending attachment text extraction through Tika."""
    total = 0
    for _ in range(limit_batches):
        n = ingest.extract_pending(db)
        db.commit()
        if not n:
            break
        total += n
        if on_batch:
            on_batch(total)
    return total


def _thumb_all(db, limit_batches: int = 200, on_batch=None) -> int:
    """Drain pending attachment previews."""
    total = 0
    for _ in range(limit_batches):
        n = ingest.thumb_pending(db)
        db.commit()
        if not n:
            break
        total += n
        if on_batch:
            on_batch(total)
    return total


def _prune_all(db, months: int, limit_batches: int = 200, on_batch=None) -> int:
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
        if on_batch:
            on_batch(total)
    return total


# How often a running drain says it is still running. Long enough that a queue
# which drains quickly prints nothing at all, short enough that a wait of
# minutes never looks like a hang.
_PROGRESS_EVERY = 15.0

# Below this, a queue is not worth announcing: it drains in seconds, and the
# end-of-pass summary already reports it. The backlog this exists for is four
# figures.
_ANNOUNCE_ABOVE = 50


class _IndexReporter:
    """Throttled progress lines for a drain that can run for many minutes.

    The first pass over a real mailbox queues thousands of attachments, and the
    drain used to log only once it had finished all of them: after "sync
    complete" the agent went silent for ten minutes with no way to tell work
    from a wedge (issue #3). So a phase with a real backlog says so, and then
    keeps saying where it is every `_PROGRESS_EVERY` seconds until it is done.

    One clock across all phases, so the cadence is the agent's, not each
    queue's, and a drain that finishes inside one interval stays silent — this
    is here to explain a backlog, not to narrate a healthy poll.
    """

    def __init__(self, db, every: float = _PROGRESS_EVERY) -> None:
        self._db = db
        self._every = every
        self._last = time.monotonic()

    def phase(self, label: str, queue: str | None = None):
        """Return one drain phase's per-batch callback.

        ``queue`` names the attachment queue to size ('extract' / 'thumb'),
        counted once — after the first batch has proven there is work to do.
        Deferring it that far means an idle poll, which is nearly every poll,
        pays for no count at all. Without it the phase reports a bare running
        total, which is all the prune can offer: "what is past the cutoff" is a
        scan of the whole message table, and draining it is the cheaper answer.
        """
        state: dict[str, int | None] = {"total": None}

        def report(done: int) -> None:
            if queue and state["total"] is None:
                state["total"] = done + ingest.pending_attachment_count(self._db, queue)
                if state["total"] >= _ANNOUNCE_ABOVE:
                    log.info(f"indexing {state['total']} {label} — this can take a "
                             "while on a first run", "indexer")
            now = time.monotonic()
            if now - self._last < self._every:
                return
            self._last = now
            total = state["total"]
            if not total:
                log.info(f"  {label}: {done} so far", "indexer")
                return
            # Capped at the total we measured: a sync thread can queue more
            # while this runs, and "112%" reads as a bug rather than as luck.
            log.info(f"  {label}: {done}/{total} ({min(100, done * 100 // total)}%)",
                     "indexer")

        return report


def index_once(months: int = 0, report: bool = False) -> tuple[int, int, int]:
    """Drain the attachment queues once. Returns (extracted, previews, pruned).

    With ``report`` the drain narrates itself as it goes; see _IndexReporter.
    Off by default so that callers which only want the numbers (tests, the
    preview backfill) stay quiet.
    """
    db = SessionLocal()
    try:
        if not report:
            return _extract_all(db), _thumb_all(db), _prune_all(db, months)
        reporter = _IndexReporter(db)
        return (
            _extract_all(db, on_batch=reporter.phase("attachment(s) to index", "extract")),
            _thumb_all(db, on_batch=reporter.phase("preview(s) to render", "thumb")),
            _prune_all(db, months, on_batch=reporter.phase("message(s) pruned to headers")),
        )
    finally:
        db.close()


def run_indexer_forever(cfg: Settings) -> None:
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
            extracted, thumbed, pruned = index_once(cfg.content_window_months, report=True)
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

    The outbox is named here as well, because a pass that dies before
    drain_actions leaves queued mail entirely unmentioned: the log says "sync
    failed" on a loop while the reply someone wrote an hour ago sits unsent
    behind it, and nothing connects the two.
    """
    db = SessionLocal()
    try:
        ingest.record_agent_error(db, email, message)
        db.commit()
    except Exception as e:  # noqa: BLE001
        log.warn(f"could not record sync error in the database: {e!r}", email)
    try:
        # Throttled: this loop can fail every thirty seconds, and the list is a
        # reminder of what is riding on the failure above, not the failure.
        actions.report_waiting(db, email, throttle=True)
    except Exception as e:  # noqa: BLE001
        log.warn(f"could not read the outbox: {e!r}", email)
    finally:
        db.close()


def sync_once(account: AccountConfig, cfg: Settings, reconcile: bool = True) -> None:
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
        # Name, colour and footer, for whichever of them meerail.toml pins. Done
        # here rather than at the end of the pass so an edit to the file shows up
        # in the UI as soon as the agent reaches the server, without waiting on a
        # backfill that may run for another hour — or on a pass that fails.
        ingest.record_presentation(db, account_row, account.presentation())
        # Read once, up front: a request arriving mid-pass must not be cleared
        # by this pass, which has already walked part of the mailbox without it.
        recheck_at = ingest.take_recheck(db, account_row)
        db.commit()
        if recheck_at:
            log.info("full recheck requested — rewinding all folder cursors", account.email)

        # Write-back first: apply any queued flag/move/delete/send actions.
        # Anything that failed has already logged why; the counts here only make
        # sure the summary never reads as "applied" for a queue that did not
        # fully apply.
        applied, failed, sent = drain_actions(db, bridge, account_row)
        if failed:
            log.warn(f"{applied} queued action(s) applied, {failed} failed", account.email)
        elif applied:
            log.info(f"applied {applied} queued action(s)", account.email)

        # Mail has just gone out, and the folder walk below is about to read the
        # server's copy of it back. /send asks for this pass by name so the
        # outbox empties promptly, which puts that read within a second of the
        # SMTP hand-off — and Proton creates a message before it links the
        # attachments to it. Read that early and what lands is your own mail
        # with the attachment missing, stored as complete because nothing in
        # those bytes says otherwise.
        #
        # So let it settle. The cost is a few seconds on the passes that sent
        # something, and only on those; the send itself has already happened,
        # and mail arriving in the meantime is picked up by the same walk.
        # _repair_short_content is the backstop for when this is not long
        # enough — this wait is what keeps that from being needed.
        if sent:
            time.sleep(_SEND_SETTLE_SECONDS)

        # Recomputed per pass, not per process: the window slides, and an agent
        # that has been up for weeks would otherwise still be fetching against
        # the cutoff it worked out at startup.
        cutoff = ingest.content_cutoff(cfg.content_window_months)
        ingest.record_content_window(db, cfg.content_window_months)

        folders = bridge.list_folders()
        if not folders:
            # Not fatal and not silent. A Bridge that is still starting, signed
            # out, or sitting on a machine that has been offline for days
            # answers LIST with nothing; the pass has nothing to do, and
            # prune_mailboxes refuses to read it as "every folder was deleted".
            log.warn("the server listed no folders — nothing was synced, and nothing "
                     "was removed. Is this account loaded and signed in in Bridge?",
                     account.email)
        # LIST order is the display order — that is what sort_order below hands
        # the UI — but it is not the order worth walking in. Servers commonly
        # list INBOX somewhere in the middle, and on a first run that leaves the
        # one folder the user is actually looking at waiting behind every
        # archive folder on the account. So walk the inbox first and keep its
        # LIST position for display: the pair is (display position, folder).
        walk = sorted(
            enumerate(folders),
            key=lambda p: ingest.derive_role(p[1]["name"], p[1]["role_hint"]) != "inbox",
        )
        progress = PassProgress(len(folders))
        beat = Heartbeat(db, account_row, progress)
        for i, (order, f) in enumerate(walk):
            uidvalidity, uidnext = bridge.select(f["name"])
            mailbox = ingest.register_folder(
                db, account_row, f["name"], f["role_hint"], uidvalidity, uidnext, sort_order=order
            )
            if recheck_at:
                ingest.reset_cursor(db, mailbox)
            progress.enter_folder(f["name"], i)
            ingest.set_progress(db, account_row, progress.snapshot())
            beat.mark()
            db.commit()
            _sync_new(db, bridge, account_row, mailbox, batch, progress, cutoff, beat)
            # A recheck reconciles unconditionally: flags and vanished messages
            # are as much a part of "is everything still right" as the bodies.
            if reconcile or recheck_at:
                _reconcile(db, bridge, mailbox, batch, beat, account.email)

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
                           identities=account.send_identities())
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


def run_account_forever(account: AccountConfig, cfg: Settings) -> None:
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
