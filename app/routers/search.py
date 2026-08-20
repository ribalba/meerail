"""Regex + keyword search over the whole corpus (subject + participants + body +
extracted attachment text).

- mode=regex   -> Postgres ~* (real POSIX regex), over the pg_trgm GIN index on
  search_text. The date-window filter bounds patterns that can't use that index
  (no literal >=3 chars).
- mode=keyword -> AND of substrings; "quoted runs" stay one term so spaces
  inside them are matched literally. Answered from the GIN index on search_tsv,
  which holds every suffix of every word in the message: a substring of a word
  is a prefix of one of its suffixes, so this is the same answer ILIKE gave
  without reading the text to get it. A term that spans the separators the
  index splits on ("50% off", an address) uses the tsquery as a prefilter and
  rechecks with ILIKE, which by then costs a handful of rows. See
  core/database.py for the index and app/searchquery.py for the split.

Both modes are case-insensitive: mail is not typed consistently enough for
case to be a useful filter, and a miss looks identical to "no such mail".

`:unread`, `:read`, `:has-attachment`, `:no-trash`, `:from <pattern>`,
`:to <pattern>` and `:similar <fingerprint|message id>`
are lifted out of the query first (see app.searchquery) and applied as SQL
filters. They narrow rather than search, so a query that is nothing but
filters is still a search — `:unread :has-attachment` is a perfectly good
question to ask.
"""

from __future__ import annotations

import re
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, cast, exists, func, literal, or_, select, text, tuple_
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session as DBSession

from core import bodysig
from core.config import get_settings
from core.database import get_db
from .messages import _not_deleted
from .. import searchquery
from ..deps import require_ui_auth
from core.models import Account, Mailbox, Message, MessageLocation, Recipient, utcnow

settings = get_settings()

router = APIRouter(prefix="/api", tags=["search"], dependencies=[Depends(require_ui_auth)])


def _check_regex(pattern: str, where: str) -> str:
    """Reject a pattern the engine can't compile, naming the filter it came from.

    Python's `re` is not Postgres' POSIX dialect, so this catches the ordinary
    typos (an unclosed group) and leaves the exotic differences to the DBAPIError
    handler further down.
    """
    try:
        re.compile(pattern)
    except re.error as e:
        raise HTTPException(status_code=400, detail=f"Invalid regex in {where}: {e}") from e
    return pattern


# Postgres' code for "cancelled because it ran past statement_timeout", which is
# the one DBAPIError here that is not the user's pattern being wrong.
_QUERY_CANCELED = "57014"

# A ceiling on a single search. The browser drops a search the next keystroke
# has superseded and aborts its fetch, but this endpoint is synchronous and
# never learns that the client hung up, so without a limit an abandoned query
# goes on holding a pooled connection until it finishes. Deliberately generous:
# keyword search is milliseconds, so anything near this is a regex doing
# something pathological, and cutting it off is the only way it ends.
SEARCH_TIMEOUT_MS = 20_000

# How many matching messages a page is allowed to look at before it stops
# counting conversations. Deliberately far more than the sixty a page shows,
# because carrying ids is nearly free next to finding the rows in the first
# place: measured on a 113k-message mailbox, raising this from 15k to 80k moved
# an ordinary search by a millisecond or two and the widest one by nothing at
# all. Every match that fits inside the budget is one the "N results" line can
# count exactly rather than rounding up to "N+", and at this size only a term
# matching most of the mailbox falls outside it.
SCAN_BUDGET = 80_000

# The ceiling on the widening pass below. Past here the answer is "a lot", and
# spending longer to say how many of a lot helps nobody.
MAX_SCAN = 400_000


def _similar_clause(db: DBSession, value: str):
    """WHERE for `:similar` — mail whose body says what this one says.

    The value is either a body fingerprint token (which is what the Cleanup
    panel puts in the box) or a message id (which is what "find more like this"
    means from anywhere else). A message id is resolved to that message's own
    fingerprint first, so both spellings end up asking the same question.

    "The same" means sharing a band of the banded MinHash in core/bodysig.py —
    the same test the Cleanup panel groups on, and the reason a hundred property
    alerts with a hundred different subjects can be pulled up as one search.

    A bare token is matched in *any* band position rather than the one it came
    from, because the token arrives without its position attached. The looseness
    is theoretical: the bands are 32-bit, so a token colliding with a different
    band of an unrelated message is a one-in-four-billion event, and the query
    would have to be pointed at that message for it to matter.

    Returns None when the filter cannot match anything, which the caller turns
    into an empty result rather than into no filter at all.
    """
    sig = value
    if value.isdigit():
        # A message id, which is the spelling the Cleanup panel uses and the one
        # "more like this" means anywhere else. Resolved without regard to where
        # the message sits: a group that has just been trashed is exactly the
        # one somebody re-runs the query for.
        sig = db.scalar(select(Message.body_sig).where(Message.id == int(value))) or ""
    return bodysig.shares_band(Message.body_sig, sig)


def _bound_runtime(db: DBSession) -> None:
    if db.get_bind().dialect.name == "postgresql":
        # LOCAL, so it lasts exactly as long as this request's transaction and
        # cannot leak onto the next thing to borrow the connection.
        db.execute(text(f"SET LOCAL statement_timeout = {SEARCH_TIMEOUT_MS}"))


# Whether every message has its search_tsv yet. False only between the upgrade
# that adds the column and the moment core.searchindex finishes filling it in,
# and during that window keyword search stays on the ILIKE path — slower, but a
# search that quietly skipped the mail not yet indexed would be worse than a
# slow one. Latched, because a row that has the value can never lose it again:
# the trigger fills it on the way in, so once there are none missing there never
# will be. See ix_messages_search_tsv_missing for why asking is one index probe.
_index_ready = False


def _index_built(db: DBSession) -> bool:
    global _index_ready
    if not _index_ready:
        _index_ready = db.scalar(
            select(Message.id).where(Message.search_tsv.is_(None)).limit(1)
        ) is None
    return _index_ready


@router.get("/search")
def search(
    db: DBSession = Depends(get_db),
    q: str = "",
    mode: str = Query("keyword", pattern="^(keyword|regex)$"),
    mailbox_id: int | None = None,
    account_id: int | None = None,
    # A window in years, and one no wider than the mail can be: `years` is turned
    # into a date, and a big enough number overflows the arithmetic long before
    # it means anything — a 500 for a query that only asked for everything, which
    # is what 0 already says. See list_messages for the bounds on the other two.
    #
    # Absent is not the same as 0. 0 is "search everything", asked for on
    # purpose; leaving it out is not asking, and gets `server.default_search_years`
    # — a year, out of the box. A search costs what it has to look at, and the
    # caller who names no window is the one most likely not to have thought about
    # what that means on twenty years of mail.
    years: int | None = Query(None, ge=0, le=200),
    # The same ceiling list_messages uses, which is what the comment above means
    # by "the other two". A wide limit costs no more scanning than a narrow one
    # — the budget below is a floor of 80k either way — so the ceiling is here
    # to bound the rows fetched and returned, exactly as it is there. The page
    # the browser asks for is far smaller; this is the size of a *re-fetch* of a
    # result list someone has paged through, which is why 200 was not enough.
    limit: int = Query(60, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    q = q.strip()
    if not q:
        return {"query": q, "mode": mode, "total": 0, "total_capped": False, "rows": []}
    _bound_runtime(db)

    if years is None:
        years = settings.default_search_years

    parsed = searchquery.parse(q)
    empty = {"query": q, "mode": mode, "total": 0, "total_capped": False, "rows": []}

    clauses = []
    if parsed.text:
        if mode == "regex":
            _check_regex(parsed.text, "the query")
            clauses.append(Message.search_text.op("~*")(parsed.text))
        else:
            terms = searchquery.keyword_terms(parsed.text)
            if not terms:
                return empty
            indexed = _index_built(db)
            for t in terms:
                tsq, exact = searchquery.tsquery(t)
                if indexed and tsq:
                    # Cast, not to_tsquery: searchquery.tsquery already hands
                    # back tsquery syntax, and running the text-search parser
                    # over it is what used to lose hex ids and order numbers.
                    clauses.append(
                        Message.search_tsv.op("@@")(cast(literal(tsq), TSQUERY))
                    )
                    if exact:
                        # The index answers this term outright; going to the
                        # text as well would undo the point of having it.
                        continue
                # Either there is no index to ask yet, or the term spans the
                # separators it splits on and the tsquery above is only a
                # prefilter. A term is a literal, so % and _ in it are
                # characters the user typed ("50% off"), not LIKE wildcards.
                clauses.append(
                    Message.search_text.ilike(f"%{searchquery.like_escape(t)}%", escape="\\")
                )
    elif not parsed.filtered:
        # The whole query was a filter still being typed (`:from `).
        return empty

    if parsed.unread is not None:
        # A conversation counts as unread while any copy of the message is
        # unread anywhere it still lives — the same rollup the folder list and
        # the sidebar badges use, so the three never disagree.
        unseen = exists(
            select(MessageLocation.id).where(
                MessageLocation.message_pk == Message.id,
                MessageLocation.deleted.is_(False),
                MessageLocation.seen.is_(False),
            )
        )
        clauses.append(unseen if parsed.unread else ~unseen)
    if parsed.has_attachments:
        clauses.append(Message.has_attachments.is_(True))
    if parsed.no_trash:
        # Still filed somewhere that is not Trash. Not "has no placement in
        # Trash": on a label server a message can sit in Trash and in \All at
        # once, and the copy that matters is the one you can still get to.
        clauses.append(
            exists(
                select(MessageLocation.id)
                .join(Mailbox, Mailbox.id == MessageLocation.mailbox_id)
                .where(
                    MessageLocation.message_pk == Message.id,
                    MessageLocation.deleted.is_(False),
                    Mailbox.role != "trash",
                )
            )
        )
    if parsed.similar:
        clause = _similar_clause(db, parsed.similar)
        if clause is None:
            # A fingerprint nothing carries, or a message with no body to
            # fingerprint. An empty result is the honest answer — the alternative
            # is dropping the filter, which silently widens the search to every
            # message the rest of the query allows.
            return empty
        clauses.append(clause)
    if parsed.from_pat:
        # Address or display name: "who sent this" is a name to the user, and
        # the address is what they reach for when the name is ambiguous.
        pat = _check_regex(parsed.from_pat, ":from")
        clauses.append(or_(Message.from_addr.op("~*")(pat), Message.from_name.op("~*")(pat)))
    if parsed.to_pat:
        pat = _check_regex(parsed.to_pat, ":to")
        clauses.append(
            exists(
                select(Recipient.id).where(
                    Recipient.message_pk == Message.id,
                    Recipient.kind.in_(("to", "cc", "bcc")),
                    or_(Recipient.address.op("~*")(pat), Recipient.name.op("~*")(pat)),
                )
            )
        )

    match = and_(*clauses)

    # Three columns, not the ten the page needs: this reads tens of thousands of
    # rows to find sixty conversations, and carrying the subject and the snippet
    # of every one of them is most of what that would cost. The page's own
    # columns are fetched below, for the sixty ids that survive.
    base = (
        select(Message.id, Message.thread_id, Message.account_id)
        # Only mail that is still filed somewhere. A search across the whole
        # account is the one read path with no folder in it, so without this it
        # was the way a message the user had emptied out of Trash went on
        # turning up — listed nowhere, and first hit for its own subject.
        .where(match, _not_deleted())
    )
    if years > 0:
        base = base.where(Message.date_sent >= utcnow() - timedelta(days=365 * years))
    if mailbox_id is not None:
        base = base.where(
            exists(
                select(MessageLocation.id).where(
                    MessageLocation.message_pk == Message.id,
                    MessageLocation.mailbox_id == mailbox_id,
                    MessageLocation.deleted.is_(False),
                )
            )
        )
    elif account_id is not None:
        base = base.where(Message.account_id == account_id)

    # Newest matching message first, and one row per conversation — a term that
    # appears in a mail and again in every reply quoting it would otherwise fill
    # the list with the same thread.
    #
    # The conversations are picked out here rather than by a SQL DISTINCT ON,
    # which is what this used to be. DISTINCT ON cannot hand back a single row
    # until it has sorted *every* match, so a term like "de" — which is in 107k
    # of a 113k-message mailbox, and which nobody meant to search for because it
    # is a word half typed — spent a second arranging 67k conversations in order
    # to show sixty of them. Scanning in date order and keeping the first
    # message of each conversation is the same answer for a fraction of the
    # work: measured there, 1143ms to 331ms on that term, and 50ms to 24ms on an
    # ordinary one.
    #
    # The same answer literally, not approximately: under this ORDER BY the
    # first message seen for a conversation is its newest matching one, which is
    # exactly the row DISTINCT ON was choosing, and they come out in the order
    # the page wants. Only the count changes — see `total_capped`.
    base = base.order_by(Message.date_sent.desc().nulls_last(), Message.id.desc())

    try:
        scan = min(MAX_SCAN, max(SCAN_BUDGET, (offset + limit) * 40))
        while True:
            found = db.execute(base.limit(scan)).all()
            threads: dict[tuple[int, str], int] = {}
            for mid, thread_id, acct in found:
                # Mail that never got threaded stands alone rather than
                # collapsing into one "no thread" pile — same key the folder
                # list builds.
                threads.setdefault((acct, thread_id or f"msg:{mid}"), mid)
            # Fewer messages back than we asked for means there are no more:
            # every match was seen, so the conversation count is exact.
            exhausted = len(found) < scan
            if exhausted or len(threads) > offset + limit or scan >= MAX_SCAN:
                break
            # A page that came up short against a saturated scan means the
            # matches are piled into very few conversations — a mailing list,
            # typically. Rare enough to be worth a second, wider pass rather
            # than a budget large enough to cover it every time.
            scan = min(scan * 8, MAX_SCAN)

        page_ids = list(threads.values())[offset:offset + limit]
        by_id = {}
        if page_ids:
            by_id = {
                r.id: r for r in db.execute(
                    select(
                        Message.id, Message.thread_id, Message.subject, Message.from_name,
                        Message.from_addr, Message.date_sent, Message.snippet,
                        Message.has_attachments, Message.account_id, Account.color,
                    )
                    .join(Account, Account.id == Message.account_id)
                    .where(Message.id.in_(page_ids))
                ).all()
            }
    except DBAPIError as e:
        db.rollback()
        if getattr(e.orig, "sqlstate", None) == _QUERY_CANCELED:
            # Said as what it is. "The engine rejected that pattern" sends
            # someone off to fix a query that is perfectly valid and merely
            # slow, which is the opposite of the advice they need.
            raise HTTPException(
                status_code=400,
                detail="Search took too long — narrow the time window or the pattern.") from e
        raise HTTPException(
            status_code=400,
            detail="Search failed — the engine rejected that pattern.") from e

    # Back into the order the scan found them in — `IN` returns rows in whatever
    # order suits the plan, and the page is sorted by date, which is the one
    # thing about a result list a reader relies on.
    rows = [by_id[i] for i in page_ids if i in by_id]
    # Exact whenever the scan reached the end of the matches, which is every
    # search but the ones that match a large fraction of the mailbox. Where it
    # is not, it is a floor rather than a guess, and the UI says so with a "+".
    total = len(threads)
    total_capped = not exhausted

    ids = [r.id for r in rows]
    flags: dict[int, tuple[bool, bool]] = {}
    if ids:
        for pk, seen_all, flagged_any, trashed in db.execute(
            select(MessageLocation.message_pk, func.bool_and(MessageLocation.seen),
                   func.bool_or(MessageLocation.flagged),
                   # Every live placement is in Trash, so this result *is* the
                   # deleted copy. Results come from the whole mailbox, Trash
                   # included, and without saying so a search run straight after
                   # a delete looks exactly like a delete that did nothing.
                   func.bool_and(Mailbox.role == "trash"))
            .join(Mailbox, Mailbox.id == MessageLocation.mailbox_id)
            # Live placements only. A deleted one keeps the flags it had when it
            # was deleted, so a result read in Trash and emptied out of it would
            # otherwise come back marked read while the copy the search actually
            # found sits unread in the inbox.
            .where(MessageLocation.message_pk.in_(ids), MessageLocation.deleted.is_(False))
            .group_by(MessageLocation.message_pk)
        ).all():
            flags[pk] = (bool(seen_all), bool(flagged_any), bool(trashed))

    tids = {r.thread_id for r in rows if r.thread_id}
    sizes: dict[tuple[int, str], int] = {}
    if tids:
        account_threads = {(r.account_id, r.thread_id) for r in rows if r.thread_id}
        for aid, tid, n in db.execute(
            select(Message.account_id, Message.thread_id, func.count())
            # Counts what opening the result will actually show. The thread view
            # applies _not_deleted too, so without it here a conversation whose
            # older half was deleted advertises "8 messages" and opens on 3.
            .where(tuple_(Message.account_id, Message.thread_id).in_(account_threads),
                   _not_deleted())
            .group_by(Message.account_id, Message.thread_id)
        ).all():
            sizes[(aid, tid)] = n

    return {
        "query": q, "mode": mode, "total": int(total or 0),
        "total_capped": total_capped,
        "rows": [
            {
                "id": r.id, "thread_id": r.thread_id, "subject": r.subject or "(no subject)",
                "from_name": r.from_name, "from_addr": r.from_addr,
                "date": r.date_sent.isoformat() if r.date_sent else None,
                "snippet": r.snippet, "has_attachments": r.has_attachments,
                "seen": flags.get(r.id, (True, False, False))[0],
                "flagged": flags.get(r.id, (True, False, False))[1],
                "in_trash": flags.get(r.id, (True, False, False))[2],
                "account_id": r.account_id, "account_color": r.color,
                "thread_count": sizes.get((r.account_id, r.thread_id), 1),
            }
            for r in rows
        ],
    }
