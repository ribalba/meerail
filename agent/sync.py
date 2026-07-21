"""Per-account sync: backfill, incremental, flags, vanished, extraction, IDLE.

The agent owns the whole write path. It fetches from IMAP and calls into
``core.ingest`` to parse, thread, store and index — writing directly to
Postgres. The web app never touches this; it only reads the result.
"""

from __future__ import annotations

import time

from core import ingest
from core.database import SessionLocal

from actions import drain_actions
from config import AccountConfig, AgentConfig
from imap import Bridge


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _sync_new(db, bridge: Bridge, account, mailbox, batch: int) -> int:
    """Ingest UIDs above the folder's cursor. Returns how many were stored.

    The cursor only advances once a chunk is fully ingested, so an interrupted or
    partial IMAP fetch is retried next pass rather than silently skipped.
    """
    new = bridge.new_uids(mailbox.last_uid)
    if not new:
        return 0
    stored = 0
    for chunk in _chunks(new, batch):
        headers = bridge.fetch_headers(chunk)
        missing_headers = set(chunk) - set(headers)
        if missing_headers:
            raise RuntimeError(f"IMAP header fetch omitted UIDs: {sorted(missing_headers)}")

        # Content we already hold (same Message-ID under another Proton label)
        # only needs a placement row; everything else needs the raw bytes.
        need_raw = []
        for uid, h in headers.items():
            if not ingest.record_known(db, account, mailbox, uid, h["flags"], h["message_id"]):
                need_raw.append(uid)

        chunk_stored = 0
        if need_raw:
            raws = bridge.fetch_raw(need_raw)
            missing_raw = set(need_raw) - {u for u, r in raws.items() if r.get("raw")}
            if missing_raw:
                raise RuntimeError(f"IMAP raw fetch omitted UIDs: {sorted(missing_raw)}")
            for uid, r in raws.items():
                if r["raw"]:
                    ingest.store_message(db, account, mailbox, uid, r["flags"], r["raw"])
                    chunk_stored += 1

        ingest.advance_cursor(db, mailbox, max(chunk))
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


def sync_once(account: AccountConfig, cfg: AgentConfig, reconcile: bool = True) -> None:
    """One full pass over every folder for an account."""
    bridge = Bridge(account)
    bridge.connect()
    db = SessionLocal()
    try:
        account_row = ingest.get_or_create_account(db, account.email)
        db.commit()

        # Write-back first: apply any queued flag/move/delete/send actions.
        drain_actions(db, bridge, account_row)

        for i, f in enumerate(bridge.list_folders()):
            uidvalidity, uidnext = bridge.select(f["name"])
            mailbox = ingest.register_folder(
                db, account_row, f["name"], f["role_hint"], uidvalidity, uidnext, sort_order=i
            )
            db.commit()
            _sync_new(db, bridge, account_row, mailbox, cfg.batch_size)
            if reconcile:
                _reconcile(db, bridge, mailbox, cfg.batch_size)

        _extract_all(db)
        ingest.record_sync(db, account_row, backfill_complete=True,
                           addresses=account.send_addresses())
        db.commit()
    finally:
        db.close()
        bridge.logout()


def run_account_forever(account: AccountConfig, cfg: AgentConfig) -> None:
    """Continuous loop: initial backfill, then IDLE for changes."""
    backoff = 5
    while True:
        try:
            sync_once(account, cfg, reconcile=True)
            backoff = 5
            # Steady state: IDLE on INBOX, then re-sync on any change.
            bridge = Bridge(account)
            bridge.connect()
            try:
                while True:
                    bridge.select("INBOX")
                    bridge.idle_wait(cfg.poll_interval)
                    sync_once(account, cfg, reconcile=True)
            finally:
                bridge.logout()
        except Exception as e:  # noqa: BLE001
            print(f"[{account.email}] sync error: {e!r}; retrying in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
