#!/usr/bin/env python3
"""Re-read the messages an older parser read wrongly, from the bytes it kept.

Three faults in the MIME walk, all fixed in core/mail/parse.py, left mail stored
short of what arrived:

  * A forward made by attaching the original — ``message/rfc822``, which is what
    Thunderbird's "forward as attachment" produces and what any signed forward
    has to be — was stored as the covering note alone. The forwarded message
    reached neither the reader nor the search corpus, and the file offered for
    it held zero bytes.
  * Every ``multipart/signed`` message had its whole body subtree filed as an
    attachment named "attachment" holding nothing, while the real attachments
    inside it were never reached.
  * A calendar invitation carried only inside ``multipart/alternative`` was
    not stored as an attachment at all. That is how Outlook and Exchange send
    one, a ``text/calendar`` part next to the text and the HTML with no file
    attached anywhere, so the message looked like any other mail and offered
    nothing to add to a calendar. Gmail and Thunderbird invitations were not
    hit, because they attach the same event as an invite.ics file as well.

All three are recoverable without going back to the mail server: the parse is a
function of ``messages.raw_mime``, which is still there for anything stored with
agent.store_raw_mime on. This re-runs it. Rows whose raw copy was not kept
(store_raw_mime off, or content pruned out of the window) cannot be recovered
here and are reported as skipped — a full re-fetch is the only route to those.

Dry run by default:

  tools/reparse_forwards.py                   # what would change
  tools/reparse_forwards.py --apply
  tools/reparse_forwards.py --apply --all     # every stored message, not just
                                              # the ones the fault is visible in

The first two faults leave a mark in the attachment rows, and that mark is what
the default run picks its messages by. A missed invitation leaves no row at all,
so it has to be found in the bytes instead. ``--calendar`` re-reads only the
messages whose stored raw copy mentions text/calendar or application/ics, upper
or lower case alike. It cannot be combined with ``--all``. Gmail invitations
it finds come out as already correct. Finding the candidates means reading every
stored raw copy on the database side, so on a large store the first line takes
a while to appear. Dry run first, then apply:

  tools/reparse_forwards.py --calendar
  tools/reparse_forwards.py --calendar --apply

Headers, flags, threading and folder placement are untouched — this rewrites
only what comes out of the body: body_text, body_html, the snippet, the search
text and the attachment rows. Attachment text extraction and thumbnails are put
back in the queue, so Tika will re-read what it needs to.

Best run with the agent stopped (``make agent-service-stop``, or stop the
compose agent): nothing here conflicts with a sync, but a message re-parsed
while it is being re-fetched is work done twice.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import func, select                                   # noqa: E402

from core.database import SessionLocal                                # noqa: E402
from core.mail.parse import (                                         # noqa: E402
    CALENDAR_TYPES, is_calendar_part, parse_email,
)
from core.mail.store import replace_content                           # noqa: E402
from core.models import Attachment, Message                           # noqa: E402

# What the fault looks like from the outside, without reading a single blob: an
# attachment row holding a container (a multipart is never a file) or an empty
# attached message. Both are things only the old parser produced, so this picks
# out the affected mail cheaply and exactly.
_SUSPECT = (
    select(Attachment.message_pk)
    .where(Attachment.content_type.like("multipart/%")
           | (Attachment.content_type == "message/rfc822"))
    .distinct()
)

# A missed invitation left no attachment row to pick it out by, so --calendar
# looks in the stored bytes instead. raw_mime is bytea, which has LIKE but no
# ILIKE, and MIME types are case-insensitive, so a plain byte match would quietly
# skip a sender who writes Text/Calendar. encode(..., 'escape') turns the bytes
# into text that keeps every ASCII byte as it was and writes NULs and high bytes
# as backslash escapes, so no message can make the conversion fail, and ~* then
# matches either type in any case. The type names hold only letters and a slash,
# so they go into the pattern as they are. Every stored raw copy is read to
# answer it, which is slow on a large store, but this is a one-off backfill.
_CALENDAR_RAW = func.encode(Message.raw_mime, "escape").regexp_match(
    "|".join(sorted(CALENDAR_TYPES)), flags="i")


def _candidates(db, every: bool, calendar: bool = False):
    """Ids of the messages to re-read, oldest first so a resumed run is orderly."""
    stmt = select(Message.id).where(Message.content_status == "full")
    if calendar:
        stmt = stmt.where(_CALENDAR_RAW)
    elif not every:
        stmt = stmt.where(Message.id.in_(_SUSPECT))
    return list(db.execute(stmt.order_by(Message.id)).scalars().all())


def _shown_calendars(rows) -> int:
    """How many calendar files the reader lists, out of (content_type, filename,
    is_inline) triples. It is the fourth number in a shape, because an older
    parser could store an invitation and still hide it. A calendar part marked
    inline was kept as an inline row, which the reader leaves out, and the
    other three numbers do not move when that is put right."""
    return sum(1 for ctype, name, inline in rows
               if not inline and is_calendar_part(ctype, name))


def _shape(db, msg: Message) -> tuple[int, int, int, int]:
    """The numbers this changes. The first three make the before/after line."""
    files, payload = db.execute(
        select(func.count(Attachment.id), func.coalesce(func.sum(Attachment.size_bytes), 0))
        .where(Attachment.message_pk == msg.id)
    ).one()
    shown = _shown_calendars(db.execute(
        select(Attachment.content_type, Attachment.filename, Attachment.is_inline)
        .where(Attachment.message_pk == msg.id)
    ).all())
    return len(msg.body_text or "") + len(msg.body_html or ""), files, payload, shown


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="write the re-parse (default: report only)")
    which = ap.add_mutually_exclusive_group()
    which.add_argument("--all", action="store_true", dest="every",
                       help="re-read every stored message, not only the suspect ones")
    which.add_argument("--calendar", action="store_true",
                       help="re-read only messages whose raw copy mentions text/calendar "
                            "or application/ics, to file invitations that came only "
                            "inside multipart/alternative (cannot be combined with --all)")
    ap.add_argument("--batch", type=int, default=100,
                    help="messages per commit (default: 100)")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many (0: no limit)")
    args = ap.parse_args()

    changed = unchanged = skipped = 0
    grew_body = grew_files = 0

    with SessionLocal() as db:
        ids = _candidates(db, args.every, args.calendar)
        if args.limit:
            ids = ids[:args.limit]
        scope = ("" if args.every else
                 " (mentioning a calendar only)" if args.calendar else
                 " (suspect only; --all for the whole store)")
        print(f"{len(ids)} message(s) to re-read{scope}")

        for done, mid in enumerate(ids, 1):
            msg = db.get(Message, mid)
            if msg is None:
                continue
            # raw_mime is deferred on the model; touching it is the load.
            raw = msg.raw_mime
            if not raw:
                skipped += 1
                continue

            before = _shape(db, msg)
            after_parse = parse_email(raw)
            after = (len(after_parse.body_text or "") + len(after_parse.body_html or ""),
                     len(after_parse.attachments),
                     sum(len(a.payload) for a in after_parse.attachments),
                     _shown_calendars((a.content_type, a.filename, a.is_inline)
                                      for a in after_parse.attachments))
            if before == after:
                unchanged += 1
                continue

            changed += 1
            grew_body += after[0] > before[0]
            grew_files += after[2] > before[2]
            print(f"  #{msg.id} body {before[0]}->{after[0]} chars, "
                  f"files {before[1]}->{after[1]} ({before[2]}->{after[2]} bytes)"
                  f"  {(msg.subject or '(no subject)')[:60]}")

            if args.apply:
                replace_content(db, msg, raw)
                # _store_content re-decides this from the current setting, and an
                # install that has since turned store_raw_mime off would have the
                # column cleared as a side effect of a re-read. The bytes were
                # already kept; keep them.
                msg.raw_mime = raw
                if done % args.batch == 0:
                    db.commit()

        if args.apply:
            db.commit()

    print(f"\n{changed} changed, {unchanged} already correct, "
          f"{skipped} skipped (no raw copy stored)")
    print(f"{grew_body} recovered body text, {grew_files} recovered attachment bytes")
    if changed and not args.apply:
        print("\nDry run. Re-run with --apply to write it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
