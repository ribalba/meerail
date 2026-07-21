"""Per-account sync orchestration: backfill, incremental, flags, vanished, IDLE."""

from __future__ import annotations

import base64
import time

from actions import drain_actions
from client import ServerClient
from config import AccountConfig, AgentConfig
from imap import Bridge


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _sync_new(bridge: Bridge, server: ServerClient, email: str, folder: str,
              uidvalidity: int | None, last_uid: int, batch: int) -> int:
    """Ingest UIDs greater than the server's cursor. Returns count uploaded."""
    new = bridge.new_uids(last_uid)
    if not new:
        return 0
    uploaded = 0
    for chunk in _chunks(new, batch):
        headers = bridge.fetch_headers(chunk)
        missing_headers = set(chunk) - set(headers)
        if missing_headers:
            raise RuntimeError(f"IMAP header fetch omitted UIDs: {sorted(missing_headers)}")
        scan_items = [{"uid": uid, "message_id": h["message_id"], "flags": h["flags"]}
                      for uid, h in headers.items()]
        resp = server.scan(email, folder, uidvalidity, scan_items)
        need = resp.get("need_raw", [])
        if need:
            raws = bridge.fetch_raw(need)
            missing_raw = set(need) - {uid for uid, row in raws.items() if row.get("raw")}
            if missing_raw:
                raise RuntimeError(f"IMAP raw fetch omitted UIDs: {sorted(missing_raw)}")
            items = [{"uid": uid, "flags": r["flags"],
                      "raw_b64": base64.b64encode(r["raw"]).decode()}
                     for uid, r in raws.items() if r["raw"]]
            result = server.upload_messages(email, folder, uidvalidity, items)
            if result.get("stored") != len(items):
                raise RuntimeError("server did not confirm the complete upload batch")
            uploaded += len(items)
        server.advance_cursor(email, folder, max(chunk))
    return uploaded


def _reconcile(bridge: Bridge, server: ServerClient, email: str, folder: str,
               uidvalidity: int | None, batch: int) -> None:
    """Push flag changes for known UIDs and prune vanished ones."""
    uids = bridge.all_uids()
    for chunk in _chunks(uids, batch):
        flags = bridge.fetch_flags(chunk)
        server.update_flags(email, folder, [{"uid": u, "flags": f} for u, f in flags.items()])
    server.present(email, folder, uidvalidity, uids)


def sync_once(account: AccountConfig, server: ServerClient, cfg: AgentConfig,
              reconcile: bool = True) -> None:
    """One full pass over all folders for an account."""
    bridge = Bridge(account)
    bridge.connect()
    try:
        # Write-back first: apply any queued flag/move/delete/send actions.
        drain_actions(bridge, server, account.email)
        folders = bridge.list_folders()
        registered = server.register_folders(
            account.email,
            [{"imap_name": f["name"], "role_hint": f["role_hint"]} for f in folders],
        )
        # Re-register with uidvalidity now that we can SELECT each folder.
        cursors = {c["imap_name"]: c for c in registered}
        for f in folders:
            name = f["name"]
            uidvalidity, uidnext = bridge.select(name)
            fresh = server.register_folders(
                account.email,
                [{"imap_name": name, "role_hint": f["role_hint"],
                  "uidvalidity": uidvalidity, "uidnext": uidnext}],
            )[0]
            last_uid = fresh["last_uid"]
            _sync_new(bridge, server, account.email, name, uidvalidity, last_uid, cfg.batch_size)
            if reconcile:
                _reconcile(bridge, server, account.email, name, uidvalidity, cfg.batch_size)
        server.heartbeat(account.email, backfill_complete=True)
    finally:
        bridge.logout()


def run_account_forever(account: AccountConfig, server: ServerClient, cfg: AgentConfig) -> None:
    """Continuous loop: initial backfill, then poll + IDLE for changes."""
    backoff = 5
    while True:
        try:
            sync_once(account, server, cfg, reconcile=True)
            backoff = 5
            # Steady state: IDLE on INBOX, then re-sync new mail.
            bridge = Bridge(account)
            bridge.connect()
            try:
                while True:
                    bridge.select("INBOX")
                    bridge.idle_wait(cfg.poll_interval)
                    # A change (or timeout) -> do a light incremental pass.
                    sync_once(account, server, cfg, reconcile=True)
            finally:
                bridge.logout()
        except Exception as e:  # noqa: BLE001
            print(f"[{account.email}] sync error: {e!r}; retrying in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
