#!/usr/bin/env python3
"""Import an mbox file into meerail's database, indexing as it goes.

Mail normally arrives through the agent: IMAP fetch -> parse -> Postgres. An
mbox has no server behind it, so this walks the file instead and drives the same
``core.ingest`` calls the agent's sync pass makes — one account, one folder, one
placement per message — and then runs the indexing phase (Tika attachment text,
previews, search_text) that the agent's indexer thread would have run.

What lands is ordinary mail: threaded, searchable, with attachments extracted.
What it is not is *synced* mail. Nothing on the far end corresponds to it, so
there is no cursor to advance and no flag to write back, and the account it
lands in should be one no agent is configured for — see ``_agent_owns`` for why
importing into a live account is refused by default.

Run it through ``tools/import-mbox.sh`` (which builds the venv), or directly
with an interpreter that has ``agent/requirements.txt`` installed:

  tools/import-mbox.sh archive.mbox
  tools/import-mbox.sh archive.mbox --account old@example.com --folder Archive
"""

from __future__ import annotations

import argparse
import mailbox
import os
import re
import sys
import time
from pathlib import Path

# The tool shares the `core` package with the server and the agent, which lives
# one level up. import-mbox.sh exports PYTHONPATH for this; do it here too so
# the script also works when invoked directly from an activated venv.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# How many messages go in one transaction. Large enough that the per-commit cost
# disappears into the parse, small enough that Ctrl-C on an hour-long import
# loses seconds of work rather than the lot.
BATCH = 200

# How often a run that is going fine says so.
PROGRESS_EVERY = 5.0

# Header block of a message: everything up to the first blank line. Read for the
# mbox status flags below, which is all we need from it before parsing properly.
_HEADER_END = re.compile(rb"\r?\n\r?\n")
_STATUS_RE = re.compile(rb"^(?:Status|X-Status):[ \t]*(.*)$", re.MULTILINE | re.IGNORECASE)

# mbox status letters (see mailbox.mboxMessage): R read, O old, D deleted,
# F flagged, A answered. Everything else — Thunderbird writes several — is
# ignored rather than guessed at.
_FLAG_LETTERS = {"R": "seen", "D": "deleted", "F": "flagged", "A": "answered"}


def mbox_flags(raw: bytes) -> dict:
    """The IMAP-shaped flags an mbox message carries in its Status headers.

    Most mbox files have none at all — a Gmail Takeout export writes neither
    header — in which case every message reads as unread, which is what
    ``--keep-unread`` is for.
    """
    end = _HEADER_END.search(raw)
    head = raw[: end.start()] if end else raw[:65536]
    letters = "".join(
        m.group(1).decode("ascii", "ignore") for m in _STATUS_RE.finditer(head)
    )
    return {name: (letter in letters) for letter, name in _FLAG_LETTERS.items()}


def default_email(mbox_path: Path) -> str:
    """An address for the account an unnamed import lands in.

    ``.local`` because it must never resolve: this account exists to hold mail,
    and mail sent to (or from) it has nowhere to go.
    """
    slug = re.sub(r"[^a-z0-9._-]+", "-", mbox_path.stem.lower()).strip("-._") or "mbox"
    return f"{slug}@imported.local"


def _agent_owns(cfg, email: str) -> bool:
    """Is this address one the agent on this machine syncs?

    Importing into it is refused by default, and the reason is not tidiness. The
    agent deletes folders the IMAP server does not list (``prune_mailboxes``),
    and with the last placement of each message its content — so an imported
    folder, which exists nowhere but here, is removed on the next pass and takes
    the imported mail with it. --force is for someone who has read that sentence.
    """
    return any(acc.email.strip().lower() == email for acc in cfg.accounts)


def _open_mbox(path: Path):
    """The mbox, or a clear error. Never creates one: a typo'd path must not
    silently produce an empty mailbox and an import of nothing."""
    try:
        return mailbox.mbox(str(path), factory=None, create=False)
    except mailbox.NoSuchMailboxError:
        raise SystemExit(f"No mbox file at {path}") from None


class Progress:
    """Throttled 'still going' lines for a run measured in minutes."""

    def __init__(self, every: float = PROGRESS_EVERY) -> None:
        self._every = every
        self._last = time.monotonic()
        self._start = self._last

    def maybe(self, line: str) -> None:
        now = time.monotonic()
        if now - self._last < self._every:
            return
        self._last = now
        print(f"  {line}", flush=True)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start


def import_messages(db, account, folder: str, box, *, keep_unread: bool,
                    batch: int = BATCH) -> tuple[int, int]:
    """Store every message in ``box`` as a placement in ``folder``.

    Returns (imported, skipped). Skipped means the account already holds that
    message in that folder — so re-running an interrupted import continues it
    rather than filing everything a second time.
    """
    from sqlalchemy import select

    from core import ingest
    from core.mail.parse import parse_email
    from core.mail.store import ingest_raw, same_message
    from core.models import Message, MessageLocation

    mailbox_row = ingest.register_folder(db, account, folder)
    # UIDs are ours to invent — there is no server holding the real ones — so
    # they carry on from wherever a previous import of this folder stopped.
    # Positive and increasing, because that is what everything downstream reads
    # them as (a negative UID means "placement the server has not seen yet").
    next_uid = mailbox_row.last_uid + 1

    imported = skipped = batch_imported = 0
    progress = Progress()
    keys = box.keys()
    total = len(keys)
    print(f"Importing {total} message(s) from the mbox into {folder}...", flush=True)

    for n, key in enumerate(keys, start=1):
        # The message's own bytes: the "From " separator line is dropped and CRLF
        # is normalised to LF, both by mailbox itself. What is deliberately left
        # alone is the escaping an mbox writer adds to body lines that start with
        # "From " (they arrive as ">From "). Undoing it is a guess — a genuine
        # quoted line reading ">From ..." is written unchanged by most writers,
        # so unescaping would corrupt it — and the artifact is one character in
        # a body line that rarely occurs.
        raw = box.get_bytes(key)
        # Parsed once, here, and handed to ingest_raw: the dedup key decides
        # whether this message is stored at all, and parsing a mailbox twice
        # means decoding every attachment twice.
        parsed = parse_email(raw)

        # "Is this message already in this folder?", and not "is its Message-ID".
        # A Message-ID is what the store files mail *under*, not proof of what a
        # message is: two different mails can carry the same one, and matching on
        # the key alone dropped the second of them here — permanently, and with
        # no mention in the count, because the importer had already decided it
        # was a message it held. Both keys are looked up (the id, and the bytes,
        # which is where a collision is filed) and the candidates are then asked
        # whether they are actually this message.
        candidates = db.execute(
            select(Message)
            .join(MessageLocation, MessageLocation.message_pk == Message.id)
            .where(
                Message.account_id == account.id,
                Message.dedup_key.in_((parsed.dedup_key, parsed.content_key)),
                MessageLocation.mailbox_id == mailbox_row.id,
            )
        ).scalars().all()
        if any(same_message(m, parsed) for m in candidates):
            skipped += 1
        else:
            flags = mbox_flags(raw)
            if not keep_unread:
                # An archive being imported is not new mail, and forty thousand
                # unread messages is not a state anyone wants their inbox in.
                flags["seen"] = True
            ingest_raw(db, account, mailbox_row, next_uid, flags, raw, parsed=parsed)
            next_uid += 1
            imported += 1
            batch_imported += 1

        if n % batch == 0:
            ingest.advance_cursor(db, mailbox_row, next_uid - 1)
            ingest.note_ingested(account, mailbox_row, batch_imported)
            db.commit()
            batch_imported = 0
            rate = n / max(progress.elapsed, 0.001)
            progress.maybe(f"{n}/{total} read — {imported} imported, "
                           f"{skipped} already here ({rate:.0f}/s)")

    ingest.advance_cursor(db, mailbox_row, max(next_uid - 1, 0))
    ingest.note_ingested(account, mailbox_row, batch_imported)
    db.commit()
    return imported, skipped


def drain(db, fn, label: str) -> int:
    """Run one of the indexer's queues to empty. Returns how many it processed.

    Both queues are global rather than per-account: the import is what filled
    them, but a running agent's backlog drains here too, which is a bonus rather
    than a problem.
    """
    total = 0
    progress = Progress()
    while True:
        n = fn(db)
        db.commit()
        if not n:
            return total
        total += n
        progress.maybe(f"{label}: {total} so far")


def run(mbox_path: Path, email: str, folder: str, *, keep_unread: bool,
        index: bool, batch: int) -> int:
    from sqlalchemy import select

    from core import events, ingest
    from core.config import get_settings
    from core.database import SessionLocal, init_db
    from core.mail import thumbs, tika
    from core.models import Account

    cfg = get_settings()
    email = email.strip().lower()

    box = _open_mbox(mbox_path)

    print(f"Database: {re.sub(r'://[^@/]*@', '://***@', cfg.database_url)}", flush=True)
    init_db()

    db = SessionLocal()
    try:
        account = db.execute(
            select(Account).where(Account.email == email)
        ).scalar_one_or_none()
        if account is None:
            account = Account(
                email=email,
                label=email.split("@")[0],
                # Nothing is going to backfill this account — the mbox is all
                # there is — so saying otherwise would leave the status panel
                # reporting a first sync that never finishes.
                backfill_complete=True,
            )
            db.add(account)
            db.flush()
            db.commit()
            events.publish({"type": "accounts", "account": email})
            print(f"Created account {email} (id {account.id}).", flush=True)
        else:
            print(f"Using the existing account {email} (id {account.id}).", flush=True)

        imported, skipped = import_messages(
            db, account, folder, box, keep_unread=keep_unread, batch=batch
        )
        print(f"Imported {imported} message(s); {skipped} were already in {folder}.",
              flush=True)

        if not index:
            print("Skipping indexing (--no-index). Attachment text and previews stay "
                  "queued; the agent's indexer picks them up if one is running.")
            return 0

        if not tika.health():
            print(f"! Tika is not answering at {cfg.tika_url} — attachment text cannot "
                  "be extracted. The attachments stay queued; run this again (or start "
                  "the agent) once Tika is up.", flush=True)
            extracted = 0
        else:
            extracted = drain(db, ingest.extract_pending, "attachments indexed")
        if not thumbs.available():
            # thumb_pending returns 0 rather than burning the queue to 'error',
            # so this would otherwise read as "nothing to render".
            print("! the preview renderer (PyMuPDF, Pillow) is not installed here — "
                  "previews stay queued for the agent.", flush=True)
            thumbed = 0
        else:
            thumbed = drain(db, ingest.thumb_pending, "previews rendered")
        print(f"Indexed {extracted} attachment(s) and rendered {thumbed} preview(s).",
              flush=True)

        contacts = _rebuild_contacts(db, cfg)
        if contacts is not None:
            print(f"Rebuilt the address book: {contacts} contact(s).", flush=True)

        if cfg.content_window_months:
            print(f"! agent.content_window_months is {cfg.content_window_months} — a "
                  "running agent strips the *content* of imported mail older than "
                  "that back to headers. Set it to 0 to keep the archive whole.",
                  flush=True)
        return 0
    finally:
        db.close()


def _rebuild_contacts(db, cfg) -> int | None:
    """Refresh compose autocomplete over the newly imported mail.

    The server rebuilds this on its own schedule (every six hours), so a failure
    here costs a wait rather than the feature — hence the swallow. Returns None
    when it did not run.
    """
    try:
        from app.contacts import rebuild_contact_pairs, rebuild_contacts
    except ImportError:
        return None
    try:
        count = rebuild_contacts(db, cfg.contacts_scan_years)
        rebuild_contact_pairs(db, cfg.contacts_scan_years)
        db.commit()
        return count
    except Exception as e:  # noqa: BLE001
        db.rollback()
        print(f"! address book rebuild failed ({e!r}) — the server redoes it "
              "periodically anyway.", flush=True)
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="import-mbox",
        description="Import an mbox file into meerail as its own account.",
        epilog="Mail lands as an ordinary folder: threaded, searchable and with "
               "attachment text indexed. Nothing is sent, and nothing is synced back.",
    )
    parser.add_argument("mbox", type=Path, help="path to the .mbox file")
    parser.add_argument("-a", "--account", default=None,
                        help="account to import into; created if it does not exist "
                             "(default: <filename>@imported.local)")
    parser.add_argument("-f", "--folder", default="INBOX",
                        help="folder the mail lands in (default: INBOX). A name like "
                             "Sent or Archive takes that folder's role in the sidebar.")
    parser.add_argument("--keep-unread", action="store_true",
                        help="take read/unread from the mbox Status headers instead of "
                             "marking everything read (most exports carry no flags at "
                             "all, so this marks the whole import unread)")
    parser.add_argument("--no-index", action="store_true",
                        help="import only: leave attachment text and previews queued")
    parser.add_argument("--batch", type=int, default=BATCH,
                        help=f"messages per transaction (default: {BATCH})")
    parser.add_argument("--config", default=None, help="path to meerail.toml")
    parser.add_argument("--force", action="store_true",
                        help="import into an account this machine's agent syncs — see "
                             "the warning it prints")
    args = parser.parse_args(argv)

    # Must precede the first get_settings(), and so any core.* import that reaches
    # one — which is why every core import in this file is inside a function.
    if args.config:
        os.environ["MEERAIL_CONFIG"] = args.config

    if args.batch < 1:
        parser.error("--batch must be at least 1")

    from core.config import get_settings

    cfg = get_settings()
    email = (args.account or default_email(args.mbox)).strip().lower()
    if _agent_owns(cfg, email) and not args.force:
        print(
            f"Refusing to import into {email}: the agent configured in "
            f"{cfg.config_path or 'the environment'} syncs that account.\n"
            f"The agent deletes folders its IMAP server does not list, so the "
            f"imported mail would be removed on its next pass.\n"
            f"Import into an account of its own (the default), or pass --force if "
            f"you know the folder exists on the server too.",
            file=sys.stderr,
        )
        return 1

    from sqlalchemy.exc import OperationalError

    try:
        return run(args.mbox, email, args.folder, keep_unread=args.keep_unread,
                   index=not args.no_index, batch=args.batch)
    except OperationalError as e:
        print(f"\nCannot reach the database: {e.orig or e}\n"
              f"Is the stack up? ./meerail.sh start", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # Every batch was committed as it went, so what is in the database is
        # whole — say so, because "interrupted" otherwise reads as "corrupted".
        print("\nInterrupted. Everything imported so far is saved; re-run the same "
              "command to continue where it stopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
