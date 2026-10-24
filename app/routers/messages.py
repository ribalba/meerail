"""Read APIs for the mail UI: message list, detail, thread, attachments."""

from __future__ import annotations

import io
import re
import zipfile
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import Integer, String, and_, case, column, func, or_, select, tuple_, values
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session as DBSession

from core import ingest
from core.database import get_db
from core.mail.parse import html_to_text, is_calendar_part
from .. import reminders as reminders_core, searchquery, spanning
from ..deps import require_ui_auth
from ..mail.render import sanitize_html
from .mailboxes import ROLE_ORDER
from core.models import (
    Account, Attachment, Mailbox, Message, MessageLocation, Recipient, Reminder, Setting,
    parked_out, still_filed,
)

router = APIRouter(prefix="/api", tags=["messages"], dependencies=[Depends(require_ui_auth)])


def _resolve_mailbox_ids(db: DBSession, mailbox_id: int | None, scope: str | None) -> list[int]:
    if mailbox_id is not None:
        return [mailbox_id]
    if scope == "unified_inbox":
        return list(db.execute(select(Mailbox.id).where(Mailbox.role == "inbox")).scalars().all())
    return []  # flagged/other scopes filter differently (see below)


def _not_deleted():
    """A message still filed somewhere the user hasn't deleted it from.

    The predicate itself is `core.models.still_filed` — it moved there when a
    second reader outside this package needed it (app/threadtext.py, which turns
    a conversation into the text an AI feature sends out) and two copies of "does
    this mail still exist" would have been one too many. This name stays because
    every read path in this module is written in terms of it.
    """
    return still_filed()


def _readable(db: DBSession, message_id: int) -> Message:
    """The message behind an id, if the user has not deleted it. 404 otherwise —
    the same answer as for one that was never here, because for someone who
    emptied their Trash those are the same thing."""
    msg = db.get(Message, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    placed = db.scalar(
        select(MessageLocation.id).where(MessageLocation.message_pk == message_id,
                                         MessageLocation.deleted.is_(False)).limit(1)
    )
    if placed is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return msg


def folders_of(placements) -> list[dict]:
    """Where a message is filed, from ``(mailbox id, role, display name, parked)``.

    The chips the list and the reader draw over a message: which folder it is
    actually in, which is the one thing neither a search result nor a
    conversation half-deleted from can otherwise say.

    Deduped by folder — a label server can file the same message under one
    folder twice, with two UIDs — and ordered the way the sidebar orders its
    folders, so Inbox leads and a custom folder trails wherever the message
    turns up. The leaf name only: the full path is the browser's to add, out of
    the folder tree it already holds (see App.folderChips).

    ``parked`` rides along rather than replacing the role. The mail really is
    in INBOX (see MessageLocation.parked), and the browser reads the role to
    decide things that parking does not change, such as where a move starts
    from and whether Send & Archive applies. Only the chip says Parked instead.
    A folder counts as parked only when every copy of the message in it is.
    """
    seen: dict[int, dict] = {}
    for mailbox_id, role, name, parked in placements:
        before = seen.get(mailbox_id)
        seen[mailbox_id] = {"mailbox_id": mailbox_id, "role": role, "name": name,
                            "parked": bool(parked) and (before is None or before["parked"])}
    return sorted(seen.values(),
                  key=lambda f: (ROLE_ORDER.get(f["role"], 8), f["name"].lower()))


def account_ids_param(given: list[int]) -> list[int]:
    """The ``account_id`` list a conversation route was given, each id once.

    A conversation can be in several accounts (app/spanning.py), so the routes
    that name one by its thread id take the accounts as a repeated query
    parameter. One value is still the common case, and the shape every older
    client sends. The order is kept, because the first account named is the one
    the list row stood for.

    Counted before the duplicates go, so the bound is on what was sent. An
    unbounded list would be an unbounded IN clause, which is not a request
    anyone makes by hand.
    """
    if len(given) > spanning.MAX_ACCOUNTS:
        raise HTTPException(
            status_code=422,
            detail=f"A conversation can be asked for in at most {spanning.MAX_ACCOUNTS} "
                   f"accounts at once.")
    accounts = spanning.distinct_ids(given)
    if not accounts:
        raise HTTPException(status_code=422, detail="Say which account the conversation is in.")
    return accounts


def also_ids(given: list[int], message_id: int) -> list[int]:
    """The ``also`` list of a message verb, each id once and never the message itself.

    ``also`` names the other copies of the same mail in other accounts (see
    app/spanning.py), and always means "the same change for these too". The
    reader sends it because it shows those copies as one card, and a keypress
    on the card that reached only one copy would leave the other unread, or in
    the inbox, with nothing on screen to say so. Here beside account_ids_param
    because the reminder routes take it as well as the action routes.

    Bounded so a request is. Nobody has a mail in fifty accounts.
    """
    if len(given) > spanning.MAX_ALSO:
        raise HTTPException(
            status_code=422,
            detail=f"At most {spanning.MAX_ALSO} other copies can be changed along with "
                   f"one message.")
    return [i for i in spanning.distinct_ids(given) if i != message_id]


def _thread_counts(db: DBSession, groups: dict) -> dict:
    """How many mails each conversation holds across all folders.

    ``groups`` maps a caller's key to the ``(account, thread)`` pairs that make
    up one conversation: a single pair in a folder list, one per account where a
    list merges a conversation across accounts. The answer counts a twin once
    (app/spanning.py), because the reader shows it once, and a count that said
    "2" over a conversation that opens as one message would be a count of
    something nobody can see.

    Per twin key, the largest number of copies any one of the accounts holds.
    For a single account that is simply every message, the same number as
    before conversations spanned accounts. Two copies of one mail inside one
    account are two messages there and count twice, as the reader shows them.

    One statement for the whole page. The groups travel as a VALUES list, so
    two conversations sharing a thread id in different accounts on the same
    page (two flagged rows, say) are still counted apart.
    """
    groups = {key: pairs for key, pairs in groups.items() if pairs}
    if not groups:
        return {}
    keys = list(groups)
    g = values(column("gid", Integer), column("account_id", Integer),
               column("thread_id", String), name="g").data(
        [(gid, account_id, thread_id)
         for gid, key in enumerate(keys) for account_id, thread_id in groups[key]])
    twin = spanning.twin_key()
    per_account = (
        select(g.c.gid, Message.account_id, twin.label("twin"), func.count().label("n"))
        .select_from(g)
        .join(Message, and_(Message.account_id == g.c.account_id,
                            Message.thread_id == g.c.thread_id))
        .where(_not_deleted())
        .group_by(g.c.gid, Message.account_id, twin)
    ).subquery()
    per_mail = (
        select(per_account.c.gid, func.max(per_account.c.n).label("n"))
        .group_by(per_account.c.gid, per_account.c.twin)
    ).subquery()
    rows = db.execute(
        select(per_mail.c.gid, func.sum(per_mail.c.n)).group_by(per_mail.c.gid)
    ).all()
    return {keys[gid]: int(n) for gid, n in rows}


@router.get("/messages")
def list_messages(
    db: DBSession = Depends(get_db),
    mailbox_id: int | None = None,
    scope: str | None = Query(None, description="unified_inbox | flagged | reminders | parked"),
    unread_only: bool = False,
    # Bounded at both ends. A ceiling alone leaves the floor open, and a negative
    # limit is not a smaller page: SQLite reads it as "no limit at all" and
    # Postgres refuses the query outright, so the same URL is an uncapped read on
    # one and a 500 on the other. Neither is an answer to a request for -1 rows.
    limit: int = Query(60, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """A date-descending list of *conversations* in a folder/scope.

    One row per thread, not per message: a reply landing in the inbox should
    bump the conversation you already have, not stack a second entry beside it.
    The row shows the newest message in this folder and opening it loads the
    whole thread in the reader.

    Unread/flagged are rolled up across the thread's messages *in this folder*,
    so a conversation reads as unread while any part of it is — which is what
    the badge in the sidebar counts too.

    Flagged is the one scope that is not a list of conversations — see
    ``by_message`` below.

    The lists that already mix accounts (spanning.SPANNING_SCOPES: All Inboxes,
    Parked, Reminders) merge a conversation across them. The same mail
    delivered to two accounts is one conversation to the person who got it,
    and two rows side by side for it read as a duplicate. There a conversation
    is its thread key whatever the account, the rollups run over every account's
    part of it, and the row speaks for the newest message of all of them. Folder
    lists do not merge: a folder belongs to one account.

    Every row carries ``members``, one entry per account it stands for, with
    that account's own message and folder, so the browser can act on every
    account at once and draw each account's colour. A row that merged nothing
    has exactly one member, equal to its own fields. Everything else on the row
    is the representative's.
    """
    # Flagged is a list of *messages*. A folder holds conversations, so its list
    # collapses to one row each; a flag is put on a single mail, by hand, and
    # the list of them is the list of those mails. Rolled up like a folder it
    # showed one row per conversation, so flagging a second message in a
    # conversation already in here changed nothing on screen — the row was
    # already there, standing for a different mail — and the message just
    # flagged could not be found anywhere (issue #21).
    by_message = scope == "flagged"
    across = scope in spanning.SPANNING_SCOPES

    # Messages that never got threaded stand alone rather than collapsing into
    # one "no thread" pile. That is also what keeps them from merging across
    # accounts: their key is their own row id, which no other message shares.
    thread_key = func.coalesce(Message.thread_id, func.concat("msg:", Message.id)).label("thread_key")

    j = select(
        Message.id, Message.thread_id, Message.subject, Message.from_name, Message.from_addr,
        Message.date_sent, Message.snippet, Message.has_attachments, Message.content_status,
        MessageLocation.seen, MessageLocation.flagged, MessageLocation.answered,
        Message.account_id, Account.color, MessageLocation.mailbox_id, Mailbox.role,
        thread_key,
    ).select_from(MessageLocation).join(
        Message, Message.id == MessageLocation.message_pk
    ).join(Mailbox, Mailbox.id == MessageLocation.mailbox_id).join(
        Account, Account.id == Message.account_id
    ).where(MessageLocation.deleted.is_(False))

    if scope == "flagged":
        j = j.where(MessageLocation.flagged.is_(True))
    elif scope == "reminders":
        # Mail put off until later. Not a folder — the messages are sitting in
        # Archive, which is where a reminder parks them (app/reminders.py) — so
        # this selects them by the promise rather than by where they are.
        #
        # By conversation where there is one, because that is what a reminder
        # acts on: matching only the message it was set on would show an older
        # reply as the row for a thread whose newest mail is parked beside it.
        #
        # The conversation in the reminder's own account. A thread id is the
        # root Message-ID, so the same conversation has the same id in every
        # account that got it, and matching on the id alone listed the other
        # account's copy here too, sitting in its inbox with nothing put off.
        # Now that this list merges a conversation across accounts, that copy
        # would also join the row as a member and be carried along by the next
        # keypress on it.
        j = j.where(or_(
            tuple_(Message.account_id, Message.thread_id).in_(
                select(Reminder.account_id, Reminder.thread_id)
                .where(Reminder.state == "pending", Reminder.thread_id.is_not(None))),
            Message.id.in_(
                select(Reminder.message_pk)
                .where(Reminder.state == "pending", Reminder.thread_id.is_(None))),
        ))
    elif scope == "parked":
        # Inbox mail the user took off the inbox without filing it anywhere
        # (app/mailops.py, "parking"). Every account's, like All Inboxes. It is
        # one place to the person who put things there, not one per account.
        j = j.where(parked_out())
    else:
        ids = _resolve_mailbox_ids(db, mailbox_id, scope)
        if not ids:
            return {"rows": [], "total": 0}
        # Parked mail is still filed in INBOX, and this is what keeps it out of
        # the inbox lists. Applied to every folder rather than only to inboxes,
        # because nothing outside an inbox is ever parked and elsewhere it is a
        # test that is always true. A folder list that could hide mail without
        # Parked showing it would be the one way to lose sight of some.
        j = j.where(MessageLocation.mailbox_id.in_(ids), ~parked_out())

    if unread_only:
        j = j.where(MessageLocation.seen.is_(False))

    # DISTINCT ON keeps the first row per conversation under this ORDER BY,
    # i.e. the newest message of each. The outer query then sorts those
    # representatives by date, since DISTINCT ON dictates the inner order.
    #
    # A conversation is (account, thread) in a folder list, and the thread
    # alone in a list that spans accounts. There the account breaks a tie on
    # the date before the id does, so that twins, which share their date,
    # always elect the copy in the same account. For a single account that tie
    # break changes nothing.
    #
    # By message where the list is of messages, and still DISTINCT: the rows are
    # placements, and a label server files one mail in several folders at once —
    # deduping on the mail is what keeps a flagged message that is in INBOX and
    # in \All from arriving here twice. Which placement speaks for it decides
    # only the folder the row names, so the real folder wins over the \All
    # bucket every message on such a server is also in.
    #
    # And on the mail rather than the row, because a flagged mail delivered to
    # two accounts is still one mail: twins (app/spanning.py) share a row. The
    # copy number is what keeps two copies inside one account apart. They are
    # two messages there, so the first copy in each account pairs with the
    # first in the others, the second with the second, and none of an account's
    # copies is hidden behind another. The lowest account speaks for the row,
    # for the same stability as above.
    copies = real_first = None
    if by_message:
        twin = spanning.twin_key()
        copies = j.add_columns(
            twin.label("twin_key"),
            func.dense_rank().over(partition_by=(Message.account_id, twin),
                                   order_by=Message.id).label("copy_no"),
        ).subquery()
        real_first = case((copies.c.role == "all", 1), else_=0)
        reps = select(copies).distinct(copies.c.twin_key, copies.c.copy_no).order_by(
            copies.c.twin_key, copies.c.copy_no, copies.c.account_id, real_first,
            copies.c.mailbox_id,
        ).subquery()
    elif across:
        reps = j.distinct(thread_key).order_by(
            thread_key, Message.date_sent.desc().nulls_last(), Message.account_id,
            Message.id.desc()
        ).subquery()
    else:
        reps = j.distinct(Message.account_id, thread_key).order_by(
            Message.account_id, thread_key, Message.date_sent.desc().nulls_last(), Message.id.desc()
        ).subquery()

    def group_of(r):
        """The key a row's conversation (or, in Flagged, its mail) is grouped by."""
        if by_message:
            return (r.twin_key, r.copy_no)
        return r.thread_key if across else (r.account_id, r.thread_key)

    total = db.scalar(select(func.count()).select_from(reps))
    rows = db.execute(
        # The id breaks a tie on the date, so that paging cannot show one row
        # twice and skip another: mail imported in a batch commonly shares a
        # timestamp, and without a total order the pages need not agree on
        # which of those rows came first.
        select(reps).order_by(reps.c.date_sent.desc().nulls_last(), reps.c.id.desc())
        .limit(limit).offset(offset)
    ).all()

    # Unread/flagged roll up over the *folder-filtered* set: a conversation
    # reads as unread while any part of it here is, which is what the sidebar
    # badge counts too. Across accounts that is every account's part of it.
    #
    # A list of messages rolls up too, but only over the copies of one mail.
    # The row is that mail and says how it is flagged and whether it has been
    # read, and a mail delivered twice has been read once every copy of it
    # has, which is also how the reader decides it (get_thread). With no
    # twins that is the one mail's own placements in this list.
    keys = {group_of(r) for r in rows}
    rollup: dict = {}
    if keys and by_message:
        grouped = select(
            copies.c.twin_key, copies.c.copy_no,
            func.bool_or(copies.c.seen.is_(False)), func.bool_or(copies.c.flagged),
        ).where(tuple_(copies.c.twin_key, copies.c.copy_no).in_(keys)).group_by(
            copies.c.twin_key, copies.c.copy_no)
        for twin_key, copy_no, any_unread, any_flagged in db.execute(grouped).all():
            rollup[(twin_key, copy_no)] = (bool(any_unread), bool(any_flagged))
    elif keys and across:
        grouped = j.with_only_columns(
            thread_key,
            func.bool_or(MessageLocation.seen.is_(False)),
            func.bool_or(MessageLocation.flagged),
        ).where(thread_key.in_(keys)).group_by(thread_key)
        for key, any_unread, any_flagged in db.execute(grouped).all():
            rollup[key] = (bool(any_unread), bool(any_flagged))
    elif keys:
        grouped = j.with_only_columns(
            Message.account_id, thread_key,
            func.bool_or(MessageLocation.seen.is_(False)),
            func.bool_or(MessageLocation.flagged),
        ).where(tuple_(Message.account_id, thread_key).in_(keys)).group_by(Message.account_id, thread_key)
        for account_id, key, any_unread, any_flagged in db.execute(grouped).all():
            rollup[(account_id, key)] = (bool(any_unread), bool(any_flagged))

    # Which accounts each row stands for, and that account's own message in
    # each. The browser needs one per account to act on a merged row (archive
    # it in both, park both, mark both read) and to say which accounts it is
    # in. The newest message of each account here, picked by the same order
    # that elected the representative. A Flagged row lists each copy of its
    # mail, in the folder that speaks for it.
    #
    # The representative's own entry is built from the row itself rather than
    # read back, so a row that merged nothing has a member exactly equal to it.
    # Read back, a message filed in two folders of the list could name the
    # other one.
    found: dict = {}
    if keys and by_message:
        merged = (
            select(copies.c.twin_key, copies.c.copy_no, copies.c.account_id,
                   copies.c.thread_id, copies.c.id, copies.c.mailbox_id, copies.c.color)
            .where(tuple_(copies.c.twin_key, copies.c.copy_no).in_(keys))
            .distinct(copies.c.id)
            .order_by(copies.c.id, real_first, copies.c.mailbox_id)
        )
        for m in db.execute(merged).all():
            found.setdefault((m.twin_key, m.copy_no), []).append(m)
    elif keys and across:
        merged = (
            j.with_only_columns(thread_key, Message.account_id, Message.thread_id, Message.id,
                                MessageLocation.mailbox_id, Account.color)
            .where(thread_key.in_(keys))
            .distinct(thread_key, Message.account_id)
            .order_by(thread_key, Message.account_id,
                      Message.date_sent.desc().nulls_last(), Message.id.desc())
        )
        for m in db.execute(merged).all():
            found.setdefault(m.thread_key, []).append(m)
    members = [
        [_member(r), *sorted((_member(m) for m in found.get(group_of(r), [])
                              if m.account_id != r.account_id),
                             key=lambda m: m["account_id"])]
        for r in rows
    ]

    # The count, though, spans folders, because that is what opening the row
    # shows: the reader loads the whole conversation regardless of where its
    # messages live. Counting only this folder made the badge say "2" and the
    # reader then render every message of a 900-strong thread.
    #
    # Over the members whose thread the reader will open with the row, which is
    # every account of a merged conversation and, in Flagged, the copies that
    # share the row's thread id (the client opens a row in exactly those).
    counts = _thread_counts(db, {
        i: {(m["account_id"], m["thread_id"]) for m in ms if m["thread_id"] == r.thread_id}
        for i, (r, ms) in enumerate(zip(rows, members)) if r.thread_id
    })
    remind = _remind_times(db, members)

    return {
        "total": int(total or 0),
        "rows": [
            {
                "id": r.id, "thread_id": r.thread_id, "subject": r.subject or "(no subject)",
                "from_name": r.from_name, "from_addr": r.from_addr,
                "date": r.date_sent.isoformat() if r.date_sent else None,
                "snippet": r.snippet, "has_attachments": r.has_attachments,
                # A row with no snippet because there is no body to take one
                # from; the list says so rather than showing a blank line.
                "content_status": r.content_status,
                "seen": not rollup.get(group_of(r), (not r.seen, r.flagged))[0],
                "flagged": rollup.get(group_of(r), (not r.seen, r.flagged))[1],
                "answered": r.answered,
                "account_id": r.account_id, "account_color": r.color,
                "mailbox_id": r.mailbox_id, "mailbox_role": r.role,
                "thread_count": counts.get(i, 1),
                # When this conversation is due back, for the one that is waiting
                # on a reminder. Null for everything else, which is nearly every
                # row — the list draws a small clock on the ones that have it.
                "remind_at": remind[i],
                "members": members[i],
            }
            for i, r in enumerate(rows)
        ],
    }


def _member(row) -> dict:
    """One account's share of a list row: its message, and the folder it is in."""
    return {"account_id": row.account_id, "thread_id": row.thread_id, "id": row.id,
            "mailbox_id": row.mailbox_id, "account_color": row.color}


def _remind_times(db: DBSession, members: list[list[dict]]) -> list[str | None]:
    """When each conversation on this page comes back, for the ones put off.

    Two queries for the whole page rather than one per row, and skipped entirely
    when nothing is waiting on a reminder — which is the ordinary state of a
    mailbox, and this must not cost it anything.

    Asked of every member of a row, and the earliest answer wins. A row that
    stands for one conversation in two accounts comes back as soon as either
    account's part of it does, and that is the time the clock beside it has to
    show.
    """
    threaded = {(m["account_id"], m["thread_id"]) for ms in members for m in ms if m["thread_id"]}
    loose = {m["id"] for ms in members for m in ms if not m["thread_id"]}
    by_thread = reminders_core.pending_by_thread(db, threaded)
    by_message = reminders_core.pending_by_message(db, loose)
    out: list[str | None] = []
    for ms in members:
        whens = [by_thread.get((m["account_id"], m["thread_id"])) if m["thread_id"]
                 else by_message.get(m["id"]) for m in ms]
        whens = [w for w in whens if w is not None]
        out.append(min(whens).isoformat() if whens else None)
    return out


def _recipients(db: DBSession, message_pk: int) -> dict[str, list[dict]]:
    rows = db.execute(
        select(Recipient.kind, Recipient.name, Recipient.address)
        .where(Recipient.message_pk == message_pk)
    ).all()
    out: dict[str, list[dict]] = {"to": [], "cc": [], "bcc": [], "reply_to": [], "from": []}
    for kind, name, addr in rows:
        out.setdefault(kind, []).append({"name": name, "address": addr})
    return out


def content_window_months(db: DBSession) -> int:
    """The agent's content window, as it last published it. 0 = keep everything.

    On a split deployment the app has no copy of meerail.toml — the two share
    nothing but the database — so the agent writes the number there each pass
    and this reads it back. Used only to explain a missing body, so an unset or
    unparseable value is not an error: the reader just says less.
    """
    row = db.get(Setting, ingest.CONTENT_WINDOW_KEY)
    try:
        return max(0, int(row.value)) if row else 0
    except (TypeError, ValueError):
        return 0


def _placement_state(db: DBSession, msg: Message) -> dict:
    """Where a message is filed and the flags it carries there.

    Split out of _message_detail because the reader asks the same of every
    copy of a mail delivered to several accounts (get_thread's ``twins``), and
    a copy needs its folders and flags without the body and attachments that
    the card already shows once.
    """
    # Any *live* location's flags (a message may be in several folders; report
    # the union). Deleted placements are excluded for the same reason the list
    # excludes them: they keep the flags they had when the user deleted them, so
    # a mail read in Trash and emptied out of it would otherwise go on marking
    # the copy still sitting in the inbox as read — and one flagged there would
    # keep a star on a conversation nothing is flagging any more. The row
    # outlives the keypress by an agent pass (see _not_deleted).
    locs = db.execute(
        select(MessageLocation).where(MessageLocation.message_pk == msg.id,
                                      MessageLocation.deleted.is_(False))
    ).scalars().all()
    return {
        "seen": any(l.seen for l in locs), "flagged": any(l.flagged for l in locs),
        "answered": any(l.answered for l in locs),
        # Named as well as identified: the reader draws the folder beside the
        # date, and the browser's copy of the folder tree can be a sync pass
        # behind — a message filed into a folder made a minute ago must still
        # say where it is.
        "locations": folders_of(
            (mb.id, mb.role, mb.display_name, l.parked)
            for l, mb in ((l, db.get(Mailbox, l.mailbox_id)) for l in locs)
        ),
    }


def _message_detail(db: DBSession, msg: Message, load_remote: bool,
                    window_months: int | None = None) -> dict:
    safe_html, blocked = sanitize_html(msg.body_html, msg.id, load_remote) if msg.body_html else ("", 0)
    # Columns, not entities: selecting Attachment would load `content` — the whole
    # payload — for every attachment just to render a filename chip, and a thread
    # view pays that per message. `thumb IS NOT NULL` is likewise tested in SQL so
    # the preview bytes stay in the database until something actually asks for them.
    atts = db.execute(
        select(
            Attachment.id, Attachment.filename, Attachment.content_type,
            Attachment.size_bytes, Attachment.is_inline,
            Attachment.thumb.is_not(None).label("has_thumb"),
            # Pruning empties the payload but keeps the row, so the reader can
            # still name what was attached — as a chip it will not offer to open.
            Attachment.content.is_not(None).label("stored"),
            # Whether Tika got words out of it. A length test rather than a NULL
            # test: an extraction that ran and found nothing (a scan with no OCR
            # behind it, a spreadsheet of numbers) stores an empty string, and
            # offering to explain that is offering a button that can only say
            # "there is nothing here". Costs a length(), not the text itself —
            # the column is deliberately absent from this select.
            (func.coalesce(func.length(Attachment.extracted_text), 0) > 0).label("has_text"),
        )
        .where(Attachment.message_pk == msg.id, Attachment.is_inline.is_(False))
        .order_by(Attachment.id)
    ).all()
    # Whether "view source" has anything to show. Asked as a predicate rather
    # than by touching msg.raw_mime, which is deferred precisely so a thread
    # view never drags every message's original bytes across the wire — the
    # test costs a boolean, reading the attribute would cost the blob.
    has_source = bool(db.execute(
        select(Message.raw_mime.is_not(None)).where(Message.id == msg.id)
    ).scalar())
    return {
        "id": msg.id, "account_id": msg.account_id, "thread_id": msg.thread_id,
        "message_id": msg.message_id, "subject": msg.subject or "(no subject)",
        "from_name": msg.from_name, "from_addr": msg.from_addr,
        "date": msg.date_sent.isoformat() if msg.date_sent else None,
        "recipients": _recipients(db, msg.id),
        "body_html": safe_html, "body_text": msg.body_text,
        # What the reader's "show plain text" switch displays. Most HTML mail
        # here carries no text/plain part at all, and that mail is exactly the
        # mail whose layout misbehaves — so fall back to flattening the HTML
        # rather than withholding the escape hatch. Flattened from the sanitized
        # copy, so nothing nh3 stripped can come back as text. Link addresses
        # are spelled out: dropping them would leave "click here" pointing at
        # nothing, and reading where a link goes is half of why you switch.
        "body_plain": msg.body_text or html_to_text(safe_html, links=True),
        "remote_blocked": blocked, "images_loaded": load_remote,
        "has_attachments": msg.has_attachments,
        # full | skipped | pruned, plus the window that explains the last two.
        # Looked up only when there is something to explain — normal mail must
        # not pay a settings read per message, and a thread is many messages.
        "content_status": msg.content_status,
        "content_window_months": (
            (content_window_months(db) if window_months is None else window_months)
            if msg.content_status != "full" else 0
        ),
        "has_source": has_source,
        # seen, flagged, answered and locations.
        **_placement_state(db, msg),
        "attachments": [
            {"id": a.id, "filename": a.filename, "content_type": a.content_type,
             "size": a.size_bytes, "is_inline": a.is_inline, "stored": a.stored,
             "has_thumb": a.has_thumb, "viewable": _inline_safe(a.content_type),
             # Is there anything for a model to read? Extracted text, or the
             # picture itself. Decided here because only the server can see the
             # first of the two — see the AI router's "explain this attachment".
             "has_text": a.has_text,
             # An iCalendar file, which the reader offers to put into meercal
             # (app/routers/calendar.py). Decided by the same rule the parser
             # used to store it and the import route uses to accept it, so the
             # button and the route cannot disagree about what qualifies.
             "calendar": is_calendar_part(a.content_type, a.filename)}
            for a in atts
        ],
    }


@router.get("/messages/{message_id}")
def get_message(message_id: int, images: bool = False, db: DBSession = Depends(get_db)):
    return _message_detail(db, _readable(db, message_id), load_remote=images)


@router.get("/messages/{message_id}/source")
def message_source(message_id: int, db: DBSession = Depends(get_db)):
    """The message exactly as it arrived — headers, MIME structure and all.

    Served as text/plain so a new tab shows it rather than saving it, and under
    the same sandbox/nosniff headers as an attachment: these are the sender's
    bytes, and a browser that decided for itself that they were HTML would be
    running the sender's markup on our own origin.

    Two things stop this having a body: `store_raw_mime` off when the mail was
    ingested, and pruning as it aged out of the content window. Neither is an
    error worth a different code than "there is nothing here to show" — the
    reader already draws the button that leads here disabled in both cases.
    """
    _readable(db, message_id)      # deleted mail has no source to show either
    row = db.execute(
        select(Message.id, Message.raw_mime).where(Message.id == message_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if row.raw_mime is None:
        raise HTTPException(status_code=404, detail="Original message bytes are not stored")
    return Response(
        content=row.raw_mime,
        media_type="text/plain; charset=utf-8",
        headers={
            # Named so that saving the tab lands a file a mail client can open.
            "Content-Disposition": f'inline; filename="message-{message_id}.eml"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )


# --- Search-term hits inside attachments ---------------------------------
# A search matches Message.search_text, which folds in extracted attachment
# text. So a result can be one where the term appears in a PDF and nowhere in
# the mail itself — highlighting only the body would leave the reader staring
# at a message with no visible reason to be there. The client can mark up
# subject and body on its own; extracted text it has never seen, so the hits
# are found here and shipped as pre-split context windows.

_CONTEXT_CHARS = 90     # either side of the hit
_MAX_HITS = 3           # per attachment — a preview, not a concordance


def match_patterns(q: str, mode: str) -> list[re.Pattern]:
    """The client-visible mirror of the search WHERE clause.

    keyword -> each term the search ANDed together, case-insensitively, as a
    literal substring — a quoted run stays one term, so `"was sent"` marks the
    phrase it matched on. regex -> the pattern itself. Postgres POSIX and Python
    `re` part ways on exotic syntax; a pattern that only one of them accepts
    costs a missing highlight, never a wrong result.

    Filter tokens narrowed the result set rather than matching text in it, so
    they are dropped here — otherwise `:unread` would come back highlighted as
    though it were something the user had searched for.
    """
    q = searchquery.parse(q).text
    if not q:
        return []
    try:
        if mode == "regex":
            return [re.compile(q, re.IGNORECASE)]
        return [re.compile(re.escape(t), re.IGNORECASE) for t in searchquery.keyword_terms(q)]
    except re.error:
        return []


def _contexts(text: str, pats: list[re.Pattern]) -> list[dict]:
    """Up to _MAX_HITS windows around matches, split so the client can wrap the
    matched span without us handing it HTML to trust."""
    spans: list[tuple[int, int]] = []
    for p in pats:
        for m in p.finditer(text):
            if m.end() > m.start():
                spans.append((m.start(), m.end()))
            if len(spans) >= _MAX_HITS * len(pats):
                break
    spans.sort()
    out, last_end = [], -1
    for start, end in spans:
        if start < last_end:          # overlapping windows read as one blur
            continue
        out.append({
            "before": text[max(0, start - _CONTEXT_CHARS):start].lstrip(),
            "match": text[start:end],
            "after": text[end:end + _CONTEXT_CHARS].rstrip(),
        })
        last_end = end + _CONTEXT_CHARS
        if len(out) >= _MAX_HITS:
            break
    return out


def _annotate_attachment_hits(db: DBSession, msgs, details: list[dict], q: str,
                              mode: str, pats: list[re.Pattern]) -> None:
    # The SQL filter is written the way search.py writes it — ILIKE for keyword,
    # POSIX for regex, quoted runs kept whole — rather than by feeding it the
    # Python patterns, whose backslash escaping Postgres reads differently. The
    # filter tokens are already gone: `pats` comes from the same stripped text,
    # and searching attachments for the literal ":unread" would find nothing.
    if mode == "regex":
        where = Attachment.extracted_text.op("~*")(q)
    else:
        where = or_(*[
            Attachment.extracted_text.ilike(f"%{searchquery.like_escape(t)}%", escape="\\")
            for t in searchquery.keyword_terms(q)
        ])
    # Filtered in SQL first: extracted text runs to whole PDFs, and a thread of
    # them should not cross the wire so Python can throw most of it away.
    rows = db.execute(
        select(Attachment.id, Attachment.extracted_text).where(
            Attachment.message_pk.in_([m.id for m in msgs]),
            Attachment.extracted_text.is_not(None),
            where,
        )
    ).all()
    texts = {aid: t for aid, t in rows}
    if not texts:
        return
    for d in details:
        for a in d["attachments"]:
            hits = _contexts(texts.get(a["id"]) or "", pats)
            if hits:
                a["match_contexts"] = hits


# `:path` because a thread_id is a Message-ID, and plenty of senders build those
# out of a path — GitHub's are `owner/repo/pull/21/c123@github.com`. The client
# percent-encodes the slashes, but the ASGI server unquotes the whole path before
# routing, so a plain `{thread_id}` never matches and every such thread 404s.
@router.get("/threads/{thread_id:path}")
def get_thread(
    thread_id: str,
    # Repeated, one per account the conversation is to be read in: a merged
    # list row stands for the same thread in several (app/spanning.py). One
    # value is what every older caller sends, and still works.
    account_id: list[int] = Query(...),
    images: bool = False,
    q: str = "",
    mode: str = Query("keyword", pattern="^(keyword|regex)$"),
    db: DBSession = Depends(get_db),
):
    """One conversation, in every account named, as the reader shows it.

    The messages of the thread in all of ``account_id``, oldest first, with a
    mail delivered to two of those accounts shown once. That card is the copy
    in the account named first (the one the list row stood for) and lists the
    other copies under ``twins``, with their own folders, so the reader can
    pass every keypress on to them and draw where each one is filed. It reads
    as unread until every copy is read, so that marking it read on open reaches
    the copy that is not, and as flagged or answered when any copy is.

    ``account_ids`` says which of the accounts named actually hold some of the
    thread, in the order named, and ``members`` names the newest message of
    each. The reader sends those along when it parks or reminds the
    conversation, so the keypress reaches every account's part of it.
    """
    accounts = account_ids_param(account_id)
    msgs = db.execute(
        select(Message)
        .where(Message.account_id.in_(accounts), Message.thread_id == thread_id, _not_deleted())
        # The id after the date only so that the order is total. Twins share
        # their date, and the card a reader draws must not depend on which of
        # them the planner happened to return first.
        .order_by(Message.date_sent.asc().nulls_first(), Message.id)
    ).scalars().all()
    if not msgs:
        raise HTTPException(status_code=404, detail="Thread not found")
    present = [a for a in accounts if any(m.account_id == a for m in msgs)]
    newest: dict[int, Message] = {}
    for m in msgs:
        newest[m.account_id] = m           # oldest first, so the last one stays
    entries = spanning.collapse_twins(msgs, present)
    shown = [rep for rep, _ in entries]

    # One settings read for the whole thread, not one per message in it.
    window = content_window_months(db) if any(m.content_status != "full" for m in shown) else 0
    details = []
    for rep, twins in entries:
        d = _message_detail(db, rep, load_remote=images, window_months=window)
        states = [(t, _placement_state(db, t)) for t in twins]
        d["twins"] = [{"id": t.id, "account_id": t.account_id, "locations": s["locations"]}
                      for t, s in states]
        if states:
            d["seen"] = d["seen"] and all(s["seen"] for _, s in states)
            d["flagged"] = d["flagged"] or any(s["flagged"] for _, s in states)
            d["answered"] = d["answered"] or any(s["answered"] for _, s in states)
        details.append(d)
    pats = match_patterns(q, mode)
    if pats:
        try:
            _annotate_attachment_hits(db, shown, details, searchquery.parse(q).text, mode, pats)
        except DBAPIError:
            # A pattern Postgres rejects costs the attachment highlights, not
            # the thread — the reader still opens.
            db.rollback()
    # The first account whose part of the conversation is waiting on one, in
    # the order the accounts were named. Asked lazily, so the ordinary case of
    # one account costs the one lookup it always did.
    reminder = next((r for r in (reminders_core.pending_for(db, newest[a]) for a in present)
                     if r is not None), None)
    return {
        "thread_id": thread_id,
        "subject": msgs[-1].subject or "(no subject)",
        "messages": details,
        "account_ids": present,
        "members": [{"account_id": a, "id": newest[a].id} for a in present],
        # Set when this conversation is waiting on a reminder, so the reader can
        # say when it is coming back and offer to take that back — the button
        # that put it there is not on screen any more, because the conversation
        # left the folder it was pressed in.
        "reminder": reminders_core.describe(reminder) if reminder else None,
    }


# Types safe to hand the browser with Content-Disposition: inline. Anything
# scriptable in a same-origin document (text/html, image/svg+xml) is deliberately
# absent: an attachment is attacker-controlled, and rendering one inline on our
# own origin would be stored XSS against the session.
_INLINE_SAFE = {
    "application/pdf",
    "image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp",
}


def _inline_safe(content_type: str) -> bool:
    return (content_type or "").split(";")[0].strip().lower() in _INLINE_SAFE


def _disposition(dispo: str, filename: str) -> str:
    """A Content-Disposition line for a name mail may have written in any script.

    Header values go out latin-1; attachment names do not. A single narrow
    no-break space in an invoice name — the thing a German bank puts between
    the amount and the currency — was a 500 where a download belonged. So the
    quoted `filename` keeps only what survives the wire and stands as the
    fallback, while RFC 5987's `filename*` carries the real name for every
    browser of the last decade. Quotes, backslashes and control characters go
    with the rest: the name is sender-controlled, and a newline in a header is
    someone else's response.
    """
    ascii_name = re.sub(r'[^\x20-\x7e]+', "_", filename).replace('"', "").replace("\\", "")
    return (f'{dispo}; filename="{ascii_name.strip() or "attachment"}"; '
            f"filename*=UTF-8''{quote(filename, safe='')}")


@router.get("/attachments/{attachment_id}")
def download_attachment(
    attachment_id: int, inline: bool = False, db: DBSession = Depends(get_db)
):
    att = db.get(Attachment, attachment_id)
    if att is None or att.content is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    _readable(db, att.message_pk)   # nor do its files outlive the message
    filename = att.filename or "attachment"
    dispo = "inline" if inline and _inline_safe(att.content_type) else "attachment"
    # The stored content type came off the wire too, so it gets the same
    # treatment: anything but a plain ASCII one is not worth a broken response.
    media = att.content_type or "application/octet-stream"
    if not media.isascii() or not media.isprintable():
        media = "application/octet-stream"
    # Belt and braces around the allowlist: never let the browser sniff its way
    # to a different type, and neuter scripts if one slips past.
    csp = "sandbox; default-src 'none'"
    headers = {
        "Content-Disposition": _disposition(dispo, filename),
        "X-Content-Type-Options": "nosniff",
    }
    if dispo == "inline":
        # A PDF is drawn by the browser's own viewer, and WebKit and Chromium
        # both refuse to draw one into a sandboxed document: the page simply
        # comes up empty. Only a PDF loses the sandbox, and only inline, which
        # the allowlist has already confined to exactly application/pdf.
        if media.split(";")[0].strip().lower() == "application/pdf":
            csp = "default-src 'none'"
        # The phone's attachment viewer (app.mobile.js) shows this in a frame of
        # the app's own. The middleware's X-Frame-Options: DENY would blank that
        # frame too, so an inline attachment may be framed by this origin and
        # still by no other.
        csp += "; frame-ancestors 'self'"
        headers["X-Frame-Options"] = "SAMEORIGIN"
    headers["Content-Security-Policy"] = csp
    return Response(content=att.content, media_type=media, headers=headers)


# --- "Save them all" -----------------------------------------------------

def _zip_name(name: str, taken: set[str]) -> str:
    """A filename safe to write into a zip, unique within it.

    Mail names attachments, and a sender can name one `../../.ssh/authorized_keys`
    — every path-ish character goes, because the archive is unpacked on someone
    else's machine by a tool we do not control. Collisions are numbered rather
    than silently overwritten: two `invoice.pdf` in one mail is ordinary, and an
    archive that quietly held one of them would be the worst of the options.
    """
    name = re.sub(r"[/\\]", "_", name or "").strip().lstrip(".") or "attachment"
    name = "".join(c for c in name if c.isprintable())[:120] or "attachment"
    if name.lower() not in taken:
        taken.add(name.lower())
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    for n in range(2, 10_000):
        cand = f"{stem} ({n}){dot}{ext}"
        if cand.lower() not in taken:
            taken.add(cand.lower())
            return cand
    raise HTTPException(status_code=409, detail="Too many attachments with the same name")


@router.get("/messages/{message_id}/attachments.zip")
def download_all_attachments(message_id: int, db: DBSession = Depends(get_db)):
    """Every stored attachment on one message, as a single archive.

    The same set the reader draws chips for — non-inline, so a signature logo
    does not end up in the folder someone unpacked three invoices into — minus
    the ones pruning has emptied, which have a name in the list but no bytes to
    put in a file. Deflate at level 1: PDFs and JPEGs are already compressed and
    would only cost CPU to re-squeeze, while the text ones still gain something.
    """
    msg = _readable(db, message_id)
    atts = db.execute(
        select(Attachment.filename, Attachment.content)
        .where(Attachment.message_pk == message_id, Attachment.is_inline.is_(False),
               Attachment.content.is_not(None))
        .order_by(Attachment.id)
    ).all()
    if not atts:
        raise HTTPException(status_code=404, detail="No stored attachments on this message")

    buf = io.BytesIO()
    taken: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for a in atts:
            zf.writestr(_zip_name(a.filename, taken), a.content)

    # The archive is named after the mail so a Downloads folder full of them can
    # be told apart, but only out of characters that survive a latin-1 header —
    # the id alone is the fallback, never a broken response.
    stem = re.sub(r"[^A-Za-z0-9 ._-]+", " ", msg.subject or "").strip()[:60].strip(" .")
    stem = re.sub(r"\s+", " ", stem)
    filename = f"{stem} attachments.zip" if stem else f"attachments-{message_id}.zip"
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )


@router.get("/attachments/{attachment_id}/thumb")
def attachment_thumb(attachment_id: int, db: DBSession = Depends(get_db)):
    att = db.get(Attachment, attachment_id)
    if att is None or att.thumb is None:
        raise HTTPException(status_code=404, detail="No preview")
    _readable(db, att.message_pk)
    return Response(
        content=att.thumb,
        media_type="image/webp",
        headers={
            # Attachment bytes never change, so the preview never does either.
            "Cache-Control": "private, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/messages/{message_id}/cid/{content_id}")
def inline_cid(message_id: int, content_id: str, db: DBSession = Depends(get_db)):
    """One inline part of a message, for a `cid:` the sanitizer rewrote.

    Gated like every other way of reading a message, because it is one: a
    `cid:` is a handle on the bytes of a mail, and mail the user has deleted has
    no bytes to hand out (see _readable).

    Served under the same headers as a downloaded attachment, for the same
    reason: these are the sender's bytes with the sender's Content-Type on them,
    and a browser that sniffed its way to text/html here would be running a
    stranger's markup on our own origin — inside the reader's iframe, where the
    sanitizer's work would then be beside the point.
    """
    _readable(db, message_id)
    att = db.execute(
        select(Attachment).where(
            Attachment.message_pk == message_id, Attachment.content_id == content_id
        )
    ).scalars().first()
    if att is None or att.content is None:
        raise HTTPException(status_code=404, detail="Inline image not found")
    return Response(
        content=att.content,
        media_type=att.content_type or "application/octet-stream",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )
