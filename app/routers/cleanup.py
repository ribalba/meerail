"""Cleanup: find mail that is many copies of the same thing, and file it.

A mailbox that has been running for fifteen years is mostly not correspondence.
It is build failures, backup reports, "somebody logged in", property alerts and
job digests — mail that was worth a glance once and has been worth nothing
since. There is no useful way to go through it one message at a time, and
searching for it means already knowing what to search for.

So this groups it. Two ways, because bulk mail comes in two shapes:

  * **subject** — sender plus the subject with its numbers masked out.
    "Verlängerungsbericht vom 3.4.2016" and "…vom 7.9.2017" are one template,
    and this is the cheap, exact, entirely explainable way to see that. It is
    one GROUP BY and it catches most of it.
  * **body** — sender plus a near-duplicate body, via the banded MinHash in
    core/bodysig.py. This is for the senders that write a fresh subject every
    time ("3 neue Angebote: Reihenhaus in …") over an unchanging template. The
    subject grouping cannot see those at all.

Everything here is about *not* deleting the wrong thing, so the filter is worth
stating plainly. A message is a candidate only if it is:

  * not in Sent, Drafts, Junk, Trash, or a \\Flagged pseudo-folder — and note
    that this is "has no placement in one", not "has a placement outside one",
    because on a label server your sent mail is also in \\All and the second
    reading would offer to delete it;
  * not flagged and not answered, anywhere it sits.

There is deliberately no age floor. An earlier version of this only looked at
mail over a month old, on the theory that this month's notifications might still
be worth reading; in practice that hid the groups people most want gone — the
sender who has written to you four times this week is exactly the sender you are
looking for — and it meant the totals answered a narrower question than the one
being asked. Cleanup reads the whole mailbox, and what keeps today's mail safe is
the rest of the filter, not its date.

And a group is only offered when it has at least as many conversations as half
its messages. That is what separates a flood of notifications — one thread each
— from a long back-and-forth with a person, where twenty messages share one
thread and identical subjects. Without it, "Re: the house" from your solicitor
is a group of thirty.

What survives all of that still only ever goes to Trash, under one undo id, by
the same engine every other delete in meerail goes through (app/mailops.py).
Nothing here empties anything.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, literal, or_, select
from sqlalchemy.orm import Session as DBSession, selectinload

from core import bodysig, undo
from core.database import get_db
from core.models import Account, Mailbox, Message, MessageLocation
from .. import mailops
from ..deps import require_ui_auth

router = APIRouter(prefix="/api/cleanup", tags=["cleanup"], dependencies=[Depends(require_ui_auth)])

# Folders whose contents are never a cleanup candidate. Sent and Drafts are
# yours; Junk and Trash are already on their way out and offering them again
# would be the panel proposing to delete what is deleted. \\Flagged is Proton's
# and Gmail's starred pseudo-folder: a placement in it *is* a star.
PROTECTED_ROLES = ("sent", "drafts", "junk", "trash", "flagged")

# Smallest group worth showing. Below five this stops reading as "this sender
# does this to you constantly" and starts reading as coincidence.
DEFAULT_MIN_COUNT = 5

# A template shorter than this is not a template. Mail with no subject at all
# groups into one enormous bucket of unrelated things, which is the single most
# dangerous group this could offer.
MIN_TEMPLATE_LEN = 3

# Groups returned. The panel is meant to be worked from the top, so the tail
# past this is whatever the chosen order ranks lowest — see `sort`.
DEFAULT_LIMIT = 100

# Messages trashed per request. The client loops until `done`, exactly as the
# bulk endpoints in app/routers/actions.py do, and for the same reason: each
# call is its own committed transaction, so one that covered a ten-thousand
# message group would hold the mailbox for the whole of it.
CHUNK = 1000

# The subject, as a template: lower-cased, whitespace collapsed, and every run
# of digits replaced by '#'. Dates, counts, ticket numbers, sizes and prices are
# all digits, and they are the entire difference between one copy of a
# notification and the next.
#
# Deliberately *not* messages.subject_norm, which is what threading uses: that
# one strips "Re:"/"Fwd:" as well, so a reply and the message it answers share
# it. Here that would quietly merge a conversation into the thing it is a reply
# to, which is the one merge this must never make.
_TEMPLATE = func.btrim(
    func.regexp_replace(
        func.regexp_replace(func.lower(Message.subject), r"\s+", " ", "g"),
        "[0-9]+", "#", "g",
    )
)


def _protected():
    """EXISTS a placement that takes this message out of consideration."""
    return (
        select(literal(1))
        .select_from(MessageLocation)
        .join(Mailbox, Mailbox.id == MessageLocation.mailbox_id)
        .where(
            MessageLocation.message_pk == Message.id,
            or_(
                Mailbox.role.in_(PROTECTED_ROLES),
                MessageLocation.flagged.is_(True),
                MessageLocation.answered.is_(True),
            ),
        )
        .exists()
    )


def _placed():
    """EXISTS a placement at all — a message with none is already gone."""
    return (
        select(literal(1))
        .select_from(MessageLocation)
        .where(MessageLocation.message_pk == Message.id,
               MessageLocation.deleted.is_(False))
        .exists()
    )


def _unread():
    """No placement has been seen. Reported, not filtered on: a pile of unread
    notifications is the strongest argument for deleting the pile, not against
    it — but it is the user's call, so the panel shows the number."""
    return ~(
        select(literal(1))
        .select_from(MessageLocation)
        .where(MessageLocation.message_pk == Message.id,
               MessageLocation.seen.is_(True))
        .exists()
    )


def _sent_at():
    """When the message is dated, falling back to when it arrived.

    Only the years a group spans are read off this now — nothing is selected or
    rejected by it. The fallback still matters for what it says: date_sent is
    NULL when the Date header was missing or unparseable, which is most common
    in the oldest mail in a long-lived mailbox, and a group of it would
    otherwise be dated from nothing at all.
    """
    return func.coalesce(Message.date_sent, Message.date_received)


def _candidates(account_id: int | None):
    """The WHERE clauses that decide what may be grouped and what may be filed.

    Applied to the listing *and* to the delete, on purpose — the delete
    re-derives its message set from the group's key rather than trusting a list
    of ids from the client, so every protection above holds at the moment the
    mail moves and not merely at the moment it was drawn.
    """
    conds = [
        _placed(),
        ~_protected(),
    ]
    if account_id is not None:
        conds.append(Message.account_id == account_id)
    return conds


class TrashRequest(BaseModel):
    """One group, named the way the listing named it.

    No message ids: the server resolves the group again from `from_addr` + `key`
    under the same filter it listed it with. That costs a query and buys the
    guarantee that a stale panel — one drawn before a sync, or before another
    window flagged something in the group — cannot talk this into moving mail
    the filter would now protect.
    """
    mode: str
    from_addr: str
    key: str
    account_id: int | None = None


# --- Grouping by subject template ---------------------------------------------


def _subject_groups(db: DBSession, account_id: int | None, min_count: int) -> list[dict]:
    f = (
        select(
            Message.from_addr.label("from_addr"),
            Message.from_name.label("from_name"),
            Message.subject.label("subject"),
            _TEMPLATE.label("template"),
            Message.thread_id.label("thread_id"),
            Message.size_bytes.label("size_bytes"),
            Message.has_attachments.label("has_attachments"),
            _unread().label("unread"),
            _sent_at().label("at"),
        )
        .where(*_candidates(account_id))
        .subquery()
    )
    rows = db.execute(
        select(
            f.c.from_addr,
            func.min(f.c.from_name).label("from_name"),
            f.c.template,
            func.count().label("count"),
            func.count(func.distinct(f.c.thread_id)).label("threads"),
            func.count(func.distinct(f.c.subject)).label("subjects"),
            func.coalesce(func.sum(f.c.size_bytes), 0).label("bytes"),
            func.count().filter(f.c.has_attachments).label("attachments"),
            func.count().filter(f.c.unread).label("unread"),
            func.min(f.c.at).label("first"),
            func.max(f.c.at).label("last"),
        )
        .where(func.length(f.c.template) >= MIN_TEMPLATE_LEN)
        .group_by(f.c.from_addr, f.c.template)
        # In the database, not in Python: without it this hands back a row for
        # every distinct subject in the mailbox — sixty thousand of them on a
        # large one — to throw all but a couple of thousand away.
        .having(func.count() >= min_count)
    ).all()
    return [
        {
            "from_addr": r.from_addr,
            "from_name": r.from_name or "",
            "key": r.template,
            "label": r.template,
            "count": r.count,
            "threads": r.threads,
            "subjects": r.subjects,
            "bytes": int(r.bytes or 0),
            "attachments": r.attachments,
            "unread": r.unread,
            "first": _iso(r.first),
            "last": _iso(r.last),
        }
        for r in rows
    ]


# --- Grouping by near-duplicate body ------------------------------------------


def _components(rows) -> dict[int, list]:
    """Group near-duplicate bodies: seed message id -> the mail that resembles it.

    Star clustering, not connected components, and the difference is the whole
    behaviour of this feature. Both start from the same edges — two messages
    from one sender are near-duplicates when their fingerprints agree on a band
    — but a connected component follows those edges *transitively*, and over LSH
    buckets that chains: A resembles B, B resembles C, and nothing says A
    resembles C. On a real mailbox that produced a "group" of 438 messages from
    a colleague, held together end to end by quoted replies and a signature.

    A star is one hop from a centre, so every member resembles the same one
    message and the group can be stated in a sentence — "everything that looks
    like this" — which is also exactly what `:similar` searches for and what the
    delete re-derives. Those three agreeing is what lets the panel open a group
    in the message list and have the row count match the button.

    Seeds are taken most-connected first, so the densest centre claims its
    neighbourhood before a message on the edge of it can start a group of its
    own; each message is claimed once.
    """
    buckets: dict[tuple, list[int]] = {}
    by_id: dict[int, object] = {}
    for row in rows:
        by_id[row.id] = row
        for band, token in enumerate(row.body_sig.split()):
            # The sender is part of the bucket key, so a group never spans two
            # of them, and so is the band *position*: two messages whose first
            # bands match are alike, one whose first band equals another's third
            # is a coincidence of hashing.
            buckets.setdefault((row.from_addr, band, token), []).append(row.id)

    def bands(row):
        return [(row.from_addr, band, token)
                for band, token in enumerate(row.body_sig.split())]

    # How much company each message has, summed over its buckets. An
    # over-estimate where two of its bands hold the same neighbours, which is
    # fine: this only decides who gets to be a centre first.
    reach = {mid: sum(len(buckets[key]) for key in bands(row))
             for mid, row in by_id.items()}

    unclaimed = set(by_id)
    groups: dict[int, list] = {}
    for seed in sorted(by_id, key=lambda mid: (reach[mid], mid), reverse=True):
        if seed not in unclaimed:
            continue
        members = []
        for key in bands(by_id[seed]):
            for other in buckets[key]:
                if other in unclaimed:
                    unclaimed.discard(other)
                    members.append(by_id[other])
        groups[seed] = members
    return groups


def _body_rows(db: DBSession, account_id: int | None, from_addr: str | None = None):
    """Candidate rows for the grouping — everything it needs and no text.

    This is read over the whole mailbox, so what is *not* selected matters: the
    subject and the body would be tens of megabytes crossing into Python for
    something only a hundred groups will ever show. The thread id and the size
    are here rather than in the second pass because the headline totals and the
    conversation test have to cover every group, not just the ones drawn.
    """
    conds = list(_candidates(account_id))
    conds += [Message.body_sig.is_not(None), Message.body_sig != ""]
    if from_addr is not None:
        conds.append(Message.from_addr == from_addr)
    return db.execute(
        select(Message.id, Message.from_addr, Message.body_sig,
               Message.thread_id, Message.size_bytes).where(*conds)
    ).all()


def _body_groups(db: DBSession, account_id: int | None,
                 min_count: int, limit: int, sort: str) -> tuple[list[dict], dict]:
    rows = _body_rows(db, account_id)
    groups = [
        (seed, m) for seed, m in _components(rows).items()
        if len(m) >= min_count and len({r.thread_id for r in m}) * 2 >= len(m)
    ]
    # Measured here rather than in the second pass, which is why _body_rows
    # selects size_bytes: the order decides which groups the second pass is even
    # *for*, so both figures have to be known before the list is cut to `limit`.
    sized = sorted(
        ((sum(int(r.size_bytes or 0) for r in m), len(m), seed, m) for seed, m in groups),
        key=lambda row: row[0] if sort == "size" else row[1], reverse=True)

    # Over every qualifying group, not only the drawn ones: the headline is
    # "this is what the mailbox is carrying", and a figure that silently meant
    # "the top hundred of it" would understate the answer by most of itself.
    totals = {
        "groups": len(sized),
        "messages": sum(n for _, n, _, _ in sized),
        "bytes": sum(size for size, _, _, _ in sized),
    }
    shown = [(seed, m) for _, _, seed, m in sized[:limit]]
    if not shown:
        return [], totals

    # Second pass for the parts a person reads. Scoped by sender rather than by
    # a list of ids: a hundred groups can be twenty thousand messages, and a
    # hundred addresses is a parameter list Postgres does not have to think
    # about. Membership is still decided by the ids from the first pass.
    senders = {m[0].from_addr for _, m in shown}
    facts = {
        r.id: r
        for r in db.execute(
            select(
                Message.id,
                Message.from_name,
                Message.subject,
                Message.thread_id,
                Message.size_bytes,
                Message.has_attachments,
                _unread().label("unread"),
                _sent_at().label("at"),
            ).where(*_candidates(account_id), Message.from_addr.in_(senders))
        ).all()
    }

    out = []
    for seed, members in shown:
        rows_ = [facts[r.id] for r in members if r.id in facts]
        if not rows_:
            continue
        subjects = Counter(r.subject for r in rows_)
        dates = [r.at for r in rows_ if r.at is not None]
        out.append({
            "from_addr": members[0].from_addr,
            "from_name": next((r.from_name for r in rows_ if r.from_name), ""),
            # The seed: the message every other member resembles. It names the
            # group for the delete, and it is what the panel puts in the search
            # box as `:similar=<id>` — one identity for both, so what you review
            # is what you remove. It stays valid after the group is trashed,
            # because trashing moves a message rather than forgetting it.
            "key": str(seed),
            # The subject most of them carry. Unlike the subject mode there is
            # no template to show — the whole point of this mode is that the
            # subjects are all different — so the panel shows the commonest one
            # and says how many others there are.
            "label": subjects.most_common(1)[0][0] or "(no subject)",
            "count": len(rows_),
            "threads": len({r.thread_id for r in rows_}),
            "subjects": len(subjects),
            "bytes": sum(int(r.size_bytes or 0) for r in rows_),
            "attachments": sum(1 for r in rows_ if r.has_attachments),
            "unread": sum(1 for r in rows_ if r.unread),
            "first": _iso(min(dates)) if dates else None,
            "last": _iso(max(dates)) if dates else None,
        })
    # `totals` is deliberately not recomputed from `out`: out holds the hundred
    # groups being drawn, and a headline summed from those would say the mailbox
    # is carrying a tenth of what it is carrying. This line used to do exactly
    # that — it was left behind when the totals moved up to cover every
    # qualifying group — and it made the figure quietly depend on ?limit.
    return out, totals


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# --- Shared shaping -----------------------------------------------------------


def _flood(count: int, threads: int) -> bool:
    """Whether a group is a flood of notifications rather than a conversation.

    A back-and-forth is one thread carrying many messages; a flood is one thread
    per message. Half is the line, and it is drawn generously towards *not*
    offering the group: a sender who threads their alerts in pairs still clears
    it, and a discussion where two people each also started a separate thread on
    the same subject does not.
    """
    return threads * 2 >= count


@router.get("/clusters")
def clusters(
    mode: str = Query("subject", pattern="^(subject|body)$"),
    account_id: int | None = None,
    min_count: int = Query(DEFAULT_MIN_COUNT, ge=2, le=1000),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=500),
    sort: str = Query("size", pattern="^(size|count)$"),
    db: DBSession = Depends(get_db),
):
    """The groups, biggest first, with what each one would cost to keep.

    `sort` picks what "biggest" means, because there are two honest answers and
    they disagree sharply. By size is the default: this is a list of things to
    reclaim, and forty newsletters carrying a megabyte of images each free more
    disk than a thousand one-line build failures. By count is the other reading
    — the sender who interrupts you most — and on a real mailbox the top of the
    two lists has almost nothing in common.

    Whichever is chosen, it decides which groups are drawn at all, not merely
    the order they appear in: the list is cut to `limit` after sorting.

    `pending` is how many messages still have no body fingerprint. It is always
    reported and is always zero on an install that has never had an upgrade
    behind it; on one that has, the body mode would otherwise group the half of
    the mailbox the backfill had reached and say nothing at all about the rest,
    which reads as "there is nothing more to clean up". The panel says so
    instead. See core/bodysig.py and app/workers.py::body_sig_loop.
    """
    pending = bodysig.pending(db)

    if mode == "body":
        groups, totals = _body_groups(db, account_id, min_count, limit, sort)
    else:
        groups = [g for g in _subject_groups(db, account_id, min_count)
                  if _flood(g["count"], g["threads"])]
        groups.sort(key=lambda g: g["bytes"] if sort == "size" else g["count"], reverse=True)
        totals = {
            "groups": len(groups),
            "messages": sum(g["count"] for g in groups),
            "bytes": sum(g["bytes"] for g in groups),
        }
        groups = groups[:limit]

    accounts = db.execute(
        select(Account.id, Account.email, Account.label).order_by(Account.id)
    ).all()
    return {
        "mode": mode,
        "sort": sort,
        "min_count": min_count,
        "pending": pending,
        # Same shape the stats modal gets, so the account picker in both panels
        # is built from the payload it is already waiting on rather than a
        # second request of its own.
        "accounts": [{"id": a.id, "email": a.email, "label": a.label} for a in accounts],
        "totals": totals,
        "clusters": groups,
    }


# --- Filing -------------------------------------------------------------------


def _group_message_ids(db: DBSession, req: TrashRequest) -> list[int]:
    """The messages a group's key names, resolved fresh under the live filter."""
    if req.mode == "body":
        # Re-derived from the seed's own fingerprint rather than by clustering
        # again, which matters once a group is larger than one chunk: the second
        # call arrives with part of the group already in Trash, and a fresh
        # clustering of what is left is not the same clustering. A predicate is
        # the same predicate every time, so the loop converges — and it is the
        # same predicate the search box ran, so what was reviewed is what goes.
        sig = db.scalar(select(Message.body_sig).where(Message.id == int(req.key))) \
            if req.key.isdigit() else None
        clause = bodysig.shares_band(Message.body_sig, sig or "")
        if clause is None:
            return []
        return list(db.execute(
            select(Message.id).where(
                *_candidates(req.account_id),
                Message.from_addr == req.from_addr,
                Message.body_sig.is_not(None),
                Message.body_sig != "",
                clause,
            )
        ).scalars().all())

    return list(db.execute(
        select(Message.id).where(
            *_candidates(req.account_id),
            Message.from_addr == req.from_addr,
            _TEMPLATE == req.key,
        )
    ).scalars().all())


@router.post("/trash")
def trash_cluster(req: TrashRequest, db: DBSession = Depends(get_db)):
    """Move one whole group to Trash, a chunk at a time.

    Returns the same shape the bulk endpoints do — `moved`, `done` and an
    `op_id` — so the client loops on `done` and the Recent actions panel picks
    the operation up with an Undo on it without knowing this endpoint exists.

    What this checks and what it does not, because the split is easy to get
    wrong later: the *protections* — Sent, Drafts, Junk, Trash, flagged,
    answered — are re-applied here, on the messages this is about to move, so
    they hold whatever key arrives. The listing's other two rules are
    not: `min_count` and the conversation test in `_flood` decide what is worth
    *offering*, and a caller naming a group directly is a person who has decided
    they want it, exactly as ticking those rows in the list and pressing Delete
    would be. Do not promote either into a refusal here without also giving the
    user some other way to say "yes, that one".
    """
    if req.mode not in ("subject", "body"):
        raise HTTPException(status_code=400, detail="Unknown grouping")
    if not req.key:
        raise HTTPException(status_code=400, detail="No group named")

    ids = _group_message_ids(db, req)
    if not ids:
        # Not an error: a group whose mail has already gone — trashed from
        # another window, or filed by the loop's own previous chunk — is a group
        # that got what was asked of it.
        return {"ok": True, "moved": 0, "done": True, "op_id": None}

    chunk, remaining = ids[:CHUNK], max(0, len(ids) - CHUNK)
    msgs = db.execute(
        select(Message)
        .where(Message.id.in_(chunk))
        # Without this every message lazy-loads its own placements one round
        # trip at a time inside move_messages — the same reason bulk_trash does it.
        .options(selectinload(Message.locations))
    ).scalars().all()

    # One id per chunk rather than per group, for the reason bulk_trash_all
    # gives: the chunks are separate committed transactions, and a single id
    # spanning them would offer an Undo that could only take back the last one.
    op_id = undo.new_op_id()
    touched: set[int] = set()
    accounts: set[int] = set()
    moved = 0
    by_account: dict[int, list[Message]] = {}
    for msg in msgs:
        by_account.setdefault(msg.account_id, []).append(msg)

    for account_id, group in by_account.items():
        # move_messages reads the collapse rule off the first message's account,
        # so the split above is load-bearing, not tidiness.
        target = mailops.trash_mailbox(db, account_id)
        moved += mailops.move_messages(db, group, target, touched, op_id, "trash")
        accounts.add(account_id)

    mailops.recompute(db, touched)
    db.commit()
    mailops.announce(db, accounts, moved)
    return {"ok": True, "moved": moved, "done": remaining == 0, "op_id": op_id}
