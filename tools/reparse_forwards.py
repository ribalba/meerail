#!/usr/bin/env python3
"""Re-read the messages an older parser read wrongly, from the bytes it kept.

Two faults in the MIME walk, both fixed in core/mail/parse.py, left mail stored
short of what arrived:

  * A forward made by attaching the original — ``message/rfc822``, which is what
    Thunderbird's "forward as attachment" produces and what any signed forward
    has to be — was stored as the covering note alone. The forwarded message
    reached neither the reader nor the search corpus, and the file offered for
    it held zero bytes.
  * Every ``multipart/signed`` message had its whole body subtree filed as an
    attachment named "attachment" holding nothing, while the real attachments
    inside it were never reached.

Both are recoverable without going back to the mail server: the parse is a
function of ``messages.raw_mime``, which is still there for anything stored with
agent.store_raw_mime on. This re-runs it. Rows whose raw copy was not kept
(store_raw_mime off, or content pruned out of the window) cannot be recovered
here and are reported as skipped — a full re-fetch is the only route to those.

Dry run by default:

  tools/reparse_forwards.py                   # what would change
  tools/reparse_forwards.py --apply
  tools/reparse_forwards.py --apply --all     # every stored message, not just
                                              # the ones the fault is visible in

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
from core.mail.parse import parse_email                               # noqa: E402
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


def _candidates(db, every: bool):
    """Ids of the messages to re-read, oldest first so a resumed run is orderly."""
    stmt = select(Message.id).where(Message.content_status == "full")
    if not every:
        stmt = stmt.where(Message.id.in_(_SUSPECT))
    return list(db.execute(stmt.order_by(Message.id)).scalars().all())


def _shape(db, msg: Message) -> tuple[int, int, int]:
    """The three numbers this changes, for the before/after line."""
    files, payload = db.execute(
        select(func.count(Attachment.id), func.coalesce(func.sum(Attachment.size_bytes), 0))
        .where(Attachment.message_pk == msg.id)
    ).one()
    return len(msg.body_text or "") + len(msg.body_html or ""), files, payload


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="write the re-parse (default: report only)")
    ap.add_argument("--all", action="store_true", dest="every",
                    help="re-read every stored message, not only the suspect ones")
    ap.add_argument("--batch", type=int, default=100,
                    help="messages per commit (default: 100)")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many (0: no limit)")
    args = ap.parse_args()

    changed = unchanged = skipped = 0
    grew_body = grew_files = 0

    with SessionLocal() as db:
        ids = _candidates(db, args.every)
        if args.limit:
            ids = ids[:args.limit]
        print(f"{len(ids)} message(s) to re-read"
              f"{'' if args.every else ' (suspect only; --all for the whole store)'}")

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
                     sum(len(a.payload) for a in after_parse.attachments))
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
