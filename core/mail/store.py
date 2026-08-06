"""Ingest pipeline: parsed email -> DB rows (including the raw/attachment bytes).

Content is stored once per (account, dedup_key); each folder placement is a
MessageLocation. Raw MIME and attachment payloads live in the database, so the
ingesting process and the serving web app need no shared filesystem.

Attachment text extraction is deferred: attachments land with
extract_status='pending' and the agent's extraction pass fills them in, then
rebuilds the message's search_text.

Content is optional. A message can be stored as headers alone — never fetched
(outside the content window when it was seen) or fetched and later stripped as
the window slid past it. Message.content_status says which; see ingest_raw and
strip_content.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    Account,
    Attachment,
    Mailbox,
    Message,
    MessageLocation,
    PendingAction,
    Recipient,
    utcnow,
)
from .parse import (
    ParsedEmail, canonical_message_id, header_identity, html_to_text, parse_email,
)
from .threading import assign_thread
from .thumbs import should_thumb
from .tika import should_extract


def build_search_text(parsed: ParsedEmail, attachment_texts: list[str] | None = None) -> str:
    parts: list[str] = [parsed.subject, parsed.from_name, parsed.from_addr]
    for kind in ("to", "cc", "bcc", "reply_to"):
        for name, addr in parsed.recipients.get(kind, []):
            parts.extend((name, addr))
    parts.append(parsed.body_text or html_to_text(parsed.body_html))
    if attachment_texts:
        parts.extend(attachment_texts)
    return "\n".join(p for p in parts if p)


def rebuild_search_text(db: Session, message: Message) -> None:
    """Recompute search_text from stored fields + current attachment texts."""
    parts: list[str] = [message.subject, message.from_name, message.from_addr]
    recs = db.execute(
        select(Recipient.name, Recipient.address).where(Recipient.message_pk == message.id)
    ).all()
    for name, addr in recs:
        parts.extend((name or "", addr or ""))
    parts.append(message.body_text or html_to_text(message.body_html))
    att_texts = db.execute(
        select(Attachment.extracted_text).where(
            Attachment.message_pk == message.id, Attachment.extracted_text.is_not(None)
        )
    ).all()
    parts.extend(t[0] for t in att_texts if t[0])
    message.search_text = "\n".join(p for p in parts if p)


def _apply_flags(loc: MessageLocation, flags: dict) -> None:
    loc.seen = bool(flags.get("seen"))
    loc.flagged = bool(flags.get("flagged"))
    loc.answered = bool(flags.get("answered"))
    loc.draft = bool(flags.get("draft"))
    loc.deleted = bool(flags.get("deleted"))
    loc.keywords = flags.get("keywords") or []


# The flags a new placement can inherit: the two the reader writes back, and so
# the two that can be locally ahead of what the server has been told.
_INHERITED = {"seen": "\\Seen", "flagged": "\\Flagged"}


def _local_state(db: Session, message_pk: int) -> dict:
    """How this message is already flagged wherever else it sits."""
    rows = db.execute(
        select(MessageLocation.seen, MessageLocation.flagged).where(
            MessageLocation.message_pk == message_pk
        )
    ).all()
    return {"seen": any(r.seen for r in rows), "flagged": any(r.flagged for r in rows)}


def _queue_flag_catchup(
    db: Session, message_pk: int, mailbox_id: int, uid: int, ahead: list[str]
) -> None:
    """Tell the agent to bring the server's copy of this placement into step.

    Inheriting locally is only half of it. The reconcile sweep applies server
    flags verbatim, so without this the next pass reads the server's 'unseen'
    back over what was just inherited and the mail goes unread again — on a
    server that keeps flags per folder rather than per message, once a pass,
    forever.
    """
    msg = db.get(Message, message_pk)
    mailbox = db.get(Mailbox, mailbox_id)
    if msg is None or mailbox is None:
        return
    db.add(PendingAction(
        account_id=msg.account_id, message_pk=message_pk, type="setflags",
        # The UID epoch this number was read in rides along, as it does on every
        # action the UI queues — see app/routers/actions.py::_enqueue.
        payload={"folder": mailbox.imap_name, "uid": uid, "uidvalidity": mailbox.uidvalidity,
                 "add": [_INHERITED[name] for name in ahead], "remove": []},
    ))


# --- placements the server has not seen yet ----------------------------------
#
# Archiving a message removes it from the folder it was in and queues the move
# for the agent; the copy in the target folder only exists once the agent has
# run the move against IMAP and the next pass has ingested it. Between those two
# moments the message used to be in no folder at all — invisible in the source,
# invisible in the target. On a machine with a connection that is seconds away
# nobody noticed. On one that is offline for a day, the mail you filed is simply
# gone from the app until it comes back.
#
# So the move writes the target placement immediately, carrying a UID of its
# own: negative, because IMAP UIDs are positive and the sign alone says "this
# one is ours, not the server's", and -message_pk because a message has at most
# one placement per folder, which makes it unique exactly where the constraint
# needs it to be.
#
# Everything that reads placements treats it as an ordinary row — it lists,
# counts, threads and searches like any other, which is the point. The three
# places that must know the difference are: upsert_location below, which drops
# it the moment the real one arrives; prune_vanished, which must not read "the
# server never mentioned this UID" as "deleted" for a UID the server has never
# been told about; and the UI's action routes, which must not queue IMAP
# commands against it.


def pending_uid(message_pk: int) -> int:
    """The UID a placement carries while the move that creates it is queued."""
    return -message_pk


def is_pending(loc: MessageLocation) -> bool:
    """Is this placement one we wrote ourselves, ahead of the server?"""
    return loc.imap_uid < 0


# How long after a move is applied the server is still allowed to disagree with
# it. Applying a move and the server's own view of the folder catching up are
# two different events, and everything that reads "the message is not where the
# database says" during the gap between them would otherwise read it as a fact.
SETTLE_GRACE = timedelta(minutes=5)

# Statuses a move can be sitting in with nothing left to happen to it. The agent
# never retires a *failed* action — see agent/actions.py — so these are the two
# cases where it has decided the instruction cannot be carried out at all
# ("stale": the UID no longer names anything; "refused": the server said no to
# something no retry can change), plus the one an old version wrote.
#
# "undone" is the user's own version of the same verdict: the move was taken
# back out of the queue before any agent applied it (app/routers/undo.py), so
# nothing is coming and the placement it would have created is already gone.
# Reading it as in-flight would leave every later keypress on that message
# answered with "still being moved" until the grace period ran out.
SETTLED = ("stale", "refused", "error", "undone")

# How many refusals in a row end a move's claim on the local view.
#
# A queued move is believed on credit: the source placement is deleted the
# moment the key is pressed, so until the server catches up the app is showing
# something only it knows. That credit is right for a move that has not been
# tried yet — a laptop that is offline for a week still archived that mail — and
# right for one that has failed once or twice, because a Bridge restart looks
# exactly like that.
#
# It is wrong for a move the server keeps rejecting. Five attempts is a quarter
# of an hour of the backoff curve (60s doubling to the 15-minute ceiling), by
# which point "the server has not caught up yet" has stopped being a plausible
# reading: the message is in the folder the server says it is in, and the app
# saying otherwise is the failure. The action stays queued and keeps being
# retried either way — this decides only who is believed in the meantime.
STUCK_AFTER = 5


def move_in_flight(db: Session, message_pk: int) -> bool:
    """Is a move or delete the user asked for still expected to land?

    Two states look identical from the outside and want opposite answers. The
    move finished seconds ago and the server has not caught up: in flight, and
    anything that disagrees with it is too early to be believed. The move
    finished long ago and the server still disagrees: not in flight, and never
    will be — whatever the server says now is the truth.

    Read by the UI, to decide whether a second keypress on the same message can
    be honoured (app/routers/actions.py), and by the agent, to decide whether a
    placement the server lists but the database has not got is a move in
    progress or a gap to repair (agent/sync._restore_unplaced).

    "Still queued" used to be answer enough on its own, and it is the answer that
    never ends: nothing takes a failing action out of the queue, so a move the
    server refuses every fifteen minutes claimed to be in flight for good. That
    is the state that hides a divergence rather than reporting one — the app went
    on showing a message as archived while the server had it in the inbox, and
    the sweep that exists to repair exactly that was told, forever, that it was
    too early to look. See STUCK_AFTER.
    """
    action = db.execute(
        select(PendingAction).where(
            PendingAction.message_pk == message_pk,
            PendingAction.type.in_(("move", "delete")),
        ).order_by(PendingAction.updated_at.desc())
    ).scalars().first()
    if action is None:
        return False                      # nothing was ever queued for it
    if action.status in SETTLED:
        # The agent has decided this one cannot be applied — a rebuilt folder
        # whose UIDs mean nothing now (_settle_stale), or a destination the
        # server will not take mail into (_settle_refused). Nothing is coming:
        # the message is wherever the server says it is, and a fresh action
        # queued against the re-read placement is how it moves.
        return False
    if action.status != "done":
        # Queued, or being applied right now — and believed, up to the point
        # where the refusals say it is not going to land.
        return action.attempts < STUCK_AFTER
    return utcnow() - action.updated_at < SETTLE_GRACE


def place_pending(
    db: Session, msg: Message, mailbox_id: int, flags_from: MessageLocation | None = None
) -> MessageLocation:
    """Put a message in a folder before the server knows about it.

    Flags come from the placement being moved, so a message archived after it
    was read stays read on the way there.

    Goes through ``msg.locations`` rather than a query, because the caller
    reaches this once per placement and a message under three Proton labels is
    archived out of all three into the same folder — three calls, one row. A
    SELECT would not see the row the first call has only added (autoflush is
    off), and the second would hit the unique constraint.
    """
    uid = pending_uid(msg.id)
    loc = next((item for item in msg.locations
                if item.mailbox_id == mailbox_id and item.imap_uid == uid), None)
    if loc is None:
        loc = MessageLocation(mailbox_id=mailbox_id, imap_uid=uid)
        msg.locations.append(loc)
    if flags_from is not None:
        loc.seen = flags_from.seen
        loc.flagged = flags_from.flagged
        loc.answered = flags_from.answered
        loc.draft = flags_from.draft
        loc.keywords = flags_from.keywords
    return loc


def drop_pending_placement(db: Session, message_pk: int, mailbox_id: int) -> None:
    """Retire the optimistic placement written while a move was queued.

    Called when the real placement lands (upsert_location, below) and when the
    move behind it is dropped instead of applied (agent/actions.py::_settle_stale)
    — the two ways a placement nothing backs stops being the truth.
    """
    loc = db.execute(
        select(MessageLocation).where(
            MessageLocation.mailbox_id == mailbox_id,
            MessageLocation.imap_uid == pending_uid(message_pk),
        )
    ).scalar_one_or_none()
    if loc is not None:
        db.delete(loc)


def upsert_location(
    db: Session, message_pk: int, mailbox_id: int, uid: int, flags: dict
) -> MessageLocation:
    loc = db.execute(
        select(MessageLocation).where(
            MessageLocation.mailbox_id == mailbox_id, MessageLocation.imap_uid == uid
        )
    ).scalar_one_or_none()
    if loc is None:
        # A placement showing up for mail the account already holds inherits the
        # read/flag state we have locally. Servers that file one message under
        # several labels hand us each placement separately, and the second one
        # can arrive after the message has been read — the reader can only mark
        # the placements that existed when it ran. Taking the server's flags
        # verbatim there resurrects mail as unread seconds after you read it.
        #
        # Escalate only: a flag the server has and we do not still wins on its
        # own, so this can never quietly un-read something.
        local = _local_state(db, message_pk)
        ahead = [name for name in _INHERITED if local.get(name) and not flags.get(name)]
        if ahead:
            flags = {**flags, **{name: True for name in ahead}}
        loc = MessageLocation(message_pk=message_pk, mailbox_id=mailbox_id, imap_uid=uid)
        db.add(loc)
        if ahead:
            _queue_flag_catchup(db, message_pk, mailbox_id, uid, ahead)
    loc.message_pk = message_pk
    _apply_flags(loc, flags)
    # The real placement has arrived, so the one we wrote while the move was
    # queued has done its job. Dropped after _local_state has been read above,
    # which is how a message archived and then read keeps its read state: the
    # optimistic row is where that state was living.
    if uid > 0:
        drop_pending_placement(db, message_pk, mailbox_id)
    return loc


# --- when two messages claim to be one ---------------------------------------
#
# A Message-ID is supposed to be unique per message, and mostly is, which is why
# it is what collapses the same mail seen under three Proton labels into one
# stored copy. But it is a header, written by whoever sent the message: a mailer
# with a broken generator, a list that re-sends under the old id, or anyone at
# all who wants two different messages to look like one can produce a collision.
#
# Believing it outright is how content goes missing. The second message is never
# fetched — its UID is simply hung off the first message's row — so the reader
# shows the first message's subject, body and attachments in the place where the
# second one arrived, and the second one's content was never stored at all.
#
# So the id proposes and the message disposes: a candidate found by Message-ID
# has to look like the same message before anything is attached to it. What that
# means is below, and deliberately uses only fields both sides derive the same
# way — the headers each was parsed from, plus a byte count when both numbers
# are ours rather than the server's.


def same_message(msg: Message, parsed: ParsedEmail) -> bool:
    """Is this stored row the message these bytes came from?

    The content decides it wherever the content is known: ``content_hash`` is a
    hash of every byte of the message, so two mails that hash the same *are* the
    same mail, and two that do not are not — whatever their headers claim. That
    is the only test that cannot be talked round by a sender, which matters
    because the question is only ever asked about messages claiming one
    Message-ID, and a sender is who decides what a Message-ID says.

    Not every row can answer. A message stored from its headers alone never had
    a body to hash, and rows that predate the column have no hash either; those
    fall back to sender, subject and send time — the three things a message
    carries wherever it goes, and enough to keep a genuine collision apart in
    every case anyone has actually met. A row like that gains its hash the moment
    the message is stored in full, which for the headers-only case is the very
    next pass that has the window for it.

    Byte *counts* are deliberately not part of this. The same message arrives
    with CRLF over IMAP and with LF out of an mbox, so it is a different length
    in each — counting would call every message in an imported archive different
    from the copy already synced and duplicate the lot. The hash is taken over
    the normalised form for the same reason. Where two sizes *are* commensurable
    they are used: the no-fetch shortcut compares the server's RFC822.SIZE with
    our own (see find_message_by_message_id).

    One transport difference survives that normalisation: an mbox writer escapes
    body lines beginning "From " to ">From ", and tools/import_mbox.py refuses to
    guess which of those were escaped and which the author typed. Such a message
    imported into an account that already synced it hashes differently and is
    stored twice. That is the direction to be wrong in — a duplicate is visible
    and deletable, and a merge is a body nobody can get back.
    """
    if msg.content_hash and parsed.content_key:
        return msg.content_hash == parsed.content_key
    return (msg.date_sent, msg.from_addr, msg.subject_norm) == (
        parsed.date_sent, parsed.from_addr, parsed.subject_norm)


def find_message_by_message_id(db: Session, account_id: int, message_id: str,
                               size: int | None = None, date=None,
                               headers: bytes | None = None,
                               content_wanted: bool = False) -> Message | None:
    """The stored message this id names, if it is safe to say it names one.

    Everything after the id comes free with the header pass the caller has
    already done, and every one of them is a reason to answer None — which sends
    the caller off to fetch the message properly instead of hanging its UID off a
    row that may be a different mail, or one that is missing the body this pass
    was supposed to bring.

    ``headers`` is the block those headers were read from, and is what makes this
    decision the same decision ``same_message`` makes on the full bytes: sender,
    subject and send time. Without it the shortcut was down to id, date and byte
    count — no sender, no subject, because the cheap pass did not fetch them —
    and two different messages agreeing on those three merged, with the second
    body never fetched at all.

    ``content_wanted`` says the caller is prepared to fetch a body for this UID:
    it is inside the content window. A stored row that has no body then is not an
    answer to it — that is the case where the window was widened and the mail it
    used to exclude is meant to come back — so the shortcut stands aside and lets
    the fetch happen. Without this the fill-in path in ingest_raw was unreachable
    from a sync pass: the recheck re-walked the folder, recognised every
    headers-only message by its id, and skipped the download that was the whole
    point of the recheck.

    A disagreement costs one fetch and nothing else: the full ingest that follows
    compares the two properly and still recognises a message it already holds, so
    a server whose RFC822.SIZE does not match the bytes it hands over makes this
    slower, never wrong.

    What this cannot do is compare the *message*, because not fetching it is the
    point — and that is why no sync pass calls it any more. Every fact available
    here is a header, headers are written by whoever sent the mail, and a pair
    engineered to agree on all of them was taken to be one message with the
    second body never fetched by anything. A pass fetches every UID now and lets
    the bytes decide in ingest_raw (see same_message); what it costs is
    bandwidth, and what it buys is that the mirror holds what the server holds.
    """
    message_id = canonical_message_id(message_id)
    if not message_id:
        return None
    rows = db.execute(
        select(Message).where(Message.account_id == account_id, Message.message_id == message_id)
        .limit(2)
    ).scalars().all()
    if len(rows) != 1:
        # None, or an id this account has already caught out: two rows wearing it
        # means it names nothing in particular, and this shortcut's whole job is
        # to decide without reading the message. Fetch it and let the content
        # decide (ingest_raw), which is the only place that can.
        return None
    msg = rows[0]
    if content_wanted and msg.content_status != "full":
        return None
    if date is not None and msg.date_sent is not None and msg.date_sent != date:
        return None
    if size is not None and msg.content_status != "skipped" and msg.size_bytes != size:
        return None
    identity = header_identity(headers)
    if identity is not None and (msg.from_addr, msg.subject_norm) != identity:
        return None
    return msg


def _store_content(db: Session, msg: Message, parsed: ParsedEmail, raw: bytes) -> None:
    """Fill in everything that comes from the body: text, attachments, search."""
    # What this row is, as opposed to what its headers say it is. Written here
    # because here is where the whole message is in hand, and kept when the
    # content is later pruned — the bytes go, but what they were stays knowable.
    msg.content_hash = parsed.content_key
    msg.size_bytes = parsed.size_bytes
    msg.snippet = parsed.snippet
    msg.has_attachments = bool(parsed.attachments)
    msg.body_text = parsed.body_text
    msg.body_html = parsed.body_html
    msg.search_text = build_search_text(parsed)
    msg.content_status = "full"
    # size_bytes, the body, the attachments and search_text are all derived
    # above, so the raw copy is purely for future features — and it is the
    # bulk of the database. settings.store_raw_mime (agent.store_raw_mime in
    # meerail.toml, or STORE_RAW_MIME) leaves the column NULL instead.
    msg.raw_mime = raw if get_settings().store_raw_mime else None

    needs_extract = any(
        should_extract(a.content_type, a.filename) and a.payload for a in parsed.attachments
    )
    msg.extract_status = "pending" if needs_extract else "none"

    for att in parsed.attachments:
        extractable = should_extract(att.content_type, att.filename) and bool(att.payload)
        # Inline parts are the signature logos and tracking pixels embedded in
        # the body; they are never listed as attachments, so a preview of one
        # would only ever be rendering nobody looks at.
        thumbable = (
            should_thumb(att.content_type) and bool(att.payload) and not att.is_inline
        )
        db.add(
            Attachment(
                message_pk=msg.id,
                filename=att.filename,
                content_type=att.content_type,
                size_bytes=len(att.payload),
                content_id=att.content_id,
                is_inline=att.is_inline,
                content=att.payload,
                extract_status="pending" if extractable else "skipped",
                thumb_status="pending" if thumbable else "skipped",
            )
        )


def replace_content(db: Session, msg: Message, raw: bytes) -> None:
    """Re-store a message's content from bytes fetched again from the server.

    The ordinary path never does this: content is written once and afterwards
    only ever pruned. It exists for the copy that was stored complete and was
    not — a server that answered while it was still assembling the message
    hands back a body with the attachments missing, and ``ingest_raw`` will not
    look at those bytes again, because as far as the row is concerned the
    content was fetched. See the agent's reconcile sweep, which is what notices.

    The headers are left alone: they are what identified, threaded and dated the
    message, they are the same in the new bytes, and rewriting them would move
    the message in the list for no reason. Attachment rows go first — the new
    parse re-creates them, and keeping the old ones would show the message
    carrying each of its files twice.
    """
    db.execute(delete(Attachment).where(Attachment.message_pk == msg.id))
    db.flush()      # autoflush is off; the DELETE must land before the INSERTs
    _store_content(db, msg, parse_email(raw), raw)


def ingest_raw(
    db: Session, account: Account, mailbox: Mailbox, uid: int, flags: dict, raw: bytes,
    headers_only: bool = False, size_bytes: int | None = None,
    parsed: ParsedEmail | None = None, received=None,
) -> tuple[Message, bool]:
    """Parse + store raw bytes. Returns (message, created_new_content).

    With ``headers_only``, ``raw`` is just the message's header block — what the
    agent fetches for mail that falls outside the content window. The row that
    lands carries every header (so it lists, threads and shows in search by
    subject and correspondent) with content_status='skipped' and no body,
    attachments or raw MIME. ``size_bytes`` then has to come from the server's
    RFC822.SIZE, since the headers are not the message's size.

    A later full fetch of a message stored that way fills the content in — that
    is what makes widening the window plus a full recheck a way to get old mail
    back, rather than a one-way door.

    ``parsed`` is the result of ``parse_email(raw)`` when the caller already has
    it. The sync path never does — it hands over bytes straight off the socket —
    but the mbox importer has to know a message's dedup_key *before* it decides
    whether to store it at all, and parsing a mailbox twice means decoding every
    attachment twice. It must be the parse of these exact bytes; nothing checks.
    """
    parsed = parsed if parsed is not None else parse_email(raw)
    dedup_key = parsed.dedup_key
    msg = db.execute(
        select(Message).where(
            Message.account_id == account.id, Message.dedup_key == dedup_key
        )
    ).scalar_one_or_none()

    shared_id = msg is not None and not same_message(msg, parsed)
    if shared_id:
        # Same Message-ID, different message. Whoever got here first keeps the
        # id as its key; this one is filed under its bytes instead, which cannot
        # belong to two messages. It keeps its own subject, body and attachments,
        # and Message.message_id is untouched, because what the sender wrote is
        # still what the sender wrote.
        #
        # What it does *not* keep is the id as a conversation. A thread_id is
        # what "archive this conversation" acts on, and two unrelated messages
        # sharing one means archiving either files both — the collision reaching
        # past itself into somebody's unrelated mail. See assign_thread.
        dedup_key = parsed.content_key
        msg = db.execute(
            select(Message).where(
                Message.account_id == account.id, Message.dedup_key == dedup_key
            )
        ).scalar_one_or_none()

    created = msg is None
    if created:
        msg = Message(
            account_id=account.id,
            message_id=parsed.message_id,
            dedup_key=dedup_key,
            thread_id=assign_thread(db, account.id, parsed, shared_id=shared_id),
            in_reply_to=parsed.in_reply_to,
            references=parsed.references,
            subject=parsed.subject,
            subject_norm=parsed.subject_norm,
            from_name=parsed.from_name,
            from_addr=parsed.from_addr,
            date_sent=parsed.date_sent,
            # When this arrived, as the *server* saw it (INTERNALDATE), falling
            # back to now for anything with no server behind it — an mbox
            # import, a test. Deliberately not the Date header, which is written
            # by the sender: date_sent is what the reader shows and sorts by,
            # and this is what decides how long the body is kept. A message
            # dated 1998 displays as 1998 and is retained as what it is, mail
            # that arrived today. See core.ingest.prune_expired_content.
            date_received=received or utcnow(),
            # Headers only: the body is not here to be measured, so take the
            # size the server reported. Everything else is header-derived and
            # therefore already correct.
            size_bytes=size_bytes if size_bytes is not None else parsed.size_bytes,
            search_text=build_search_text(parsed),
            content_status="skipped" if headers_only else "full",
        )
        db.add(msg)
        db.flush()  # assign msg.id

        for kind, pairs in parsed.recipients.items():
            for name, addr in pairs:
                db.add(Recipient(message_pk=msg.id, kind=kind, name=name, address=addr))

        if not headers_only:
            _store_content(db, msg, parsed, raw)
    elif not headers_only and msg.content_status != "full":
        # We have the whole thing now and did not before — the window was widened
        # and a recheck re-walked this UID. Recipients and the header fields are
        # already right; only content was ever missing.
        #
        # Both ways of being without it end here. "skipped" never had a body
        # (outside the window when it was first seen) and has no attachment rows;
        # "pruned" had one and was walked back as the window slid past it, and
        # its attachment rows are still there with their payloads emptied. Those
        # go first, or the message would come back carrying each of its files
        # twice — the same reason replace_content clears them.
        db.execute(delete(Attachment).where(Attachment.message_pk == msg.id))
        db.flush()      # autoflush is off; the DELETE must land before the INSERTs
        _store_content(db, msg, parsed, raw)

    upsert_location(db, msg.id, mailbox.id, uid, flags)
    return msg, created


def strip_content(db: Session, msg: Message) -> None:
    """Walk a stored message back to its headers, as the window slides past it.

    Attachment *rows* stay: the filename, type and size are header-scale data
    the reader still shows (as chips it will not offer to open), and it is the
    payloads, previews and extracted text that are worth the disk. Both queues
    go to 'skipped' so the indexer does not pick the emptied rows back up.
    """
    msg.body_text = ""
    msg.body_html = ""
    msg.snippet = ""
    msg.raw_mime = None
    msg.extract_status = "none"
    msg.content_status = "pruned"
    db.execute(
        update(Attachment)
        .where(Attachment.message_pk == msg.id)
        .values(content=None, thumb=None, extracted_text=None,
                extract_status="skipped", thumb_status="skipped")
    )
    # Before the rebuild, which re-reads the attachment text it just cleared.
    db.flush()
    rebuild_search_text(db, msg)


def ingest_location_only(
    db: Session, account: Account, mailbox: Mailbox, uid: int, flags: dict, message_id: str,
    size: int | None = None, date=None, headers: bytes | None = None,
    content_wanted: bool = False,
) -> bool:
    """Record a folder placement for content we already have. Returns True if matched.

    This is the shortcut that makes a label server's backfill finish: most of a
    Proton walk is the same mail seen again under another label, and matching it
    by Message-ID means the body never crosses the wire a second time.

    Everything after ``message_id`` is what makes that shortcut safe to take, and
    all of it comes out of the header pass the caller has already done: the size
    and date the server reported, the header block those came from, and whether
    this UID is one the caller would fetch a body for. See
    find_message_by_message_id for what each of them rules out. Passing none of
    them matches by id alone, which is only safe where the caller genuinely has
    nothing else to go on.
    """
    msg = find_message_by_message_id(db, account.id, message_id, size=size, date=date,
                                     headers=headers, content_wanted=content_wanted)
    if msg is None:
        return False
    upsert_location(db, msg.id, mailbox.id, uid, flags)
    return True


def recompute_counts(db: Session, mailbox: Mailbox) -> None:
    # autoflush is off, and this counts via raw SELECTs — flush pending flag/location
    # changes first so callers that just mutated locations get accurate counts.
    db.flush()
    total = db.scalar(
        select(func.count())
        .select_from(MessageLocation)
        .where(MessageLocation.mailbox_id == mailbox.id, MessageLocation.deleted.is_(False))
    )
    unread = db.scalar(
        select(func.count())
        .select_from(MessageLocation)
        .where(
            MessageLocation.mailbox_id == mailbox.id,
            MessageLocation.deleted.is_(False),
            MessageLocation.seen.is_(False),
        )
    )
    mailbox.total_count = int(total or 0)
    mailbox.unread_count = int(unread or 0)
