#!/usr/bin/env python3
"""Put back mail that meerail is the last remaining copy of.

A placement meerail wrote itself carries a negative UID: the message was filed
here the moment the key was pressed, and the queued move is what makes it true
on the server. When that move lands, the next sync ingests the server's copy and
the optimistic placement is retired.

When it does not land, the placement stays — and until the fixes in this
release, one way it did not land was the move deleting the message outright.
Applying a move as COPY plus \\Deleted plus EXPUNGE on a server where folders
are labels meant expunging a message that the COPY had already moved to Trash,
which is how Proton spells "delete it for good". The mail was gone from every
folder on the server while meerail went on showing it from a placement nothing
backed.

This walks those placements, checks each one against the server, and APPENDs the
message back into the folder it is supposed to be in from the raw MIME meerail
still holds. The next sync ingests it under a real UID and retires the
placement, which is the same path an ordinary move takes home.

Dry run by default. Nothing is written to the database at any point — the whole
repair is on the server, and the sync does the rest.

  tools/restore_pending.py                     # what it would do
  tools/restore_pending.py --apply
  tools/restore_pending.py --apply -a me@example.com
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import timezone
from pathlib import Path

# Shares `core` with the server and `imap` with the agent, one and two levels up
# respectively. Both are added here so the script runs from an activated venv
# without a PYTHONPATH of its own.
_REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(_REPO_ROOT), str(_REPO_ROOT / "agent")):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def _flags(row) -> list[str]:
    """The IMAP flags the restored copy should land with.

    Read state matters most: these are messages that were read and then filed,
    and putting them back unread would announce a mailbox full of new mail the
    user has already dealt with.
    """
    named = [(row.seen, "\\Seen"), (row.flagged, "\\Flagged"),
             (row.answered, "\\Answered"), (row.draft, "\\Draft")]
    return [flag for on, flag in named if on]


def _already_there(ops, folder: str, message_id: str) -> bool:
    """Is this message in that folder on the server already?

    The one check that makes a second run harmless. It also covers the case this
    tool is not the answer to: a placement still waiting on a move that simply
    has not been applied yet, where the message is exactly where the server left
    it and appending a copy would be the only damage done.
    """
    ops.select_folder(folder, readonly=True)
    return bool(ops.search(["HEADER", "MESSAGE-ID", message_id]))


def _stuck_placements(db, account_id: int):
    """Placements this account wrote itself, oldest message first.

    Columns rather than whole ORM rows on purpose: this runs against whatever
    database the installation actually has, which — being a repair tool — may
    well be one a half-finished upgrade left a column short of the models.
    Asking for the four fields the repair needs keeps it working there.
    """
    from sqlalchemy import select
    from core.models import Mailbox, Message, MessageLocation

    return db.execute(
        select(Message.message_id, Message.subject, Message.date_sent, Message.raw_mime,
               Mailbox.imap_name,
               MessageLocation.seen, MessageLocation.flagged,
               MessageLocation.answered, MessageLocation.draft)
        .join(Message, Message.id == MessageLocation.message_pk)
        .join(Mailbox, Mailbox.id == MessageLocation.mailbox_id)
        .where(MessageLocation.imap_uid < 0, Message.account_id == account_id)
        .order_by(Message.id)
    ).all()


def _restore_account(db, acc, account_id: int, apply: bool) -> tuple[int, int, int]:
    """Returns (restored, skipped, unrestorable) for one account."""
    from imap import Bridge

    rows = _stuck_placements(db, account_id)
    if not rows:
        print(f"{acc.email}: nothing waiting on a move that never landed")
        return (0, 0, 0)

    print(f"{acc.email}: {len(rows)} placement(s) with no server copy behind them")
    restored = skipped = unrestorable = 0
    bridge = None
    if apply:
        bridge = Bridge(acc)
        bridge.connect()
    try:
        for row in rows:
            when = f"{row.date_sent:%Y-%m-%d}" if row.date_sent else "??????????"
            what = f"  {when} {(row.subject or '(no subject)')[:56]!r}"
            if not row.raw_mime:
                # Nothing to put back. The message is still readable here — it
                # just cannot be handed to a server as bytes any more.
                # (agent.store_raw_mime off, or content the window has pruned.)
                print(f"{what} -> no raw copy held, cannot restore")
                unrestorable += 1
                continue
            if not apply:
                print(f"{what} -> would append to {row.imap_name}")
                restored += 1
                continue

            ops = bridge.ops()
            if _already_there(ops, row.imap_name, row.message_id):
                print(f"{what} -> already in {row.imap_name}, left alone")
                skipped += 1
                continue
            # Naive UTC in the database; IMAP wants to be told which it is, and
            # a bare datetime would be read as this machine's local time.
            stamp = row.date_sent.replace(tzinfo=timezone.utc) if row.date_sent else None
            ops.append(row.imap_name, row.raw_mime, flags=_flags(row), msg_time=stamp)
            print(f"{what} -> appended to {row.imap_name}")
            restored += 1
    finally:
        if bridge is not None:
            bridge.logout()
    return (restored, skipped, unrestorable)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="restore-pending",
        description="Put back mail whose queued move never landed on the server.",
        epilog="Dry run unless --apply is given. Re-running is safe: a message "
               "already in the folder is left alone.",
    )
    parser.add_argument("-a", "--account", default=None,
                        help="restore one account (default: every configured account)")
    parser.add_argument("--apply", action="store_true",
                        help="actually append; without it nothing is written anywhere")
    parser.add_argument("--config", default=None, help="path to meerail.toml")
    args = parser.parse_args(argv)

    # Must precede the first get_settings(), and so any core.* import that
    # reaches one — which is why the core imports here are inside functions.
    if args.config:
        os.environ["MEERAIL_CONFIG"] = args.config

    from sqlalchemy import select
    from core.config import get_settings
    from core.database import SessionLocal
    from core.models import Account

    cfg = get_settings()
    accounts = cfg.accounts
    if args.account:
        wanted = args.account.strip().lower()
        accounts = [a for a in accounts if a.email.lower() == wanted]
        if not accounts:
            print(f"No account {wanted} in {cfg.config_path or 'the environment'}",
                  file=sys.stderr)
            return 1

    totals = [0, 0, 0]
    db = SessionLocal()
    try:
        for acc in accounts:
            account_id = db.scalar(
                select(Account.id).where(Account.email == acc.email.strip().lower())
            )
            if account_id is None:
                continue          # configured but never synced; nothing to repair
            counts = _restore_account(db, acc, account_id, args.apply)
            totals = [t + c for t, c in zip(totals, counts)]
    finally:
        db.close()

    restored, skipped, unrestorable = totals
    if args.apply:
        print(f"\n{restored} restored, {skipped} already there, "
              f"{unrestorable} with no raw copy held.")
        if restored:
            print("The next sync pass ingests them and retires the placeholders.")
    else:
        print(f"\n{restored} would be restored, {unrestorable} cannot be "
              f"(no raw copy held). Re-run with --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
