"""meerail-agent entrypoint.

Runs next to Proton Bridge. Streams mail into the meerail server and (from M5)
drains queued actions back to Bridge over SMTP/IMAP.

  python main.py            # continuous: backfill + IDLE, one thread per account
  python main.py --once     # single sync pass over every account, then exit
  python main.py --config /path/to/config.toml
"""

from __future__ import annotations

import argparse
import sys
import threading

from client import ServerClient
from config import load_config
from sync import run_account_forever, sync_once


def main() -> int:
    parser = argparse.ArgumentParser(prog="meerail-agent")
    parser.add_argument("--config", default=None, help="path to config.toml")
    parser.add_argument("--once", action="store_true", help="sync once and exit")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if not cfg.accounts:
        print("No [[account]] entries in config.", file=sys.stderr)
        return 1

    server = ServerClient(cfg.server_url, cfg.agent_token)

    if args.once:
        for account in cfg.accounts:
            print(f"[{account.email}] one-shot sync...")
            sync_once(account, server, cfg, reconcile=True)
            print(f"[{account.email}] done.")
        return 0

    threads = []
    for account in cfg.accounts:
        t = threading.Thread(target=run_account_forever, args=(account, server, cfg),
                             name=f"sync-{account.email}", daemon=True)
        t.start()
        threads.append(t)
    print(f"meerail-agent running for {len(threads)} account(s). Ctrl-C to stop.")
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\nStopping.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
