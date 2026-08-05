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
  ./main.py --requeue-abandoned   # re-queue work an older agent gave up on
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

# The agent shares the `core` package with the server, which lives one level up.
# run.sh exports PYTHONPATH for this, but do it here too so the script also works
# when invoked directly from an activated venv.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# noqa: E402 throughout — these must follow the sys.path bootstrap above.
from core.config import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    LEGACY_AGENT_CONFIG,
    config_file_path,
    get_settings,
)


def main() -> int:
    parser = argparse.ArgumentParser(prog="meerail-agent")
    parser.add_argument("--config", default=None, help="path to meerail.toml")
    parser.add_argument("--once", action="store_true", help="sync once and exit")
    parser.add_argument("--test", action="store_true",
                        help="check every connection (database, Tika, IMAP, SMTP) and exit")
    parser.add_argument("--backfill-previews", action="store_true",
                        help="render previews for already-stored attachments, then exit")
    parser.add_argument("--requeue-abandoned", action="store_true",
                        help="put actions an older agent gave up on (including unsent "
                             "mail) back in the queue, then exit")
    args = parser.parse_args()

    # Must precede the first get_settings() — and so any core.* import that
    # reaches one, which is why core.database and sync are imported below rather
    # than at the top of the file.
    if args.config:
        os.environ["MEERAIL_CONFIG"] = args.config

    if config_file_path() is None and LEGACY_AGENT_CONFIG.exists():
        print(f"{LEGACY_AGENT_CONFIG} is no longer read — the server and the agent now "
              f"share one {DEFAULT_CONFIG_PATH}.\n"
              f"Fold this install's .env and agent config into it with:\n"
              f"\n    python -m core.config migrate\n", file=sys.stderr)
        return 1

    cfg = get_settings()
    if not cfg.accounts:
        where = cfg.config_path or "the environment"
        print(f"No [[agent.account]] entries in {where}.", file=sys.stderr)
        return 1

    # Before init_db: --test is read-only and must not create the schema, so that
    # it stays safe to run against a database you haven't committed to yet.
    if args.test:
        import preflight
        return preflight.run(cfg)

    import commands
    import log
    from core.database import init_db
    from core.version import VERSION
    from sync import index_once, run_account_forever, run_indexer_forever, sync_once

    # First line of every run: which build this is. `docker logs` shows nothing
    # about the image tag it came from, and "which version are you on?" is where
    # most of the answers start.
    log.info(f"meerail-agent {VERSION}")

    # The agent writes the schema it depends on, so it can run before (or
    # without) the web app ever having started.
    init_db()

    # Only worth a line when it is off: this is the one setting that quietly
    # discards something, so say so rather than let a later export find gaps.
    if not cfg.store_raw_mime:
        log.info("store_raw_mime is off — new mail is stored without its original bytes.")

    if args.backfill_previews:
        from sync import backfill_previews
        return backfill_previews()

    # Both of these read the queue for work an older agent retired as
    # permanently failed. Nothing does that any more (agent/actions.py), but the
    # rows outlive the upgrade, and an unsent message is worth saying out loud
    # every single run until someone deals with it.
    import actions
    from core.database import SessionLocal

    db = SessionLocal()
    try:
        if args.requeue_abandoned:
            count = actions.requeue_abandoned(db)
            log.ok(f"{count} action(s) put back in the queue. Start the agent normally "
                   "and it will work through them.")
            return 0
        actions.report_abandoned(db)
        # And what is queued right now. A run that starts with mail already
        # waiting should say so on its first breath rather than leaving it to be
        # inferred from an SMTP error twenty lines later — or, when the mail
        # server is unreachable and the send is never even attempted, from
        # nothing at all.
        actions.report_waiting(db)
    finally:
        db.close()

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
        # the mail and leave every attachment unindexed and unsearchable. The
        # window prune rides the same call, for the same reason.
        #
        # report=True because this is the phase that runs after the last "sync
        # complete" line: on a first run it is minutes of work behind a silent
        # prompt, which reads as a hung process (issue #3).
        extracted, thumbed, pruned = index_once(cfg.content_window_months, report=True)
        log.ok(f"indexing complete — {extracted} attachment(s) extracted, "
               f"{thumbed} preview(s) rendered, {pruned} message(s) pruned to headers",
               "indexer")
        # Said again on the way out, because this run is the last chance to say
        # it: --once exits, and anything it could not send stays in the outbox
        # until something starts the agent again.
        db = SessionLocal()
        try:
            actions.report_waiting(db)
        finally:
            db.close()
        # The exit itself needs saying too: --once is the Getting Started
        # command, and the last thing it printed used to be a sync line that
        # left it ambiguous whether the process was done or stalled.
        log.info("one-shot run finished — start without --once to keep syncing "
                 "and to keep the web app's agent status green.")
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
