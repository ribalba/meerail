"""Keeping several installs of meerail in agreement about what this app decided.

Mail syncs itself: three machines against the same accounts see the same folders,
the same read flags, the same conversations, because that is what IMAP is for.
What does not sync is everything meerail invented — a conversation parked on a
reminder until Monday, the footer a compose window prefills, the name and colour
an account wears in the sidebar. None of those are facts about mail, so there is
nowhere on a mail server to keep them, and three installs each keep their own
answer and disagree.

The fix is a log they all read: core/journal.py seals records, journal/server.py
orders them, and this module is what turns records into rows and rows into
records. Three things are worth reading before the code.

**Nothing here moves mail.** A reminder set on the laptop archives the
conversation there, and that move goes to the mail server the ordinary way — so
by the time the desktop reads the record, the mail is already out of its inbox
too. Applying a record only writes down the *promise*: when it comes back, and
where it goes when it does. An apply path that also moved mail would move it
twice, once per machine that read the record.

**The log is replayed, not merged.** Records are applied strictly in the order
the server numbered them, and the last one about a given thing wins. That is why
there is no merge rule per field and no per-record conflict handling: two
machines that set a reminder on the same conversation produce two records, the
later one is applied second, and all three machines end up on the later deadline
because all three replay the same sequence. Applying is therefore idempotent, and
a full replay from the start of the log lands in exactly the state an incremental
one does — which is what makes the retention window in journal/server.py safe.

**Being behind is normal, and is not an error.** A record can name a message this
install has not synced yet; the laptop that wrote it was simply ahead. Those are
parked in ``journal_deferred`` and retried, rather than dropped (which loses the
reminder) or allowed to stall the cursor (which loses everything after it).

Adding a new kind of synced state is a handler and a publisher — see ACCOUNT_PREFS
below, which is the whole of "keep footers and account names in step" and is
about forty lines. Nothing in journal/server.py changes, and nothing in the wire
format changes, which is the point of having built it this way.
"""

from __future__ import annotations

import socket
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from core import journal as wire
from core.config import get_settings
from core.models import (
    Account, JournalDeferred, JournalOutbox, Mailbox, Message, Reminder, Setting, utcnow,
)

settings = get_settings()

# Where this install has read up to in the log. A plain settings row: it is one
# integer, written once a pass, and nothing joins against it.
CURSOR_KEY = "journal.cursor"

# How long a claim on a due reminder is honoured before the others may take it.
# Long enough that a machine mid-fire is not overtaken (firing is a few database
# writes and a queued move — milliseconds, unless the database is in trouble),
# short enough that a laptop shut between claiming and firing does not park the
# conversation until somebody notices. See claim().
CLAIM_TTL = timedelta(minutes=10)

# How many passes a record that cannot be applied is retried before it is given
# up on. At one pass a minute this is a bit over a day, which is far longer than
# a backfill needs to reach a single message and short enough that a record
# naming mail that will never arrive does not get retried forever.
MAX_TRIES = 2000

# Records per request, and how many deferred ones to retry per pass. Both are
# ceilings for the catch-up case; a settled install moves a handful a day.
PAGE = 500
DEFER_BATCH = 200

_keys: wire.Keys | None = None


def enabled() -> bool:
    return bool(settings.journal_url and settings.journal_passphrase)


def keys() -> wire.Keys:
    """The derived keys, worked out once.

    Cached because the derivation is a deliberately expensive scrypt pass and the
    sync loop would otherwise pay it every minute forever.
    """
    global _keys
    if _keys is None:
        _keys = wire.derive(settings.journal_passphrase)
    return _keys


def instance_name() -> str:
    """What this machine calls itself in the records it writes.

    Only ever displayed. Two machines that pick the same name make the log
    harder to read and nothing harder to compute — order comes from the server's
    sequence number, never from this.
    """
    return (settings.journal_instance or socket.gethostname() or "meerail")[:64]


# --- Transport -------------------------------------------------------------


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=settings.journal_url.rstrip("/"),
        headers={"Authorization": f"Bearer {keys().token}"},
        timeout=30.0,
    )


def _post(records: list[tuple[dict, bool]]) -> list[int]:
    """Seal and append. Returns the sequence numbers, in the order sent."""
    payload = {"records": [{"blob": wire.seal(keys(), rec), "snapshot": snap}
                           for rec, snap in records]}
    with _client() as http:
        resp = http.post("/journal", json=payload)
        resp.raise_for_status()
        return resp.json()["seqs"]


def _fetch(since: int) -> dict:
    with _client() as http:
        resp = http.get("/journal", params={"since": since, "limit": PAGE})
        resp.raise_for_status()
        return resp.json()


# --- Cursor ----------------------------------------------------------------


def cursor(db: DBSession) -> int:
    row = db.get(Setting, CURSOR_KEY)
    try:
        return int(row.value) if row and row.value else 0
    except ValueError:
        return 0


def set_cursor(db: DBSession, seq: int) -> None:
    row = db.get(Setting, CURSOR_KEY)
    if row is None:
        db.add(Setting(key=CURSOR_KEY, value=str(seq)))
    else:
        row.value = str(seq)


# --- Writing ---------------------------------------------------------------


def publish(db: DBSession, kind: str, body: dict, *, account: str | None = None,
            key: str | None = None, snapshot: bool = False) -> None:
    """Queue a record for the log. Does not talk to the network.

    Called from the routes that change something worth sharing, and deliberately
    not from the functions underneath them: those same functions are what the
    apply path uses, and a publish in there would echo every record this install
    read straight back to the server.

    Nothing here fails a request. The row is written in the caller's transaction,
    so a record is published exactly when the change it describes is committed —
    and if the journal server is unreachable for a week, the reminder still works
    locally and the record goes up when it comes back.
    """
    if not enabled():
        return
    db.add(JournalOutbox(
        kind=kind, snapshot=snapshot,
        body=wire.envelope(kind, body, instance=instance_name(), account=account, key=key),
    ))


# How long a record that has been taken is kept locally. Purely a record of
# what this install published and when; nothing reads it back, so this only has
# to be long enough to answer "did my footer change actually go up" after a
# weekend of wondering.
KEEP_SENT = timedelta(days=7)


def _sweep(db: DBSession) -> None:
    """Drop the records that have been taken and are no longer interesting.

    Every reminder set, cancelled and fired writes a row here, so without this
    the table is a permanent history of a feature whose whole state is three
    columns on `reminders`.
    """
    cutoff = utcnow() - KEEP_SENT
    for row in db.execute(
        select(JournalOutbox).where(JournalOutbox.status == "sent",
                                    JournalOutbox.sent_at < cutoff)
    ).scalars().all():
        db.delete(row)


def drain(db: DBSession) -> int:
    """Send everything queued. Returns how many went.

    One request for the batch, then the sequence numbers are written back onto
    the rows — which matters only for claims, but costs nothing for the rest.
    """
    _sweep(db)
    rows = db.execute(
        select(JournalOutbox).where(JournalOutbox.status == "pending")
        .order_by(JournalOutbox.id).limit(PAGE)
    ).scalars().all()
    if not rows:
        db.commit()               # the sweep, if it found anything
        return 0
    try:
        seqs = _post([(row.body, row.snapshot) for row in rows])
    except Exception as exc:  # noqa: BLE001
        for row in rows:
            row.attempts += 1
            row.error = str(exc)[:500]
        db.commit()
        raise
    now = utcnow()
    for row, seq in zip(rows, seqs):
        row.status, row.seq, row.sent_at, row.error = "sent", seq, now, None
    db.commit()
    return len(rows)


# --- Reading ---------------------------------------------------------------


class Defer(Exception):
    """This record is fine; this install is not ready for it yet."""


def pull(db: DBSession) -> int:
    """Read the log forward and apply what is new. Returns records applied.

    Deferred records are retried first, so a message that has just finished
    syncing brings its reminder with it on the same pass rather than the next.
    """
    applied = _retry_deferred(db)
    while True:
        since = cursor(db)
        page = _fetch(since)
        if page.get("reset"):
            # This install was off for longer than the server's retention
            # window, so the records it is asking for are gone and the page it
            # would get starts mid-history. Replay the whole log instead: it is
            # ordered and idempotent, so this lands in the same state, and the
            # oldest thing still held is a snapshot by construction (nothing is
            # pruned that a snapshot has not superseded).
            print("journal: fell behind the retention window, replaying from the "
                  "oldest record held", flush=True)
            set_cursor(db, 0)
            db.commit()
            continue
        records = page.get("records") or []
        if not records:
            return applied
        for item in records:
            seq, blob = item["seq"], item["blob"]
            record = wire.unseal(keys(), blob)
            if record is None:
                # Not ours, or from a newer meerail. Neither is an error and
                # neither is retryable — step over it.
                continue
            try:
                _apply(db, record, seq)
                applied += 1
            except Defer as exc:
                _defer(db, seq, record, str(exc))
            except Exception as exc:  # noqa: BLE001
                # A record this install genuinely cannot use. Say so and move
                # on: stopping here would wedge the cursor behind one bad row.
                print(f"journal: record {seq} ({record.get('kind')}) failed: {exc!r}",
                      flush=True)
        set_cursor(db, records[-1]["seq"])
        db.commit()
        if len(records) < PAGE:
            return applied


def _defer(db: DBSession, seq: int, record: dict, reason: str) -> None:
    if db.get(JournalDeferred, seq) is None:
        db.add(JournalDeferred(seq=seq, record=record, reason=reason[:500]))


def _retry_deferred(db: DBSession) -> int:
    """Have another go at the records that were waiting on this install.

    In sequence order, because that is the order they have to be applied in for
    a later record to win over an earlier one about the same thing.

    ``blocked`` is what keeps that true when only some of them come unstuck. If
    the record that *sets* a reminder is still waiting on its message, the record
    that cancels it must keep waiting too — applied on its own it would find
    nothing to cancel and do nothing, and the set would then land afterwards and
    resurrect a reminder the user had taken back.
    """
    rows = db.execute(
        select(JournalDeferred).order_by(JournalDeferred.seq).limit(DEFER_BATCH)
    ).scalars().all()
    applied = 0
    blocked: set[tuple] = set()
    for row in rows:
        subject = (row.record.get("kind"), row.record.get("account"), row.record.get("key"))
        if subject in blocked:
            row.tries += 1
            continue
        try:
            _apply(db, row.record, row.seq)
        except Defer as exc:
            blocked.add(subject)
            row.tries += 1
            row.reason = str(exc)[:500]
            if row.tries >= MAX_TRIES:
                print(f"journal: giving up on record {row.seq} after {row.tries} "
                      f"tries ({row.reason})", flush=True)
                db.delete(row)
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"journal: deferred record {row.seq} failed: {exc!r}", flush=True)
            db.delete(row)
            continue
        db.delete(row)
        applied += 1
    if rows:
        db.commit()
    return applied


# --- The kinds -------------------------------------------------------------

REMINDER = "reminder"
ACCOUNT_PREFS = "account_prefs"


def _apply(db: DBSession, record: dict, seq: int) -> None:
    """Apply one record. ``seq`` is the server's number for it, and is the order.

    Handlers take the number as well as the record because for a claim it *is*
    the content: "I want this one" is worth nothing without "and I asked at
    position 4021", and the position is assigned by the server after the record
    was written, so it cannot be inside the sealed body.
    """
    handler = _HANDLERS.get(record.get("kind"))
    if handler is None:
        return                      # a kind this build does not know
    handler(db, record, seq)
    # Flushed before the next record is applied, and this is load-bearing:
    # sessions here are autoflush=False (core/database.py), so a Reminder added
    # by a "set" is invisible to the SELECT that the "cancel" behind it uses to
    # find it. The two would then both appear to succeed and leave the reminder
    # standing — a promise the user had already taken back.
    db.flush()


def _account(db: DBSession, record: dict) -> Account:
    email = (record.get("account") or "").strip().lower()
    account = db.execute(
        select(Account).where(Account.email == email)).scalars().first()
    if account is None:
        # Not configured here. A machine that syncs two of the three accounts is
        # a perfectly ordinary setup, so this waits rather than failing: the
        # account may be added later, and until then the record is simply not
        # for this install.
        raise Defer(f"no account {email!r} on this install")
    return account


def _message(db: DBSession, account: Account, dedup_key: str) -> Message:
    msg = db.execute(
        select(Message).where(Message.account_id == account.id,
                              Message.dedup_key == dedup_key)
    ).scalars().first()
    if msg is None:
        raise Defer(f"message {dedup_key!r} has not synced here yet")
    return msg


def _mailbox_by_name(db: DBSession, account: Account, name: str) -> Mailbox | None:
    return db.execute(
        select(Mailbox).where(Mailbox.account_id == account.id, Mailbox.imap_name == name)
    ).scalars().first()


# --- Reminders -------------------------------------------------------------
#
# Four operations, and the shape of each is in the publisher below it:
#
#   set     a conversation is parked, and comes back at this instant
#   cancel  the promise is off (the mail stays wherever it is)
#   claim   this install intends to be the one that brings it back
#   fired   it has been brought back; nobody else should
#
# The key throughout is the anchor message's ``dedup_key`` — the Message-ID where
# there is one, and a hash of the bytes where there is not (core/mail/parse.py).
# Not the local message id, which differs per install, and not ``thread_id``,
# which is derived from whichever message that install happened to see first and
# so can legitimately differ between two machines that backfilled in a different
# order.


def reminder_key(msg: Message) -> str:
    return msg.dedup_key


def _apply_reminder(db: DBSession, record: dict, seq: int) -> None:
    body = record.get("body") or {}
    op = body.get("op")
    account = _account(db, record)
    anchor = _message(db, account, record.get("key") or "")
    existing = _pending_for_key(db, account.id, anchor)

    if op == "set":
        due_at = datetime.fromisoformat(body["due_at"])
        if existing is not None:
            # Only ever the deadline. Re-parking is what the local path refuses
            # too, and for the same reason: the mail is already filed, and the
            # record of where it came from is the thing that must not be
            # overwritten with where it is now. See app/reminders.py.
            existing.due_at = due_at
            existing.error = None
            return
        park = _mailbox_by_name(db, account, body.get("park") or "")
        if park is None:
            # The folder the mail is waiting in, which this install has not
            # listed yet — a first sync that has not reached it, or an agent
            # that has not run here since it was created. Waiting is the only
            # honest answer: a reminder recorded without it would look healthy
            # in the list and then quietly move nothing when it came due,
            # because fire() has no folder to take the mail out of.
            raise Defer(f"folder {body.get('park')!r} is not known here yet")
        parked = []
        for entry in body.get("parked") or []:
            member = db.execute(
                select(Message).where(Message.account_id == account.id,
                                      Message.dedup_key == entry.get("message"))
            ).scalars().first()
            if member is None:
                # One message of a conversation that has not arrived here. The
                # rest of the thread is still worth parking — the missing one is
                # in the same folder as its siblings and will come back with
                # them or not at all, and waiting for the whole thread would
                # defer the reminder over a message that may never sync.
                continue
            origins = [mb.id for name in entry.get("from") or []
                       if (mb := _mailbox_by_name(db, account, name)) is not None]
            parked.append({"message": member.id, "from": origins})
        db.add(Reminder(
            account_id=account.id, message_pk=anchor.id, thread_id=anchor.thread_id,
            due_at=due_at, park_mailbox_id=park.id if park else None, parked=parked,
        ))

    elif op == "cancel" and existing is not None:
        existing.state = "cancelled"
        existing.error = None

    elif op == "fired" and existing is not None:
        # Somebody else brought it back. The mail is already moving — the move
        # was queued on the install that fired, and reaches this one through the
        # mail server like any other — so this only retires the promise.
        existing.state = "done"
        existing.fired_at = utcnow()
        existing.error = None

    elif op == "claim" and existing is not None:
        # The claim's weight is its position in the log, which the server
        # assigned after this record was sealed — so it comes from outside the
        # body. Recorded locally as "who currently owns firing this one", and
        # only ever replaced by an earlier claim (a better one) or an expired
        # one (a machine that claimed and then went away).
        held = (existing.claim_seq is not None and existing.claim_at is not None
                and utcnow() - existing.claim_at < CLAIM_TTL)
        if not held or seq < existing.claim_seq:
            existing.claim_seq = seq
            existing.claim_by = (record.get("instance") or "")[:64]
            existing.claim_at = utcnow()


def _pending_for_key(db: DBSession, account_id: int, anchor: Message) -> Reminder | None:
    """The pending reminder this record is about, found the way the app finds it.

    Deliberately the same rule as app/reminders.py::pending_for — by conversation
    where there is one — so that a record written against a reply and a keypress
    against the root reach the same row.
    """
    q = select(Reminder).where(Reminder.account_id == account_id,
                               Reminder.state == "pending")
    if anchor.thread_id:
        q = q.where(Reminder.thread_id == anchor.thread_id)
    else:
        q = q.where(Reminder.message_pk == anchor.id)
    return db.execute(q.order_by(Reminder.id)).scalars().first()


def publish_reminder_set(db: DBSession, reminder: Reminder, anchor: Message) -> None:
    """Tell the others a conversation is parked, and where its parts came from.

    Folders travel as IMAP names rather than ids for the obvious reason — the
    ids are this database's — and messages as ``dedup_key`` for the same one.
    """
    account = db.get(Account, reminder.account_id)
    park = db.get(Mailbox, reminder.park_mailbox_id) if reminder.park_mailbox_id else None
    parked = []
    for entry in reminder.parked or []:
        member = db.get(Message, entry.get("message"))
        if member is None:
            continue
        names = [mb.imap_name for mid in entry.get("from") or []
                 if (mb := db.get(Mailbox, mid)) is not None]
        parked.append({"message": member.dedup_key, "from": names})
    publish(db, REMINDER, {
        "op": "set",
        "due_at": reminder.due_at.isoformat(),
        "park": park.imap_name if park else None,
        "parked": parked,
    }, account=account.email if account else None, key=reminder_key(anchor))


def publish_reminder_op(db: DBSession, reminder: Reminder, op: str) -> None:
    """The one-word records: cancel and fired."""
    account = db.get(Account, reminder.account_id)
    anchor = db.get(Message, reminder.message_pk)
    if account is None or anchor is None:
        return
    publish(db, REMINDER, {"op": op},
            account=account.email, key=reminder_key(anchor))


def claim(db: DBSession, reminder: Reminder) -> bool:
    """Whether this install is the one that brings this conversation back.

    All three machines hold the reminder and all three watch the clock, so
    without this the nine o'clock deadline moves the same conversation three
    times. The rule is: everybody appends a claim, and the lowest sequence number
    wins — an ordering all three trust precisely because none of them produced
    it.

    Two round trips, once per reminder: append, then read the log back to see
    whose claim landed first. That is affordable because it happens once per
    reminder rather than once per tick.

    **A journal that cannot be reached fires anyway.** Returning False there
    would mean a mail server outage, or a rented box someone forgot to pay for,
    silently converting every reminder into a conversation that never comes back
    — a failure that looks exactly like the feature not working, discovered
    weeks later. Firing twice is a bounded, visible cost by comparison, and
    mostly not even that: fire() skips any message no longer in the folder it was
    parked in, so the second install to arrive finds the work done and moves
    nothing.
    """
    if not enabled():
        return True

    held = (reminder.claim_seq is not None and reminder.claim_at is not None
            and utcnow() - reminder.claim_at < CLAIM_TTL)
    if held and reminder.claim_by != instance_name():
        return False                      # somebody else is already on it

    account = db.get(Account, reminder.account_id)
    anchor = db.get(Message, reminder.message_pk)
    if account is None or anchor is None:
        return True                       # nothing to coordinate about
    now = utcnow()
    try:
        # Posted directly rather than through the outbox: the sequence number is
        # the answer, so there is no version of this that survives being queued.
        seq = _post([(wire.envelope(
            REMINDER, {"op": "claim"}, instance=instance_name(),
            account=account.email, key=reminder_key(anchor)), False)])[0]
    except Exception as exc:  # noqa: BLE001
        print(f"journal: could not claim reminder {reminder.id}, firing anyway: {exc!r}",
              flush=True)
        return True

    reminder.claim_seq, reminder.claim_by, reminder.claim_at = seq, instance_name(), now
    db.commit()

    # Read the log forward, which applies any competing claim onto this same row.
    try:
        pull(db)
    except Exception as exc:  # noqa: BLE001
        print(f"journal: could not read back claims, firing anyway: {exc!r}", flush=True)
        return True

    db.refresh(reminder)
    return reminder.claim_seq is None or reminder.claim_seq >= seq


# --- Account presentation --------------------------------------------------
#
# The second kind, and the reason the first one was built generically: a footer
# and an account's name in the sidebar are exactly the sort of thing that is
# obviously shared and has nowhere to live. This is the whole implementation.


def _apply_account_prefs(db: DBSession, record: dict, seq: int) -> None:
    account = _account(db, record)
    body = record.get("body") or {}
    pinned = set(account.config_fields or [])
    for field in ("label", "color", "footer"):
        if field not in body:
            continue
        if field in pinned:
            # This install's meerail.toml owns the field, and the agent rewrites
            # it on every pass — applying the record would start a fight that
            # the file wins a minute later. The file is the more specific
            # instruction, and it is local on purpose.
            continue
        setattr(account, field, body[field])
        if field == "footer":
            account.footer_customized = True


def publish_account_prefs(db: DBSession, account: Account, fields: list[str]) -> None:
    body = {f: getattr(account, f) for f in fields if f in ("label", "color", "footer")}
    if body:
        publish(db, ACCOUNT_PREFS, body, account=account.email, key=account.email)


_HANDLERS = {
    REMINDER: _apply_reminder,
    ACCOUNT_PREFS: _apply_account_prefs,
}


# --- Snapshots -------------------------------------------------------------


def snapshot(db: DBSession) -> int:
    """Restate the whole of this install's shareable state as fresh records.

    Two jobs, and the second is the one that matters. The obvious one is that a
    machine joining an existing journal gets the current picture without reading
    a year of history. The other is that the server cannot delete anything until
    something says "everything before this is covered" — it holds ciphertext, so
    it cannot work that out for itself (journal/server.py::_prune). Without a
    snapshot the log grows forever.

    Sent as ordinary records, marked snapshot, so applying one is applying a
    hundred sets — no special case in the reader.
    """
    published = 0
    for account in db.execute(select(Account)).scalars().all():
        rows = db.execute(
            select(Reminder).where(Reminder.account_id == account.id,
                                   Reminder.state == "pending")
        ).scalars().all()
        for reminder in rows:
            anchor = db.get(Message, reminder.message_pk)
            if anchor is None:
                continue
            publish_reminder_set(db, reminder, anchor)
            published += 1
        publish_account_prefs(db, account, ["label", "color", "footer"])
        published += 1

    # Mark the last of the batch as the snapshot point: everything queued above
    # together restates the state, so that record is the one the server may
    # prune behind. Nothing queued means nothing to mark — and in particular no
    # marking of some unrelated record that happened to be waiting.
    if published:
        last = db.execute(
            select(JournalOutbox).where(JournalOutbox.status == "pending")
            .order_by(JournalOutbox.id.desc()).limit(1)
        ).scalars().first()
        if last is not None:
            last.snapshot = True
    return published


def sync_once(db: DBSession) -> tuple[int, int]:
    """One pass: publish what we owe, then read what we are owed."""
    sent = drain(db)
    got = pull(db)
    return sent, got
