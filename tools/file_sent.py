#!/usr/bin/env python3
"""File a copy of mail sent before the agent did it itself into Sent.

Sending is a hand-off to the SMTP server, and nothing in that conversation puts
the message anywhere the user can see it again. Proton Bridge and Gmail file a
copy of everything they relay into Sent on their own; a plain IMAP/SMTP server
(a university's Exchange, a Dovecot) delivers the message and keeps nothing.
The agent closes that gap now by queueing a ``save_sent`` action after every
successful send, which APPENDs the outbound MIME into the folder whose role is
``sent``. But it only does that for mail it sends from here on. Everything sent
through such an account before it learned to is still nowhere but meerail's
own Outbox row: sent, and not in Sent.

This queues the same action for that older mail. It walks each account's sent
Outbound rows that still hold their MIME, oldest first, and writes one
``save_sent`` queue row per message, the same row the agent writes after a
send. The agent does the filing on its next pass, and it looks for the message
in the Sent folder (by Message-ID, then by its bytes) before it appends, so a
copy the server already holds is not made a second time.

Nothing here talks to a mail server. The only thing written is queue rows, and
only with --apply. No IMAP connection is opened, so the mail passwords in
meerail.toml (which is read for the list of accounts) are never used.

Re-running is safe. A message that already has a ``save_sent`` row, in any
status, is left alone, and so is one the database already shows in a Sent
folder; the agent's own check before the APPEND covers whatever the database
cannot see.

An account configured with ``save_sent = false`` is skipped outright. That is
the operator saying copies are not to be filed for it (usually because the
server files its own, and the agent misread it), and a one-off tool has no
business overruling the setting the agent itself obeys. An account that leaves
``save_sent`` unset is queued all the same: whether its server files its own
copies is a question only a live connection can answer, and the agent asks it
when it applies each row, passing over the row where the answer is yes.

  tools/file_sent.py                     # what it would do
  tools/file_sent.py --apply
  tools/file_sent.py --apply -a me@example.com
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Shares `core` with the server and the agent, one level up. Added here so the
# script runs from an activated venv without a PYTHONPATH of its own. Unlike
# restore_pending.py this needs nothing from agent/: it never opens a
# connection, so the IMAP wrapper stays out of it.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _header_block(raw: str) -> bytes:
    """The header section of a stored outbound message, as bytes.

    Only the headers, because the Message-ID is all this tool reads out of a
    message and the rest of raw_mime is the body with every attachment in it,
    base64-encoded. Parsing all of that to read one header is most of the cost
    of a run on an account with years of sending behind it.

    raw_mime is stored as text, in whatever line endings built it: the
    composer's ``as_string()`` writes bare LF, and a CRLF message is just as
    possible from anything else that fills the table. So the header ends at the
    first blank line in either spelling, and it has to be the earlier of the
    two. A CRLF message holds no "\\n\\n" at all until its body, and the later
    of the two is always somewhere in the body. Encoded the way the agent
    encodes the whole message before relaying it (agent/actions.py, "send"),
    so the id read here is the one the server was given.
    """
    ends = [i for i in (raw.find("\r\n\r\n"), raw.find("\n\n")) if i >= 0]
    head = raw[:min(ends)] if ends else raw
    # "replace" rather than strict: text that came out of Postgres cannot hold
    # anything utf-8 refuses, but if it ever did, the one header wanted here is
    # ASCII either way, and a one-off repair tool dying half way through an
    # account over a byte in someone's display name helps nobody.
    return head.encode("utf-8", "replace")


def _sent_outbound(db, account_id: int):
    """This account's sent mail that still holds its MIME, oldest first.

    Columns rather than whole ORM rows, and raw_mime left out on purpose: it
    carries the attachments base64-encoded, and reading every sent message's
    at once is the whole of an account's outgoing mail held in memory for a
    loop that looks at one message at a time. Each message's MIME is read on
    its own, only once the cheaper checks have not already settled it.

    Oldest first by when the server took it, so the run reads in the order the
    mail went out. A row with no sent_at (nothing current writes one, but the
    column is nullable) sorts after the rest, by id, rather than being dropped.
    """
    from sqlalchemy import select
    from core.models import Outbound

    return db.execute(
        select(Outbound.id, Outbound.subject, Outbound.sent_at,
               Outbound.to_addrs, Outbound.cc_addrs, Outbound.bcc_addrs)
        .where(Outbound.account_id == account_id, Outbound.state == "sent",
               Outbound.raw_mime.is_not(None))
        .order_by(Outbound.sent_at.asc().nulls_last(), Outbound.id)
    ).all()


def _already_queued(db, account_id: int, outbound_id: int) -> str | None:
    """The status of a save_sent row already written for this message, if any.

    Any status counts, "done" included. A finished row means the agent has
    already dealt with the message (filed it, found it filed, or found that
    its server files its own copies), and a pending, leased or failing one
    means it is about to; in neither case is a second row anything but a
    second go at the same thing. This is what makes a
    second run of the tool, and a run after the agent has started queueing
    these itself, write nothing new. If two rows ever did end up queued for
    one message, the agent's check of the folder before it appends is what
    keeps the second from making a copy.

    The id is compared as text because that is what ``->>`` gives back; the
    payload holds it as a JSON number.
    """
    from sqlalchemy import select
    from core.models import PendingAction

    return db.scalar(
        select(PendingAction.status)
        .where(PendingAction.account_id == account_id,
               PendingAction.type == "save_sent",
               PendingAction.payload["outbound_id"].astext == str(outbound_id))
        .order_by(PendingAction.id)
        .limit(1)
    )


def _filed_in_sent(db, account_id: int, message_id: str) -> str | None:
    """The name of a Sent folder the database already shows this message in.

    Asked of the database rather than the server because this tool has no
    connection to ask with, and on a server that files its own copies (Proton,
    Gmail) this is the answer for nearly every message: the copy the server
    made has been synced in like any other mail. Queueing those anyway would
    cost an agent pass per message to find out what is already known here.

    Only a folder with role "sent" counts. The same Message-ID in the Inbox is
    what a message sent to oneself looks like, and it says nothing about
    whether there is a copy in Sent. Any placement counts, including one still
    waiting on a move to land (a negative UID) and one another client has
    flagged \\Deleted: both are a copy in Sent as far as the server is
    concerned, and the agent's own search of the folder would find them too.
    """
    from sqlalchemy import select
    from core.models import Mailbox, Message, MessageLocation

    return db.scalar(
        select(Mailbox.imap_name)
        .join(MessageLocation, MessageLocation.mailbox_id == Mailbox.id)
        .join(Message, Message.id == MessageLocation.message_pk)
        .where(Message.account_id == account_id, Message.message_id == message_id,
               Mailbox.role == "sent")
        .limit(1)
    )


def _file_account(db, acc, account_id: int, apply: bool) -> tuple[int, int]:
    """Returns (queued, skipped) for one account.

    In a dry run "queued" is what would have been; nothing is written. With
    ``apply`` every row is added to the session and committed together at the
    end, so an account is either queued in full or, if something goes wrong
    part way through, not at all, and a re-run starts from a clean slate
    rather than from wherever the last one stopped.
    """
    from sqlalchemy import select
    from core.models import Outbound, PendingAction
    from core.mail.parse import header_message_id
    from core.outbox import recipients

    if acc.save_sent is False:
        print(f"{acc.email}: configured not to file sent copies (save_sent = false), "
              f"skipped")
        return (0, 0)

    rows = _sent_outbound(db, account_id)
    if not rows:
        print(f"{acc.email}: no sent mail with its MIME still held")
        return (0, 0)

    print(f"{acc.email}: {len(rows)} sent message(s) with their MIME still held")
    queued = skipped = 0
    for row in rows:
        when = f"{row.sent_at:%Y-%m-%d}" if row.sent_at else "??????????"
        what = f"  {when} {(row.subject or '(no subject)')[:56]!r}"

        status = _already_queued(db, account_id, row.id)
        if status is not None:
            print(f"{what} -> already queued ({status}), left alone")
            skipped += 1
            continue

        raw = db.scalar(select(Outbound.raw_mime).where(Outbound.id == row.id)) or ""
        message_id = header_message_id(_header_block(raw))
        if not message_id:
            # Without one there is nothing to look the message up by, here or
            # on the server, and the agent's check before it appends is the
            # only thing that stops a second copy. Every message meerail
            # composes carries one, so this is a row built some other way; it
            # is named rather than filed on a guess.
            print(f"{what} -> no Message-ID, cannot check Sent, left alone")
            skipped += 1
            continue

        folder = _filed_in_sent(db, account_id, message_id)
        if folder is not None:
            print(f"{what} -> already in {folder}, left alone")
            skipped += 1
            continue

        if apply:
            # The same payload the agent writes after a send of its own. The
            # recipients are rebuilt from the row the way the Outbox rebuilds
            # them for a retried send (core.outbox.recipients: To, Cc, then
            # Bcc), which is the list the original send went out with.
            db.add(PendingAction(account_id=account_id, message_pk=None,
                                 type="save_sent",
                                 payload={"outbound_id": row.id,
                                          "rcpt_to": recipients(row)}))
            print(f"{what} -> queued")
        else:
            print(f"{what} -> would queue a copy into Sent")
        queued += 1

    if apply:
        db.commit()
    return (queued, skipped)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="file-sent",
        description="Queue copies of mail sent before meerail filed them itself "
                    "into each account's Sent folder.",
        epilog="Dry run unless --apply is given. Re-running is safe: a message "
               "already queued or already in Sent is left alone.",
    )
    parser.add_argument("-a", "--account", default=None,
                        help="file one account (default: every configured account)")
    parser.add_argument("--apply", action="store_true",
                        help="actually queue; without it nothing is written anywhere")
    parser.add_argument("--config", default=None, help="path to meerail.toml")
    args = parser.parse_args(argv)

    # Must precede the first get_settings(), and so any core.* import that
    # reaches one, which is why the core imports here are inside functions.
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

    queued = skipped = 0
    db = SessionLocal()
    try:
        for acc in accounts:
            account_id = db.scalar(
                select(Account.id).where(Account.email == acc.email.strip().lower())
            )
            if account_id is None:
                continue          # configured but never synced; nothing was sent
            q, s = _file_account(db, acc, account_id, args.apply)
            queued += q
            skipped += s
    finally:
        db.close()

    if args.apply:
        print(f"\n{queued} queued, {skipped} left alone (already queued, already in "
              f"Sent, or no Message-ID to check the folder with).")
        if queued:
            print("The agent files them on its next pass. It checks the Sent folder "
                  "for each one first, so a copy already on the server is not made "
                  "twice.")
    else:
        print(f"\n{queued} would be queued, {skipped} left alone. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
