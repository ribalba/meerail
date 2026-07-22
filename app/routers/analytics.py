"""Analytics over the mail store (enabled by keeping everything in Postgres).

One endpoint, one round trip: the stats modal draws every panel from a single
`/overview` payload rather than fanning out a request per chart, because the
panels share the same window and the same base filter and would otherwise
re-derive them a dozen times over.

Three facts about the schema shape every query in here:

  * **There is no "direction" column.** Whether you sent a message is derived —
    see `_sent_pred`. Getting that wrong silently swaps every number on the
    page, so it is defined once and reused.
  * **A message can sit in several folders.** Proton Bridge exposes labels as
    folders, so one message has one `messages` row and N `message_locations`
    rows. Every folder-aware predicate is an EXISTS, never a join, or a
    three-label message counts three times.
  * **`date_sent` is nullable naive UTC.** Null means the `Date:` header was
    missing or unparseable; those rows are excluded rather than bucketed into
    an invented time. Local-time buckets need the caller's offset applied
    explicitly — see `_local`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Interval, and_, case, func, literal, or_, select
from sqlalchemy.orm import Session as DBSession

from core.database import get_db
from ..deps import require_ui_auth
from core.models import Account, Attachment, Mailbox, Message, MessageLocation, Recipient

router = APIRouter(prefix="/api/analytics", tags=["analytics"], dependencies=[Depends(require_ui_auth)])

# Window presets. None means "everything we hold".
RANGES: dict[str, int | None] = {"7d": 7, "30d": 30, "90d": 90, "1y": 365, "all": None}

# Bucket width per window, chosen so a series lands somewhere between ~12 and
# ~90 points: fine enough to show shape, coarse enough that the line is not
# noise. "all" is open-ended, so it gets the widest bucket.
GRAINS: dict[str, str] = {"7d": "day", "30d": "day", "90d": "day", "1y": "week", "all": "month"}

# A reply is the next message you send into a thread. Past this gap it is
# almost always a new conversation that inherited the thread id rather than an
# answer to anything, and letting those in drags the median out by weeks.
REPLY_WINDOW = timedelta(days=30)

# How long a message must have sat before it counts towards the response rate.
#
# Mail from yesterday that you will answer tomorrow is not a message you failed
# to answer, but a naive rate counts it as one. Excluding the tail removes that
# bias. Measured on a real 81k mailbox the correction is small — a 30-day rate
# moved 4.6% -> 4.3%, because ~94% of even a 30-day window is already mature —
# so this is a correctness guard, not a headline-changing adjustment. (The much
# larger gap between a 30-day and a 1-year rate on that mailbox survives this
# fix, and is a real property of the mail, not an artefact.)
#
# A fifth of the window, capped at a week: long enough to cover the great
# majority of real replies, short enough that a 7-day view still has something
# left to measure.
RESPONSE_MATURITY = timedelta(days=7)

# Folders whose contents are not correspondence. Drafts were never sent and
# junk is not a relationship; counting either would put phantom "sent" mail in
# the daily average and spam domains at the top of the contacts list.
EXCLUDED_ROLES = ("drafts", "junk")

# Latency histogram edges, in seconds, with the label for each bucket.
LATENCY_BUCKETS = [
    (900, "< 15 min"),
    (3600, "15–60 min"),
    (14400, "1–4 h"),
    (86400, "4–24 h"),
    (259200, "1–3 days"),
    (None, "> 3 days"),
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- Predicates ---------------------------------------------------------------
# Each takes the message entity explicitly: the reply-latency queries correlate
# a second alias of `messages` against the first, and both sides need the same
# definition of "sent" applied to a different alias.


def _has_location_in(entity, roles: tuple[str, ...], *, outside: bool = False):
    """EXISTS a location for this message in (or outside) `roles`.

    EXISTS rather than a join — see the module docstring on label fan-out.
    """
    role_test = Mailbox.role.not_in(roles) if outside else Mailbox.role.in_(roles)
    return (
        select(MessageLocation.id)
        .join(Mailbox, Mailbox.id == MessageLocation.mailbox_id)
        .where(MessageLocation.message_pk == entity.id, role_test)
        .exists()
    )


def _sent_pred(entity, own: set[str]):
    """Did *we* send this?

    Two signals OR'd together, because each misses a real case on its own. The
    address match misses mail sent from an alias the agent has not reported
    yet; the folder role misses mail filed in a custom-named sent folder that
    reported no SPECIAL-USE flag. `from_addr` is lowercased at parse time
    (core/mail/parse.py), so the IN comparison needs no folding here.
    """
    in_sent_folder = _has_location_in(entity, ("sent",))
    if not own:
        return in_sent_folder
    return or_(entity.from_addr.in_(sorted(own)), in_sent_folder)


def _local(entity, tz_offset: int):
    """`date_sent` shifted into the caller's local time.

    Hour-of-day and day-of-week are the whole point of two panels below, and
    both are meaningless in UTC for anyone who does not live there. The offset
    is a fixed number of minutes taken from the browser, so a window spanning a
    DST change buckets those days by the offset in force when the page loaded —
    an hour of slop at the boundary, which is well inside what these panels
    claim to tell you.
    """
    return entity.date_sent + literal(timedelta(minutes=tz_offset), Interval)


def _accounts(db: DBSession, account_id: int | None) -> list[Account]:
    q = select(Account)
    if account_id is not None:
        q = q.where(Account.id == account_id)
    rows = list(db.scalars(q).all())
    if account_id is not None and not rows:
        raise HTTPException(status_code=404, detail="Account not found")
    return rows


def _own_addresses(accounts: list[Account]) -> set[str]:
    """Every address these accounts send as, lowercased."""
    out: set[str] = set()
    for a in accounts:
        if a.email:
            out.add(a.email.strip().lower())
        for extra in a.send_addresses or []:
            if isinstance(extra, str) and extra.strip():
                out.add(extra.strip().lower())
    return out


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# --- Endpoint -----------------------------------------------------------------


@router.get("/overview")
def overview(
    db: DBSession = Depends(get_db),
    account_id: int | None = None,
    range: str = Query("30d", pattern="^(7d|30d|90d|1y|all)$"),
    # Minutes east of UTC, i.e. the browser's -getTimezoneOffset(). Bounded to
    # the real range of civil offsets so a junk value cannot shift buckets into
    # nonsense.
    tz_offset: int = Query(0, ge=-840, le=840),
    limit: int = Query(12, ge=1, le=50),
):
    accounts = _accounts(db, account_id)
    if not accounts:
        return _empty(range)

    own = _own_addresses(accounts)
    days = RANGES[range]
    since = utcnow() - timedelta(days=days) if days else None

    sent = _sent_pred(Message, own)
    base = [
        Message.account_id.in_([a.id for a in accounts]),
        Message.date_sent.is_not(None),
        # Keep a message if it lives anywhere that is not drafts/junk. A message
        # only in those folders drops out; one in Junk *and* Archive stays.
        _has_location_in(Message, EXCLUDED_ROLES, outside=True),
    ]
    if since is not None:
        base.append(Message.date_sent >= since)

    is_sent = case((sent, 1), else_=0)
    is_recv = case((sent, 0), else_=1)

    totals = db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(is_sent), 0),
            func.min(Message.date_sent),
            func.max(Message.date_sent),
            func.count(func.distinct(Message.thread_id)),
        ).where(*base)
    ).one()
    total, n_sent, first_at, last_at, n_threads = totals
    n_recv = int(total) - int(n_sent)

    # Denominator for the per-day averages. Not the nominal window length: on a
    # half-backfilled mailbox, or an account younger than the window, dividing
    # by 365 understates every rate by whatever fraction is missing. Span of
    # actual data, floored at 1 so a single day's mail is not divided by zero.
    if first_at and last_at:
        span_days = max(1.0, (last_at - first_at).total_seconds() / 86400.0)
        if days:
            span_days = min(span_days, float(days))
    else:
        span_days = 1.0

    payload = {
        "range": range,
        "grain": GRAINS[range],
        "accounts": [{"id": a.id, "email": a.email, "label": a.label} for a in accounts],
        "totals": {
            "messages": int(total),
            "received": n_recv,
            "sent": int(n_sent),
            "threads": int(n_threads or 0),
            "first_at": _iso(first_at),
            "last_at": _iso(last_at),
            "span_days": round(span_days, 2),
            "received_per_day": round(n_recv / span_days, 2),
            "sent_per_day": round(int(n_sent) / span_days, 2),
            # Guarded: a mailbox with sent mail and nothing received is rare but
            # real (a send-only alias), and it must not 500 the whole page.
            "sent_ratio": round(int(n_sent) / n_recv, 3) if n_recv else None,
        },
    }
    if not total:
        payload.update(_empty(range))
        payload["range"] = range
        return payload

    payload["volume"] = _volume(db, base, is_sent, is_recv, tz_offset, GRAINS[range])
    payload["heatmap"] = _heatmap(db, base, is_sent, is_recv, tz_offset)
    payload["correspondents"] = _correspondents(db, base, sent, own, limit)
    payload["domains"] = _domains(db, base, sent, limit)
    payload["threads"] = _threads(db, base)
    payload["attachments"] = _attachments(db, base, sent)
    payload["busiest"] = _busiest(db, base, is_recv, tz_offset)
    # A fifth of the window, capped at RESPONSE_MATURITY — see that constant.
    maturity = min(RESPONSE_MATURITY, timedelta(days=days / 5)) if days else RESPONSE_MATURITY
    payload["latency"] = _latency(db, base, own, maturity)
    return payload


def _empty(range: str) -> dict:
    """Shape-complete payload for an account with nothing in the window.

    The frontend renders panels off these keys; returning them empty rather
    than absent keeps every `.length` check on the client honest.
    """
    return {
        "range": range,
        "grain": GRAINS.get(range, "day"),
        "accounts": [],
        "totals": {
            "messages": 0, "received": 0, "sent": 0, "threads": 0,
            "first_at": None, "last_at": None, "span_days": 0,
            "received_per_day": 0, "sent_per_day": 0, "sent_ratio": None,
        },
        "volume": [], "heatmap": [], "correspondents": [], "domains": [],
        "threads": {"avg": 0, "max": 0, "multi": 0, "longest": []},
        "attachments": {"count": 0, "bytes": 0, "messages": 0},
        "busiest": None,
        "latency": {
            "mine": None, "mine_p90": None, "theirs": None, "theirs_p90": None,
            "buckets": [], "answered": 0, "inbound": 0, "outbound": 0,
            "answered_by_them": 0, "response_rate": None, "rate_basis": 0,
            "rate_answered": 0, "maturity_days": RESPONSE_MATURITY.days,
            "window_days": REPLY_WINDOW.days,
        },
    }


def _volume(db, base, is_sent, is_recv, tz_offset, grain) -> list[dict]:
    bucket = func.date_trunc(grain, _local(Message, tz_offset))
    rows = db.execute(
        select(bucket.label("b"), func.sum(is_recv), func.sum(is_sent))
        .where(*base)
        .group_by(bucket)
        .order_by(bucket)
    ).all()
    return [{"bucket": _iso(b), "received": int(r or 0), "sent": int(s or 0)} for b, r, s in rows]


def _heatmap(db, base, is_sent, is_recv, tz_offset) -> list[dict]:
    """Counts per (weekday, hour) in local time.

    Postgres `dow` is 0=Sunday; the client re-orders to a Monday-first week
    rather than having the server guess a locale convention.
    """
    local = _local(Message, tz_offset)
    dow = func.extract("dow", local)
    hour = func.extract("hour", local)
    rows = db.execute(
        select(dow.label("d"), hour.label("h"), func.sum(is_recv), func.sum(is_sent))
        .where(*base)
        .group_by(dow, hour)
    ).all()
    return [
        {"dow": int(d), "hour": int(h), "received": int(r or 0), "sent": int(s or 0)}
        for d, h, r, s in rows
    ]


def _correspondents(db, base, sent, own: set[str], limit: int) -> list[dict]:
    """Who you exchange mail with, counted in both directions and merged.

    Two queries rather than one: inbound identity is `messages.from_addr`,
    outbound identity is a `recipients` row, and there is no single column that
    means "the other party". Own addresses drop out of both sides so mail you
    sent yourself, and Cc's back to your own alias, do not top the list.
    """
    exclude = sorted(own)

    inbound_q = (
        select(
            Message.from_addr.label("addr"),
            func.max(Message.from_name).label("name"),
            func.count().label("n"),
            func.max(Message.date_sent).label("last"),
        )
        .where(*base, ~sent, Message.from_addr != "")
        .group_by(Message.from_addr)
    )
    if exclude:
        inbound_q = inbound_q.where(Message.from_addr.not_in(exclude))

    # count(distinct) because a message addressing the same person in both To
    # and Cc produces two `recipients` rows for one exchange.
    outbound_q = (
        select(
            Recipient.address.label("addr"),
            func.max(Recipient.name).label("name"),
            func.count(func.distinct(Message.id)).label("n"),
            func.max(Message.date_sent).label("last"),
        )
        .select_from(Message)
        .join(Recipient, Recipient.message_pk == Message.id)
        .where(*base, sent, Recipient.kind.in_(("to", "cc")), Recipient.address != "")
        .group_by(Recipient.address)
    )
    if exclude:
        outbound_q = outbound_q.where(Recipient.address.not_in(exclude))

    merged: dict[str, dict] = {}
    for addr, name, n, last in db.execute(inbound_q).all():
        merged[addr] = {"address": addr, "name": name or "", "received": int(n),
                        "sent": 0, "last_at": _iso(last)}
    for addr, name, n, last in db.execute(outbound_q).all():
        row = merged.setdefault(
            addr, {"address": addr, "name": "", "received": 0, "sent": 0, "last_at": None}
        )
        row["sent"] = int(n)
        row["name"] = row["name"] or (name or "")
        newest = _iso(last)
        if newest and (row["last_at"] is None or newest > row["last_at"]):
            row["last_at"] = newest

    ranked = sorted(merged.values(), key=lambda r: r["received"] + r["sent"], reverse=True)
    for r in ranked:
        r["total"] = r["received"] + r["sent"]
    return ranked[:limit]


def _domains(db, base, sent, limit: int) -> list[dict]:
    """Inbound volume by sender domain — collapses newsletters and vendors into
    one row each, which is usually where the actual noise turns out to live."""
    domain = func.split_part(Message.from_addr, "@", 2)
    rows = db.execute(
        select(domain.label("d"), func.count().label("n"))
        .where(*base, ~sent, Message.from_addr != "")
        .group_by(domain)
        .order_by(func.count().desc())
        .limit(limit)
    ).all()
    return [{"domain": d, "count": int(n)} for d, n in rows if d]


def _threads(db, base) -> dict:
    per_thread = (
        select(Message.thread_id.label("tid"), func.count().label("n"),
               func.max(Message.subject).label("subject"))
        .where(*base, Message.thread_id.is_not(None))
        .group_by(Message.thread_id)
        .subquery()
    )
    avg, mx, multi = db.execute(
        select(
            func.avg(per_thread.c.n),
            func.max(per_thread.c.n),
            func.count().filter(per_thread.c.n > 1),
        )
    ).one()
    longest = db.execute(
        select(per_thread.c.subject, per_thread.c.n)
        .order_by(per_thread.c.n.desc())
        .limit(5)
    ).all()
    return {
        "avg": round(float(avg or 0), 2),
        "max": int(mx or 0),
        "multi": int(multi or 0),
        "longest": [{"subject": s or "(no subject)", "count": int(n)} for s, n in longest],
    }


def _attachments(db, base, sent) -> dict:
    """Real attachments only. `is_inline` parts are signature logos and tracking
    pixels; counting them roughly triples the number and means nothing."""
    count, total_bytes, msgs = db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(Attachment.size_bytes), 0),
            func.count(func.distinct(Message.id)),
        )
        .select_from(Message)
        .join(Attachment, Attachment.message_pk == Message.id)
        .where(*base, Attachment.is_inline.is_(False))
    ).one()
    return {"count": int(count or 0), "bytes": int(total_bytes or 0), "messages": int(msgs or 0)}


def _busiest(db, base, is_recv, tz_offset) -> dict | None:
    """Heaviest single inbound day in the window.

    Always by day, even when the volume series is bucketed by month — "your
    worst month" is not the same answer and is far less interesting.
    """
    day = func.date_trunc("day", _local(Message, tz_offset))
    row = db.execute(
        select(day.label("d"), func.sum(is_recv).label("n"))
        .where(*base)
        .group_by(day)
        .order_by(func.sum(is_recv).desc())
        .limit(1)
    ).first()
    if not row or not row.n:
        return None
    return {"day": _iso(row.d), "received": int(row.n)}


def _latency(db, base, own: set[str], maturity: timedelta) -> dict:
    """Reply turnaround, in both directions, plus your response rate.

    A reply is the earliest message in the same thread, from the other side,
    later than the message being answered. That definition inherits whatever
    threading got right: `thread_id` comes from the References graph with a
    normalised-subject fallback (core/mail/threading.py), it is scoped per
    account, and it is rewritten retroactively when two threads turn out to be
    one. So these numbers are good but not exact, which the UI says out loud.

    Gaps beyond REPLY_WINDOW are treated as "never replied" rather than as a
    very slow reply — see the constant.

    Written as one windowed pass rather than the obvious correlated subquery
    ("earliest later message in this thread where the direction differs"). That
    version re-scans the thread once per message and measured 17s on an 81k
    mailbox; a MIN() over the following rows of each thread partition gets the
    same answer from a single ordered scan, in ~0.1s.
    """
    sent = _sent_pred(Message, own)
    mature_before = utcnow() - maturity
    # ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING, ordered by time: the
    # minimum date among *later* messages in this thread matching the filter is
    # exactly "when did the other side next write".
    frame = dict(
        partition_by=[Message.account_id, Message.thread_id],
        order_by=Message.date_sent,
        rows=(1, None),
    )
    next_sent = func.min(Message.date_sent).filter(sent).over(**frame)
    next_recv = func.min(Message.date_sent).filter(~sent).over(**frame)
    epoch = lambda a, b: func.extract("epoch", a - b)  # noqa: E731

    pairs = (
        select(
            case((sent, True), else_=False).label("is_sent"),
            (Message.date_sent <= mature_before).label("mature"),
            # They wrote, we answered.
            case((~sent, epoch(next_sent, Message.date_sent)), else_=None).label("mine"),
            # We wrote, they answered.
            case((sent, epoch(next_recv, Message.date_sent)), else_=None).label("theirs"),
        )
        .where(*base, Message.thread_id.is_not(None))
        .subquery()
    )

    cap = REPLY_WINDOW.total_seconds()
    # NULL for "no reply" and for replies past the window alike. percentile_cont
    # skips NULLs, so one expression drives the percentiles, the answered tally
    # (via count) and the histogram — they cannot drift apart.
    mine = case((pairs.c.mine <= cap, pairs.c.mine), else_=None)
    theirs = case((pairs.c.theirs <= cap, pairs.c.theirs), else_=None)

    buckets = []
    lower = 0
    for upper, label in LATENCY_BUCKETS:
        cond = mine.is_not(None) if upper is None else and_(mine.is_not(None), mine < upper)
        if lower:
            cond = and_(cond, mine >= lower)
        buckets.append(func.count(case((cond, 1), else_=None)))
        lower = upper or lower

    # The response-rate pair is restricted to mail old enough to have been
    # answered; the medians are not, because a median over answered mail has no
    # denominator to bias.
    inbound_mature = and_(~pairs.c.is_sent, pairs.c.mature)
    row = db.execute(
        select(
            func.count(case((~pairs.c.is_sent, 1), else_=None)),
            func.count(mine),
            func.percentile_cont(0.5).within_group(mine.asc()),
            func.percentile_cont(0.9).within_group(mine.asc()),
            func.count(case((pairs.c.is_sent, 1), else_=None)),
            func.count(theirs),
            func.percentile_cont(0.5).within_group(theirs.asc()),
            func.percentile_cont(0.9).within_group(theirs.asc()),
            func.count(case((inbound_mature, 1), else_=None)),
            func.count(case((and_(inbound_mature, mine.is_not(None)), 1), else_=None)),
            *buckets,
        ).select_from(pairs)
    ).one()
    n_in, ans_in, med_in, p90_in, n_out, ans_out, med_out, p90_out = row[:8]
    n_mature, ans_mature = row[8], row[9]

    def secs(v):
        return round(float(v), 1) if v is not None else None

    return {
        "mine": secs(med_in),
        "mine_p90": secs(p90_in),
        "theirs": secs(med_out),
        "theirs_p90": secs(p90_out),
        "inbound": int(n_in or 0),
        "answered": int(ans_in or 0),
        "response_rate": round(int(ans_mature or 0) / int(n_mature), 3) if n_mature else None,
        "rate_basis": int(n_mature or 0),
        "rate_answered": int(ans_mature or 0),
        "maturity_days": round(maturity.total_seconds() / 86400.0, 1),
        "outbound": int(n_out or 0),
        "answered_by_them": int(ans_out or 0),
        "buckets": [
            {"label": label, "count": int(c or 0)}
            for (_, label), c in zip(LATENCY_BUCKETS, row[10:])
        ],
        "window_days": REPLY_WINDOW.days,
    }
