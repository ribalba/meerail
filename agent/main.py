"""meerail-agent entrypoint.

Runs next to Proton Bridge and owns the entire mail pipeline: fetch over IMAP,
parse, thread, extract attachment text via Tika, and write to Postgres. It also
drains queued actions (flags/moves/sends) back to Bridge. The web app only reads
what this writes.

  python main.py            # continuous: backfill + IDLE, one thread per account
  python main.py --once     # single sync pass over every account, then exit
  python main.py --config /path/to/config.toml
"""

from __future__ import annotations

import argparse
import sys
import threading

from config import load_config


def main() -> int:
    parser = argparse.ArgumentParser(prog="meerail-agent")
    parser.add_argument("--config", default=None, help="path to config.toml")
    parser.add_argument("--once", action="store_true", help="sync once and exit")
    args = parser.parse_args()

    # Must precede any core.* import: loading the config publishes DATABASE_URL
    # and TIKA_URL into the environment, which is what core.config reads.
    cfg = load_config(args.config)
    if not cfg.accounts:
        print("No [[account]] entries in config.", file=sys.stderr)
        return 1

    from core.database import init_db
    from sync import run_account_forever, sync_once

    # The agent writes the schema it depends on, so it can run before (or
    # without) the web app ever having started.
    init_db()

    if args.once:
        for account in cfg.accounts:
            print(f"[{account.email}] one-shot sync...")
            sync_once(account, cfg, reconcile=True)
            print(f"[{account.email}] done.")
        return 0

    threads = []
    for account in cfg.accounts:
        t = threading.Thread(target=run_account_forever, args=(account, cfg),
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
