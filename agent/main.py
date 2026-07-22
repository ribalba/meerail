#!/usr/bin/env python3
"""meerail-agent entrypoint.

Runs next to Proton Bridge and owns the entire mail pipeline: fetch over IMAP,
parse, thread, extract attachment text via Tika, and write to Postgres. It also
drains queued actions (flags/moves/sends) back to Bridge. The web app only reads
what this writes.

Run it through ``run.sh`` (which builds the venv) or directly:

  ./main.py                 # continuous: backfill + IDLE, one thread per account
  ./main.py --once          # single sync pass over every account, then exit
  ./main.py --test          # check every connection (DB, Tika, IMAP, SMTP) and exit
  ./main.py --config /path/to/config.toml
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

# The agent shares the `core` package with the server, which lives one level up.
# run.sh exports PYTHONPATH for this, but do it here too so the script also works
# when invoked directly from an activated venv.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from config import load_config  # noqa: E402  (must follow the sys.path bootstrap)


def main() -> int:
    parser = argparse.ArgumentParser(prog="meerail-agent")
    parser.add_argument("--config", default=None, help="path to config.toml")
    parser.add_argument("--once", action="store_true", help="sync once and exit")
    parser.add_argument("--test", action="store_true",
                        help="check every connection (database, Tika, IMAP, SMTP) and exit")
    parser.add_argument("--backfill-previews", action="store_true",
                        help="render previews for already-stored attachments, then exit")
    args = parser.parse_args()

    # Must precede any core.* import: loading the config publishes DATABASE_URL
    # and TIKA_URL into the environment, which is what core.config reads.
    cfg = load_config(args.config)
    if not cfg.accounts:
        print("No [[account]] entries in config.", file=sys.stderr)
        return 1

    # Before init_db: --test is read-only and must not create the schema, so that
    # it stays safe to run against a database you haven't committed to yet.
    if args.test:
        import preflight
        return preflight.run(cfg)

    import commands
    import log
    from core.database import init_db
    from sync import index_once, run_account_forever, run_indexer_forever, sync_once

    # The agent writes the schema it depends on, so it can run before (or
    # without) the web app ever having started.
    init_db()

    if args.backfill_previews:
        from sync import backfill_previews
        return backfill_previews()

    if args.once:
        failed = 0
        for account in cfg.accounts:
            log.info("one-shot sync...", account.email)
            try:
                sync_once(account, cfg, reconcile=True)
            except Exception as e:  # noqa: BLE001
                # --once has no retry loop to absorb this, and exiting 0 after a
                # failed pass would tell a cron job the mail is up to date.
                failed += 1
                log.error(f"sync failed: {e!r}", account.email)
                advice = log.hint(e)
                if advice:
                    log.warn(advice, account.email)
        # Extraction is a thread of its own in continuous mode, which --once
        # never starts. Drain it inline instead, or a one-shot run would fetch
        # the mail and leave every attachment unindexed and unsearchable.
        index_once()
        return 1 if failed else 0

    # Only for the continuous mode: --once has no wait for a refresh to cut short.
    commands.start()

    threads = []
    for account in cfg.accounts:
        t = threading.Thread(target=run_account_forever, args=(account, cfg),
                             name=f"sync-{account.email}", daemon=True)
        t.start()
        threads.append(t)
    # One indexer for all accounts: the attachment queue is global.
    indexer = threading.Thread(target=run_indexer_forever, args=(cfg,),
                               name="indexer", daemon=True)
    indexer.start()
    threads.append(indexer)
    log.info(f"meerail-agent running for {len(cfg.accounts)} account(s): "
             f"{', '.join(a.email for a in cfg.accounts)}. Ctrl-C to stop.")
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        log.info("stopping.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
