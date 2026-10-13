#!/usr/bin/env python3
"""Merge mail stored twice, and clear placements no server copy will replace.

Both are leftovers of versions up to 0.3.4, and both show in the reader, as mail
listed twice or as mail sitting in a folder the server no longer has it in.

Mail stored twice, as two rows for one message. There are two ways it happened.

  * Identical bytes. A message filed under its content hash, because another row
    held its Message-ID at the time, could not be found again once that other row
    was gone, and the next copy of it became a second row (see
    core/mail/store.py::ingest_raw). Trashing or archiving such a message left
    one row in the target folder for real and the other there as a placeholder.
  * One message that Proton Bridge serialized twice. Bridge writes an
    X-Pm-Gluon-Id header and random MIME boundaries, and when it rebuilds its
    cache a message can come back with new ones. The bytes differ, and so did the
    rows. These are merged only when the two are identical once that header is
    gone and the boundaries are renamed, so two different mails that happen to
    share a Message-ID are never merged.

  The row the Message-ID names is kept, every placement moves onto it, and the
  others are deleted. Their content is the kept row's content, so no mail is
  lost.

Placeholders. A move writes the target placement before the server has been told
(store.place_pending), under a negative UID, and the server's own copy retires it
when it arrives. These are the ones whose move finished long ago and whose copy
never came. There are three kinds.

  * Beside a real copy in the same folder. Archiving on Gmail did it, where every
    inbox message is in All Mail already. Dropped.
  * Of mail the server holds in other folders. Dropped, which leaves the message
    where the server says it is.
  * Of mail no folder holds. A move applied after the message had been deleted
    for good, or after another client had deleted it. Forgotten with --forget,
    which first asks the server whether it has the message after all, and leaves
    alone anything it cannot rule out. tools/restore_pending.py is the other way
    out of this state, and puts such mail back on the server.

Dry run by default. Nothing is written without --apply, and nothing that exists
only here is deleted without --forget as well, and then only after its bytes are
in an mbox under backups/ (or --backup-to). Every row is locked before it is
merged or deleted, so the agent can keep running, but a quiet moment is still
the better time.

  tools/repair_placements.py                       # what it would do
  tools/repair_placements.py --apply               # merge, and drop placeholders
  tools/repair_placements.py --apply --forget      # also forget mail no server holds
  tools/repair_placements.py --apply -a me@example.com
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# Shares `core` with the server and `imap` with the agent, one and two levels up
# respectively. Both are added here so the script runs from an activated venv
# without a PYTHONPATH of its own.
_REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(_REPO_ROOT), str(_REPO_ROOT / "agent")):
    if _path not in sys.path:
        sys.path.insert(0, _path)


# Queue states in which the agent may still act on a message. A row in one of
# them means a move that may yet land, and the rows it names have to be there
# when it does, so anything carrying one is left alone. "error" is the legacy
# state that --requeue-abandoned can put back into the queue.
_LIVE = ("pending", "leased", "held", "error")

# How long ago a move has to have finished before its placeholder is judged.
# store.SETTLE_GRACE is five minutes. A day is past every pass that could still
# bring the server's copy in.
DEFAULT_MIN_AGE_HOURS = 24

# Same-id candidates per folder worth downloading to answer one question. More
# than this is not a folder to read through, and "cannot tell" is an answer this
# tool knows what to do with (leave the message alone).
_CANDIDATE_LIMIT = 5

_GLUON = re.compile(rb"(?im)^X-Pm-Gluon-Id:[^\r\n]*\r?\n")
_BOUNDARY = re.compile(rb'(?i)boundary="?([^";\s]+)"?')


def bridge_neutral_key(raw: bytes) -> str:
    """A content key for these bytes with what Bridge makes up per build taken out.

    The X-Pm-Gluon-Id header goes, and every MIME boundary is renamed by the
    order in which it first appears. What is left is hashed exactly as
    content_key hashes a message. Two serializations of one message then agree,
    and two different messages still do not, because everything either of them
    says is left as it was.
    """
    from core.mail.parse import content_key

    raw = _GLUON.sub(b"", raw)
    for n, bnd in enumerate(sorted(set(_BOUNDARY.findall(raw)), key=raw.find)):
        raw = raw.replace(bnd, b"meerail-boundary-%d" % n)
    return content_key(raw)


# --- what there is to do ------------------------------------------------------


@dataclass
class Group:
    """Rows that are one message. ``keeper`` survives and takes the rest over."""
    keeper: int
    others: list[int]
    kind: str                   # "identical" or "bridge"
    subject: str
    date: datetime | None
    blocked: str | None = None  # why it is left alone this time, if it is


@dataclass
class Placeholder:
    loc_id: int
    message_pk: int
    mailbox_id: int
    folder: str
    kind: str                   # "beside", "elsewhere" or "nowhere"
    subject: str
    date: datetime | None
    message_id: str | None
    content_hash: str | None
    from_folder: str | None


@dataclass
class Totals:
    merged: int = 0             # rows deleted because another row is the same message
    blocked: int = 0
    dropped: int = 0            # placeholders taken out
    nowhere: int = 0            # placeholders of mail no folder holds
    forgotten: int = 0
    kept: int = 0               # ...of those, the ones the server could not rule out
    touched: set[int] = field(default_factory=set)

    def add(self, other: "Totals") -> None:
        for name in ("merged", "blocked", "dropped", "nowhere", "forgotten", "kept"):
            setattr(self, name, getattr(self, name) + getattr(other, name))


def _live(db, message_pks) -> set[int]:
    """Which of these messages the queue may still act on."""
    from sqlalchemy import select
    from core.models import PendingAction

    if not message_pks:
        return set()
    return set(db.execute(
        select(PendingAction.message_pk).where(
            PendingAction.message_pk.in_(list(message_pks)),
            PendingAction.status.in_(_LIVE))
    ).scalars().all())


def _busy(db, message_pks) -> dict[int, str]:
    """Which of these messages something is still waiting on, and what.

    A queued move or flag change may yet land, and the rows it names have to be
    there when it does. A pending reminder names its message by primary key,
    its conversation by thread_id, and every message it parked inside a JSON
    list (Reminder.parked). A merge that deleted one of those rows would bring
    the reminder back to nothing, so these are left for a later run.
    """
    from sqlalchemy import select
    from core.models import Message, Reminder

    pks = set(message_pks)
    if not pks:
        return {}
    busy = {pk: "a change to it is still queued" for pk in _live(db, pks)}
    threads: dict[str, set[int]] = {}
    for pk, thread_id in db.execute(
            select(Message.id, Message.thread_id).where(Message.id.in_(pks))).all():
        threads.setdefault(thread_id, set()).add(pk)
    for r in db.execute(select(Reminder.message_pk, Reminder.thread_id, Reminder.parked)
                        .where(Reminder.state == "pending")).all():
        named = {r.message_pk} | threads.get(r.thread_id, set())
        named |= {entry.get("message") for entry in (r.parked or [])
                  if isinstance(entry, dict)}
        for pk in named & pks:
            busy.setdefault(pk, "a reminder is waiting on it")
    return busy


def _keeper_first(rows) -> list:
    """The row that still holds its content first, then the row the Message-ID
    names, then the oldest.

    Content first, because the rows that go take their body, attachments and
    extracted text with them, and a kept row whose content was pruned would
    leave the message without any until a recheck. Keyed by the id after that,
    which is the ordinary state and the key the mail most likely has on every
    other install too (the journal names messages by it). The oldest last,
    because it is the one the reader has known longest.
    """
    return sorted(rows, key=lambda r: (r.content_status != "full",
                                       r.dedup_key.startswith("sha256:"), r.id))


def find_duplicates(db, account_id: int) -> list[Group]:
    """Every set of rows in this account that are one message."""
    from sqlalchemy import func, select
    from core.models import Message

    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.setdefault(x, x) != x:
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    # The same bytes, which is the same message by definition.
    same_bytes = db.execute(
        select(Message.content_hash).where(Message.account_id == account_id,
                                           Message.content_hash.is_not(None))
        .group_by(Message.content_hash).having(func.count() > 1)
    ).scalars().all()
    for content_hash in same_bytes:
        ids = db.execute(select(Message.id).where(Message.account_id == account_id,
                                                  Message.content_hash == content_hash)
                         ).scalars().all()
        for other in ids[1:]:
            union(ids[0], other)

    # Rows that agree on Message-ID and size, compared by the bytes they hold.
    # That finds two more kinds. A row stored before content_hash existed has
    # none, so the pass above cannot see it, and on 2026-10-13 four of the five
    # Bridge pairs on a real account were exactly that, byte for byte the same.
    # And one message Bridge wrote out twice, which differs only in what
    # bridge_neutral_key takes out (its boundaries are all one length, so the
    # sizes agree). Only rows that still hold their bytes are compared. Without
    # them there is nothing to go on, and a merge on headers alone is the
    # mistake core/mail/store.py exists to avoid.
    from core.mail.parse import content_key

    reserialized: set[int] = set()
    buckets = db.execute(
        select(Message.message_id, Message.size_bytes).where(
            Message.account_id == account_id, Message.message_id.is_not(None),
            Message.message_id != "", Message.content_status == "full",
            Message.raw_mime.is_not(None))
        .group_by(Message.message_id, Message.size_bytes)
        .having(func.count() > 1)
    ).all()
    for message_id, size in buckets:
        rows = db.execute(select(Message.id, Message.raw_mime).where(
            Message.account_id == account_id, Message.message_id == message_id,
            Message.size_bytes == size, Message.content_status == "full",
            Message.raw_mime.is_not(None))).all()
        by_key: dict[str, list[tuple[int, str]]] = {}
        for row in rows:
            raw = bytes(row.raw_mime)
            by_key.setdefault(bridge_neutral_key(raw), []).append((row.id, content_key(raw)))
        for same in by_key.values():
            for pk, _ in same[1:]:
                union(same[0][0], pk)
            if len({exact for _, exact in same}) > 1:
                reserialized.update(pk for pk, _ in same)

    members: dict[int, list[int]] = {}
    for x in list(parent):
        members.setdefault(find(x), []).append(x)
    sets = [ids for ids in members.values() if len(ids) > 1]
    if not sets:
        return []

    busy = _busy(db, {pk for ids in sets for pk in ids})
    groups = []
    for ids in sets:
        rows = _keeper_first(db.execute(
            select(Message.id, Message.dedup_key, Message.content_status, Message.subject,
                   Message.date_sent).where(Message.id.in_(ids))).all())
        groups.append(Group(
            keeper=rows[0].id, others=[r.id for r in rows[1:]],
            kind="bridge" if reserialized & set(ids) else "identical",
            subject=rows[0].subject, date=rows[0].date_sent,
            blocked=next((busy[pk] for pk in ids if pk in busy), None)))
    return sorted(groups, key=lambda g: g.keeper)


def find_placeholders(db, account_id: int, now: datetime, min_age: timedelta,
                      skip: set[int] = frozenset()) -> tuple[list[Placeholder], int]:
    """Placeholders whose move finished long enough ago to judge, and how many
    were passed over because something about them is still in motion.

    ``skip`` names messages a merge is about to take care of. Those only appear
    here in a dry run, where the merge has not happened yet.
    """
    from sqlalchemy import select
    from core.models import Mailbox, Message, MessageLocation, PendingAction

    rows = db.execute(
        select(MessageLocation.id, MessageLocation.message_pk, MessageLocation.mailbox_id,
               MessageLocation.updated_at, Mailbox.imap_name, Message.subject,
               Message.date_sent, Message.message_id, Message.content_hash)
        .join(Message, Message.id == MessageLocation.message_pk)
        .join(Mailbox, Mailbox.id == MessageLocation.mailbox_id)
        .where(Message.account_id == account_id, MessageLocation.imap_uid < 0)
        .order_by(MessageLocation.message_pk)
    ).all()
    rows = [r for r in rows if r.message_pk not in skip]
    live = _live(db, {r.message_pk for r in rows})
    found: list[Placeholder] = []
    waiting = 0
    for r in rows:
        if r.message_pk in live:
            waiting += 1
            continue
        last = db.execute(
            select(PendingAction.updated_at, PendingAction.payload).where(
                PendingAction.message_pk == r.message_pk,
                PendingAction.type.in_(("move", "delete")))
            .order_by(PendingAction.updated_at.desc()).limit(1)
        ).first()
        # The later of the two. A placeholder re-aimed locally today is today's,
        # whatever day the move behind it finished.
        since = max(last.updated_at, r.updated_at) if last is not None else r.updated_at
        if since > now - min_age:
            waiting += 1
            continue
        real = set(db.execute(
            select(MessageLocation.mailbox_id).where(
                MessageLocation.message_pk == r.message_pk, MessageLocation.imap_uid > 0,
                MessageLocation.deleted.is_(False))
        ).scalars().all())
        kind = "beside" if r.mailbox_id in real else "elsewhere" if real else "nowhere"
        payload = (last.payload or {}) if last is not None else {}
        found.append(Placeholder(
            loc_id=r.id, message_pk=r.message_pk, mailbox_id=r.mailbox_id,
            folder=r.imap_name, kind=kind, subject=r.subject, date=r.date_sent,
            message_id=r.message_id, content_hash=r.content_hash,
            from_folder=payload.get("from_folder") or payload.get("folder")))
    return found, waiting


# --- doing it -------------------------------------------------------------------


def _inherit(db, keeper_pk: int, onto, placeholder) -> None:
    """Carry what the user did to a placeholder over to the placement it is
    giving way to, the same way store.upsert_location does for a copy that
    arrives. Escalate only, so nothing becomes unread, and tell the server, or
    the next reconcile reads its flags back over this."""
    from core.mail import store

    ahead = [name for name in store._INHERITED
             if getattr(placeholder, name) and not getattr(onto, name)]
    for name in ahead:
        setattr(onto, name, True)
    if ahead and onto.imap_uid > 0:
        store._queue_flag_catchup(db, keeper_pk, onto.mailbox_id, onto.imap_uid, ahead)
    if placeholder.parked:
        onto.parked = True


def merge(db, group: Group) -> set[int] | None:
    """Fold every other row of the group into the keeper. Returns the folders
    touched, or None when something started waiting on the group since it was
    found. The caller commits, or rolls back on None.

    The rows are locked first. A placement the agent or the UI inserts for one
    of them then waits for this transaction, and fails cleanly on the row being
    gone, instead of being written in between and swept away by the cascade
    below. Whatever was queued meanwhile is read again under the lock.

    The other rows go by a plain DELETE once nothing points at them any more.
    Through the ORM it would cascade over whatever placements the session still
    had loaded for them, re-pointed or not.
    """
    from sqlalchemy import delete, func, select, update
    from core.mail import store
    from core.models import Message, MessageLocation, PendingAction, Reminder

    keeper = group.keeper
    db.execute(select(Message.id).where(Message.id.in_([keeper, *group.others]))
               .with_for_update()).all()
    busy = _busy(db, [keeper, *group.others])
    if busy:
        group.blocked = next(iter(busy.values()))
        return None
    touched: set[int] = set()
    # The keeper's placement in each folder, a real one over a placeholder.
    mine: dict[int, MessageLocation] = {}
    for loc in db.execute(select(MessageLocation).where(
            MessageLocation.message_pk == keeper)).scalars().all():
        if loc.mailbox_id not in mine or store.is_pending(mine[loc.mailbox_id]):
            mine[loc.mailbox_id] = loc
    for other in group.others:
        locs = db.execute(select(MessageLocation).where(
            MessageLocation.message_pk == other)).scalars().all()
        for loc in locs:
            touched.add(loc.mailbox_id)
            there = mine.get(loc.mailbox_id)
            if loc.imap_uid > 0:
                loc.message_pk = keeper
                if there is not None and store.is_pending(there):
                    # The keeper was the one waiting, and this is its copy.
                    _inherit(db, keeper, loc, there)
                    db.delete(there)
                    there = None
                if there is None:
                    mine[loc.mailbox_id] = loc
            elif there is not None:
                # The copy this placeholder was waiting for is the keeper's.
                _inherit(db, keeper, there, loc)
                db.delete(loc)
            else:
                # Still waiting for something, now on the keeper's behalf. The
                # placeholder pass below judges it like any other.
                loc.message_pk = keeper
                loc.imap_uid = store.pending_uid(keeper)
                mine[loc.mailbox_id] = loc
        db.flush()
        db.execute(update(Reminder).where(Reminder.message_pk == other)
                   .values(message_pk=keeper))
        db.execute(update(PendingAction).where(PendingAction.message_pk == other)
                   .values(message_pk=keeper))
        # First read wins (see Message.read_at). LEAST skips a NULL.
        earlier = select(Message.read_at).where(Message.id == other).scalar_subquery()
        db.execute(update(Message).where(Message.id == keeper)
                   .values(read_at=func.least(Message.read_at, earlier))
                   .execution_options(synchronize_session=False))
        db.execute(delete(Message).where(Message.id == other)
                   .execution_options(synchronize_session=False))
    return touched


def drop(db, ph: Placeholder) -> None:
    """Take out a placeholder the server's own placements already answer for."""
    from sqlalchemy import select
    from core.models import MessageLocation

    loc = db.get(MessageLocation, ph.loc_id)
    if loc is None:
        return
    if ph.kind == "beside":
        real = db.execute(select(MessageLocation).where(
            MessageLocation.message_pk == ph.message_pk,
            MessageLocation.mailbox_id == ph.mailbox_id, MessageLocation.imap_uid > 0,
            MessageLocation.deleted.is_(False)).limit(1)).scalars().first()
        if real is not None:
            _inherit(db, ph.message_pk, real, loc)
    db.delete(loc)


def _connect(acc):
    """A logged-in session with the account's server. Replaced in the tests."""
    from imap import Bridge

    bridge = Bridge(acc)
    bridge.connect()
    return bridge


def _held_elsewhere(db, account_id: int, ph: Placeholder) -> bool:
    """Does another row with this Message-ID sit in a real folder here?

    Then a same-id message the server holds is that row's copy, and says nothing
    about this one. Otherwise an unexplained message under this id is a reason
    not to forget anything.
    """
    from sqlalchemy import select
    from core.models import Message, MessageLocation

    return db.scalar(
        select(MessageLocation.id).join(Message, Message.id == MessageLocation.message_pk)
        .where(Message.account_id == account_id, Message.message_id == ph.message_id,
               Message.id != ph.message_pk, MessageLocation.imap_uid > 0).limit(1)
    ) is not None


def _server_has(ops, folder: str, ph: Placeholder, neutral: str | None,
                held_elsewhere: bool) -> bool | None:
    """Is this message in that folder on the server? None is "cannot tell"."""
    from core.mail.parse import content_key

    ops.select_folder(folder, readonly=True)
    uids = list(ops.search(["HEADER", "MESSAGE-ID", ph.message_id]))[:_CANDIDATE_LIMIT]
    if not uids:
        return False
    fetched = ops.fetch(uids, [b"BODY.PEEK[]"]) or {}
    for data in fetched.values():
        body = data.get(b"BODY[]")
        if not body:
            continue
        if ph.content_hash and content_key(body) == ph.content_hash:
            return True
        if neutral and bridge_neutral_key(body) == neutral:
            return True
    return False if held_elsewhere else None


class Backup:
    """An mbox that every forgotten message is written to before it is deleted.

    Forgetting deletes the only copy meerail holds, and the server has just said
    it holds none either. Most such mail was deleted for good on purpose, but
    not all of it. Versions up to 0.3.1 could destroy a message while moving it
    (see tools/restore_pending.py), and that mail looks exactly the same from
    here. So nothing is deleted before its bytes are on disk, in a file
    tools/import-mbox.sh can read back.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.box = None

    def keep(self, raw: bytes) -> None:
        import mailbox

        if self.box is None:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.box = mailbox.mbox(str(self.path))
            os.chmod(self.path, 0o600)          # it is mail
        self.box.add(raw)
        self.box.flush()

    def close(self) -> None:
        if self.box is not None:
            self.box.close()


def forget(db, account, config, nowhere: list[Placeholder], totals: Totals,
           backup: Backup) -> None:
    """Delete mail that exists only here, after the server has said it is not
    there either and its bytes are in the backup.

    One verdict per message, over every folder any of its placeholders claims,
    plus \\All where the account has one (on Proton and Gmail that is nearly
    everywhere), plus the folder the last move came out of. Any answer short of
    a definite "not there" in every one of them keeps the message: no
    Message-ID to search by, a folder that would not open, a same-id message
    that cannot be matched to anything, no bytes to keep a backup of.
    """
    from sqlalchemy import delete, select
    from core.models import Mailbox, Message, MessageLocation

    by_message: dict[int, list[Placeholder]] = {}
    for ph in nowhere:
        by_message.setdefault(ph.message_pk, []).append(ph)
    if config is None:
        print(f"  {len(by_message)} message(s) left alone: {account.email} is not in "
              f"the config, so there are no credentials to ask its server with")
        totals.kept += len(by_message)
        return
    folders = dict(db.execute(
        select(Mailbox.imap_name, Mailbox.role).where(
            Mailbox.account_id == account.id, Mailbox.local.is_(False))).all())
    everything = [name for name, role in folders.items() if role == "all"]

    bridge = _connect(config)
    try:
        ops = bridge.ops()
        for n, (pk, phs) in enumerate(by_message.items(), start=1):
            ph = phs[0]
            what = _describe(ph)
            if n % 50 == 0:
                print(f"  ...{n}/{len(by_message)} asked")
            busy = _busy(db, {pk})
            if busy:
                print(f"{what} -> {busy[pk]}, left alone")
                totals.kept += 1
                continue
            if not ph.message_id:
                print(f"{what} -> no Message-ID to ask the server with, left alone")
                totals.kept += 1
                continue
            raw = db.scalar(select(Message.raw_mime).where(Message.id == pk))
            if not raw:
                print(f"{what} -> no copy of its bytes to keep a backup of, left alone")
                totals.kept += 1
                continue
            raw = bytes(raw)
            neutral = bridge_neutral_key(raw)
            held = _held_elsewhere(db, account.id, ph)
            claimed = [x.folder for x in phs]
            asked = [f for f in dict.fromkeys([*claimed, *everything, ph.from_folder])
                     if f and f in folders]
            doubt = None if asked else "there is no folder on the server to ask"
            present = False
            for folder in asked:
                try:
                    verdict = _server_has(ops, folder, ph, neutral, held)
                except Exception as e:  # noqa: BLE001  an unanswered question keeps the mail
                    doubt = f"{folder} could not be searched ({e})"
                    break
                if verdict is True:
                    present = True
                    break
                if verdict is None:
                    doubt = (f"{folder} holds a message with this Message-ID that "
                             f"cannot be matched to it")
                    break
            if present:
                print(f"{what} -> the server still has it, left alone (a full recheck "
                      f"brings it back properly)")
                totals.kept += 1
                continue
            if doubt:
                print(f"{what} -> {doubt}, left alone")
                totals.kept += 1
                continue
            # Locked and checked again in the transaction that deletes it. The
            # agent may have placed the message, or something may have started
            # waiting on it, while the server was being asked, and a placement
            # it inserts now waits for this and then fails on the row being gone.
            db.execute(select(Message.id).where(Message.id == pk).with_for_update()).all()
            if _busy(db, {pk}) or db.scalar(
                    select(MessageLocation.id).where(
                        MessageLocation.message_pk == pk,
                        MessageLocation.imap_uid > 0).limit(1)) is not None:
                db.rollback()
                print(f"{what} -> changed while this ran, left alone")
                totals.kept += 1
                continue
            try:
                backup.keep(raw)
            except OSError as e:
                db.rollback()
                print(f"{what} -> the backup could not be written ({e}), left alone")
                totals.kept += 1
                continue
            db.execute(delete(Message).where(Message.id == pk))
            db.commit()
            totals.touched.update(x.mailbox_id for x in phs)
            totals.forgotten += 1
            print(f"{what} -> not on the server ({', '.join(asked)}), forgotten")
    finally:
        bridge.logout()


def _describe(ph: Placeholder) -> str:
    when = f"{ph.date:%Y-%m-%d}" if ph.date else "??????????"
    return f"    {when} {(ph.subject or '(no subject)')[:56]!r} in {ph.folder}"


def repair_account(db, account, *, apply: bool, forget_unheld: bool = False,
                   min_age: timedelta = timedelta(hours=DEFAULT_MIN_AGE_HOURS),
                   config=None, now: datetime | None = None,
                   backup: Backup | None = None) -> Totals:
    """Everything this tool does, for one account."""
    from core import events
    from core.mail import store
    from core.models import Mailbox, utcnow

    now = now or utcnow()
    totals = Totals()
    print(f"{account.email}")

    groups = find_duplicates(db, account.id)
    print(f"  {len(groups)} message(s) stored more than once")
    for g in groups:
        when = f"{g.date:%Y-%m-%d}" if g.date else "??????????"
        how = ("identical bytes" if g.kind == "identical"
               else "one message, two Bridge serializations")
        line = (f"    {when} {(g.subject or '(no subject)')[:56]!r}: rows "
                f"{', '.join(map(str, [g.keeper, *g.others]))}, {how}")
        if not g.blocked and apply:
            touched = merge(db, g)
            if touched is None:
                db.rollback()
            else:
                totals.touched |= touched
                db.commit()
        if g.blocked:
            print(f"{line} -> {g.blocked}, left alone")
            totals.blocked += 1
            continue
        print(f"{line} -> keep {g.keeper}")
        totals.merged += len(g.others)

    # A placeholder on any row of a group may be the very thing the merge
    # resolves, the keeper waiting for the copy another row holds. Those of a
    # group left alone wait for the run that merges it, and in a dry run, where
    # nothing has been merged yet, so do those of every group.
    skip = {pk for g in groups if g.blocked or not apply for pk in (g.keeper, *g.others)}
    placeholders, waiting = find_placeholders(db, account.id, now, min_age, skip)
    besides = [p for p in placeholders if p.kind != "nowhere"]
    nowhere = [p for p in placeholders if p.kind == "nowhere"]
    print(f"  {len(besides)} placeholder(s) the server's own placements already answer for")
    for p in besides:
        why = ("beside the real copy in the same folder" if p.kind == "beside"
               else "the server has it in other folders")
        print(f"{_describe(p)} -> {why}, {'dropped' if apply else 'would be dropped'}")
        if apply:
            drop(db, p)
            totals.touched.add(p.mailbox_id)
        totals.dropped += 1
    if apply:
        db.commit()

    totals.nowhere = len({p.message_pk for p in nowhere})
    print(f"  {totals.nowhere} message(s) no folder on the server holds")
    if apply and forget_unheld and nowhere:
        forget(db, account, config, nowhere, totals, backup or Backup(_default_backup()))
    else:
        for p in nowhere:
            print(f"{_describe(p)} -> exists only here")
    if waiting:
        print(f"  {waiting} placeholder(s) passed over: their move is still queued or "
              f"finished less than {min_age.total_seconds() / 3600:g} hours ago")

    if apply and totals.touched:
        for mailbox_id in totals.touched:
            mailbox = db.get(Mailbox, mailbox_id)
            if mailbox is not None:
                store.recompute_counts(db, mailbox)
        db.commit()
        events.publish({"type": "pruned", "messages": totals.merged + totals.forgotten})
    return totals


def _default_backup() -> Path:
    """Next to the other mail dumps, which .gitignore already keeps out of git."""
    return _REPO_ROOT / "backups" / f"forgotten-{datetime.now():%Y%m%d-%H%M%S}.mbox"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="repair-placements",
        description="Merge mail stored twice, and clear placements no server copy "
                    "will replace.",
        epilog="Dry run unless --apply is given. Re-running is safe: what is already "
               "repaired is not found again.",
    )
    parser.add_argument("-a", "--account", default=None,
                        help="repair one account (default: every account with a server)")
    parser.add_argument("--apply", action="store_true",
                        help="actually change the database. Without it nothing is written")
    parser.add_argument("--forget", action="store_true",
                        help="with --apply, also delete mail that exists only here, once "
                             "the server has confirmed it does not have it")
    parser.add_argument("--backup-to", default=None,
                        help="mbox every forgotten message is written to first (default: "
                             "backups/forgotten-<time>.mbox in the checkout)")
    parser.add_argument("--min-age-hours", type=int, default=DEFAULT_MIN_AGE_HOURS,
                        help="only judge placeholders whose move finished at least this "
                             f"long ago (default {DEFAULT_MIN_AGE_HOURS})")
    parser.add_argument("--config", default=None, help="path to meerail.toml")
    args = parser.parse_args(argv)
    if args.forget and not args.apply:
        parser.error("--forget deletes mail, so it only works together with --apply")

    # Must precede the first get_settings(), and so any core.* import that
    # reaches one, which is why the core imports here are inside functions.
    if args.config:
        os.environ["MEERAIL_CONFIG"] = args.config

    from sqlalchemy import select
    from core.config import get_settings
    from core.database import SessionLocal
    from core.models import Account

    cfg = get_settings()
    configs = {a.email.strip().lower(): a for a in cfg.accounts}
    backup = Backup(Path(args.backup_to) if args.backup_to else _default_backup())
    db = SessionLocal()
    try:
        # Imported accounts are left out. They write no placeholders, and two
        # copies of one message there may be two imports someone made on purpose.
        q = select(Account).where(Account.local.is_(False)).order_by(Account.id)
        if args.account:
            q = q.where(Account.email == args.account.strip().lower())
        accounts = db.execute(q).scalars().all()
        if not accounts:
            print(f"No synced account {args.account or ''} in the database", file=sys.stderr)
            return 1
        totals = Totals()
        for account in accounts:
            totals.add(repair_account(
                db, account, apply=args.apply, forget_unheld=args.forget,
                min_age=timedelta(hours=args.min_age_hours),
                config=configs.get(account.email), backup=backup))
            print()
    finally:
        db.close()
        backup.close()

    if args.apply:
        print(f"{totals.merged} duplicate row(s) merged, {totals.dropped} placeholder(s) "
              f"dropped, {totals.forgotten} message(s) forgotten.")
        if totals.forgotten:
            print(f"Every forgotten message is in {backup.path}, and "
                  f"tools/import-mbox.sh can bring any of them back.")
        if totals.nowhere - totals.forgotten > 0:
            left = totals.nowhere - totals.forgotten
            hint = ("" if args.forget else " --forget deletes them once the server has "
                    "confirmed it does not have them, and tools/restore_pending.py puts "
                    "them back on the server instead.")
            print(f"{left} message(s) that exist only here were left alone.{hint}")
    else:
        print(f"{totals.merged} duplicate row(s) would be merged and {totals.dropped} "
              f"placeholder(s) dropped. {totals.nowhere} message(s) exist only here, and "
              f"--apply --forget would ask the server about them before deleting any. "
              f"Re-run with --apply.")
    if totals.blocked:
        print(f"{totals.blocked} duplicate(s) were left alone because something is still "
              f"waiting on them, a queued change or a reminder. Run this again once that "
              f"is done.")
    return 0


def _in_agent_venv() -> None:
    """Carry on under the agent's venv when started without meerail's libraries.

    Run as tools/repair_placements.py, the shebang picks whatever python3 comes
    first on PATH, and on a desktop that is the system one, without pydantic,
    SQLAlchemy or psycopg. The agent's venv has all of them (agent/run.sh and
    tools/import-mbox.sh build it), so the script starts itself again there.
    The venv's python is a symlink to the system one, so which interpreter is
    running is told by sys.prefix, not by the binary.
    """
    try:
        import pydantic_settings  # noqa: F401
        import sqlalchemy  # noqa: F401
        return
    except ImportError:
        pass
    venv = _REPO_ROOT / "agent" / ".venv"
    if (venv / "bin" / "python").exists() and Path(sys.prefix).resolve() != venv.resolve():
        os.execv(str(venv / "bin" / "python"),
                 [str(venv / "bin" / "python"), str(Path(__file__).resolve()), *sys.argv[1:]])
    raise SystemExit("meerail's Python libraries are not installed for this interpreter. "
                     "Run agent/run.sh or tools/import-mbox.sh once to build agent/.venv, "
                     "or start this with a Python that has them.")


if __name__ == "__main__":
    _in_agent_venv()
    raise SystemExit(main())
