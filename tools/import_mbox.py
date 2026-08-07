#!/usr/bin/env python3
"""Import a mailbox from disk into meerail's database, indexing as it goes.

Mail normally arrives through the agent: IMAP fetch -> parse -> Postgres. A file
on disk has no server behind it, so this walks it instead and drives the same
``core.ingest`` calls the agent's sync pass makes — one account, one folder, one
placement per message — and then runs the indexing phase (Tika attachment text,
previews, search_text) that the agent's indexer thread would have run.

Two things on disk are called an mbox and only one of them is a file:

* an actual mbox — messages one after another, ``From `` lines between them.
  Thunderbird and Gmail Takeout write these, and so does Mail.app's *Export
  Mailbox*, except that it wraps it in a folder with the mbox inside called
  ``mbox``.
* Apple Mail's own store, ``~/Library/Mail/V10/<account>/<Folder>.mbox``, which
  is a *directory* and contains no mbox at all: one ``.emlx`` file per message
  under ``<UUID>/Data/``, with attachments alongside them rather than in them.

Both go in — see ``_open_mbox`` — and both land as the same ordinary mail:
threaded, searchable, with attachments extracted. What it is not is *synced*
mail. Nothing on the far end corresponds to it, so there is no cursor to advance
and no flag to write back, and the account it lands in should be one no agent is
configured for — see ``_agent_owns`` for why importing into a live account is
refused by default.

Run it through ``tools/import-mbox.sh`` (which builds the venv), or directly
with an interpreter that has ``agent/requirements.txt`` installed:

  tools/import-mbox.sh archive.mbox
  tools/import-mbox.sh archive.mbox --account old@example.com --folder Archive
  tools/import-mbox.sh ~/Library/Mail/V10/*/Lists.mbox --folder Lists
"""

from __future__ import annotations

import argparse
import base64
import io
import mailbox
import os
import plistlib
import re
import sys
import time
from email import message_from_bytes
from email.generator import BytesGenerator
from email.policy import compat32
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


# Apple Mail keeps its state in a plist on the end of every .emlx, as one
# integer of bit flags. Only the four that mean something here are read; the
# rest of the word (attachment count, priority, junk, and a dozen bits nobody
# has ever written down) is left alone rather than guessed at.
_EMLX_FLAG_BITS = {0: "seen", 1: "deleted", 2: "answered", 4: "flagged"}

# The header Mail leaves on a part whose bytes it has put in the Attachments
# directory instead. Its presence is what makes a message file ".partial".
_APPLE_LENGTH = "X-Apple-Content-Length"


def read_emlx(path: Path) -> tuple[bytes, dict]:
    """The message inside an .emlx, and Mail's plist of state about it.

    The format is a decimal byte count, a newline, exactly that many bytes of
    RFC 5322 message, then the plist. A file that does not start with a count is
    read as a plain message: some tools write .eml under an .emlx name, and
    losing real mail over a missing header is worse than reading it.
    """
    data = path.read_bytes()
    head, sep, rest = data.partition(b"\n")
    try:
        length = int(head.strip())
    except ValueError:
        return data, {}
    if not sep or length > len(rest):    # truncated mid-write; take what is there
        length = len(rest)
    trailer = rest[length:].lstrip()
    try:
        meta = plistlib.loads(trailer) if trailer else {}
    except Exception:  # noqa: BLE001 — a damaged plist costs flags, not the message
        meta = {}
    return rest[:length], meta if isinstance(meta, dict) else {}


def emlx_flags(meta: dict) -> dict:
    """The IMAP-shaped flags an .emlx carries in its plist."""
    bits = meta.get("flags")
    bits = bits if isinstance(bits, int) else 0
    return {name: bool(bits >> bit & 1) for bit, name in _EMLX_FLAG_BITS.items()}


def _attachment_files(emlx_path: Path) -> list[Path]:
    """The files Mail parked beside this message, in part order.

    ``…/Data/3/Messages/1234.partial.emlx`` puts them in
    ``…/Data/3/Attachments/1234/<part>/<filename>``.
    """
    base = emlx_path.parent.parent / "Attachments" / emlx_path.name.split(".")[0]
    try:
        parts = sorted((d for d in base.iterdir() if d.is_dir()), key=_numeric_first)
    except OSError:
        return []
    found = []
    for part in parts:
        found.extend(sorted(f for f in part.rglob("*")
                            if f.is_file() and not f.name.startswith(".")))
    return found


def restore_apple_attachments(raw: bytes, emlx_path: Path) -> tuple[bytes, int]:
    """Put back the attachments Apple Mail stores beside a message, not in it.

    A message with attachments is written as ``<n>.partial.emlx``: the MIME
    structure is all there, but each attachment part is empty and carries
    ``X-Apple-Content-Length`` in place of its bytes, which live in a sibling
    Attachments directory. Splicing them back in is what makes the import an
    archive rather than a table of contents.

    Returns the message — rebuilt only if something was actually put back, so an
    ordinary .emlx passes through byte-for-byte — and the number of parts whose
    file was not there. Those stay empty, which is what Mail itself shows for an
    attachment it never downloaded.
    """
    if _APPLE_LENGTH.encode() not in raw:
        return raw, 0
    msg = message_from_bytes(raw)
    holes = [p for p in msg.walk() if p.get(_APPLE_LENGTH) is not None]
    if not holes:
        return raw, 0

    files = _attachment_files(emlx_path)
    restored = missing = 0
    for n, part in enumerate(holes):
        blob = _attachment_for(part, files, n, len(holes))
        if blob is None:
            missing += 1
            continue
        part.set_payload(base64.encodebytes(blob).decode("ascii"))
        del part[_APPLE_LENGTH]
        if part.get("Content-Transfer-Encoding") is None:
            part["Content-Transfer-Encoding"] = "base64"
        else:
            part.replace_header("Content-Transfer-Encoding", "base64")
        restored += 1

    if not restored:
        return raw, missing
    buf = io.BytesIO()
    # maxheaderlen=0 so headers that were already folded are not refolded, and
    # mangle_from_ off so a body line starting "From " is left as it is.
    BytesGenerator(buf, mangle_from_=False, maxheaderlen=0, policy=compat32).flatten(msg)
    return buf.getvalue(), missing


def _attachment_for(part, files: list[Path], index: int, total: int) -> bytes | None:
    """Which file on disk belongs to this empty part.

    By name first, because that is the one thing both ends agree on. Position is
    the fallback — inline images often have no filename — and only when the two
    lists are the same length, so a partial match never grafts one message's
    photo onto another part.
    """
    name = part.get_filename()
    if name:
        for match in (lambda f: f.name == name, lambda f: f.name.lower() == name.lower()):
            for f in files:
                if match(f):
                    return _read_bytes(f)
    if len(files) == total and index < len(files):
        return _read_bytes(files[index])
    return None


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _numeric_first(path: Path) -> tuple[int, str]:
    """Sort key for names Mail numbers rather than letters: 2 before 10."""
    m = re.match(r"(\d+)", path.name)
    return (int(m.group(1)) if m else 0, path.name)


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


class MboxFile:
    """An actual mbox file: messages end to end, ``From `` lines between them."""

    def __init__(self, path: Path) -> None:
        # Never creates one: a typo'd path must not silently produce an empty
        # mailbox and an import of nothing.
        self._box = mailbox.mbox(str(path), factory=None, create=False)

    def keys(self):
        return self._box.keys()

    def read(self, key) -> tuple[bytes, dict]:
        # The message's own bytes: the "From " separator line is dropped and
        # CRLF normalised to LF, both by mailbox itself. What is deliberately
        # left alone is the escaping an mbox writer adds to body lines that
        # start with "From " (they arrive as ">From "). Undoing it is a guess —
        # a genuine quoted line reading ">From ..." is written unchanged by most
        # writers, so unescaping would corrupt it — and the artifact is one
        # character in a body line that rarely occurs.
        raw = self._box.get_bytes(key)
        return raw, mbox_flags(raw)


class AppleMailbox:
    """Apple Mail's .mbox: a directory holding one .emlx file per message."""

    def __init__(self, paths: list[Path]) -> None:
        self._paths = paths
        # Counted rather than raised on: an archive where Mail never downloaded
        # some attachments still imports, and the run says how much of it is
        # headers only.
        self.missing_attachments = 0

    def keys(self):
        return list(range(len(self._paths)))

    def read(self, key) -> tuple[bytes, dict]:
        path = self._paths[key]
        raw, meta = read_emlx(path)
        raw, missing = restore_apple_attachments(raw, path)
        self.missing_attachments += missing
        return raw, emlx_flags(meta)


def _open_mbox(path: Path):
    """The mailbox at this path, whichever of the two shapes it is."""
    if path.is_dir():
        return _open_directory(path)
    try:
        return MboxFile(path)
    except mailbox.NoSuchMailboxError:
        raise SystemExit(f"No mbox file at {path}") from None


def _open_directory(path: Path):
    """A .mbox that is a directory — on macOS, both kinds of export are.

    Mail.app's *Export Mailbox* writes a folder with the real mbox inside it,
    called ``mbox``. Mail's own store has no mbox anywhere in it: the messages
    are .emlx files under ``<UUID>/Data/``, and a mailbox that has sub-mailboxes
    keeps those as further .mbox directories inside this one. Each of those is
    its own import, because this tool files everything into one folder.
    """
    try:
        entries = sorted(path.iterdir())
    except PermissionError:
        raise SystemExit(_no_access(path)) from None

    exported = path / "mbox"
    if exported.is_file():
        print(f"Reading the mbox inside {path.name} (a Mail.app export).", flush=True)
        return MboxFile(exported)

    children = [p for p in entries if p.is_dir() and p.name.endswith(".mbox")]
    messages = _emlx_files(path)
    if messages:
        if children:
            names = ", ".join(p.name for p in children)
            print(f"! {path.name} has sub-mailboxes ({names}) which are not part of "
                  f"this import — run the tool again on each, with its own --folder.",
                  flush=True)
        print(f"Reading {len(messages)} .emlx file(s) from {path.name} "
              f"(an Apple Mail mailbox).", flush=True)
        return AppleMailbox(messages)

    if children:
        listing = "\n  ".join(str(p) for p in children)
        raise SystemExit(
            f"{path} holds mailboxes rather than messages. Import one of these:\n"
            f"  {listing}"
        )
    raise SystemExit(
        f"Nothing to import in {path}: it holds no mbox file and no .emlx messages.\n"
        f"An Apple Mail mailbox is ~/Library/Mail/V10/<account-id>/<Folder>.mbox; "
        f"Mail.app's Mailbox > Export Mailbox writes one that works too."
    )


def _emlx_files(root: Path) -> list[Path]:
    """Every message file belonging to this mailbox, in Mail's own order.

    Anything under a nested .mbox belongs to a sub-mailbox and is left for that
    mailbox's own import — otherwise a parent folder would swallow its children.
    """
    found = [
        p for p in root.rglob("*.emlx")
        if p.is_file()
        and not any(part.endswith(".mbox") for part in p.relative_to(root).parts[:-1])
    ]
    # Mail numbers messages per mailbox, roughly in arrival order, so importing
    # in that order makes the UIDs invented here run the way the real ones did.
    return sorted(found, key=_numeric_first)


def _no_access(path: Path) -> str:
    return (
        f"Cannot read {path}.\n"
        f"macOS keeps ~/Library/Mail behind Full Disk Access: grant it to your "
        f"terminal (System Settings > Privacy & Security > Full Disk Access), "
        f"restart the terminal, and run this again."
    )


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
    print(f"Importing {total} message(s) into {folder}...", flush=True)

    for n, key in enumerate(keys, start=1):
        # The message's bytes and whatever the mailbox knows about its state —
        # mbox Status letters, or Apple Mail's plist. Both shapes answer the
        # same two calls, and nothing below cares which one it is holding.
        raw, flags = box.read(key)
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
        absent = getattr(box, "missing_attachments", 0)
        if absent:
            print(f"! {absent} attachment(s) were not on disk beside their message — "
                  f"Mail had not downloaded them from the server — so those parts came "
                  f"in empty.", flush=True)

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
        description="Import a mailbox from disk into meerail as its own account.",
        epilog="Mail lands as an ordinary folder: threaded, searchable and with "
               "attachment text indexed. Nothing is sent, and nothing is synced back.",
    )
    parser.add_argument("mbox", type=Path,
                        help="the mailbox: an mbox file, or a .mbox folder — both "
                             "Apple Mail's own (~/Library/Mail/V10/<id>/Name.mbox) "
                             "and the one Mail.app's Export Mailbox writes")
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
    except PermissionError:
        # Only reachable for an Apple Mail directory: mid-import, on a message
        # file rather than the directory _open_directory already checked.
        print(f"\n{_no_access(args.mbox)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # Every batch was committed as it went, so what is in the database is
        # whole — say so, because "interrupted" otherwise reads as "corrupted".
        print("\nInterrupted. Everything imported so far is saved; re-run the same "
              "command to continue where it stopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
