"""One mail in several accounts: the rules for showing it once.

The case this exists for is ordinary. A sender writes to one address and BCCs
another, both addresses are accounts here, and meerail stores the mail twice,
because content is stored once per ``(account_id, dedup_key)`` and these are two
accounts (core/models.py::Message). Each account's own inbox is right to show
it. The lists that already mix accounts, All Inboxes, Parked and Reminders, then
showed the same mail as two rows next to each other, and the reader opened half
of it. A person who gets one mail twice expects one row, as Apple Mail does.

Two different groupings come out of that, and they must not be confused.

**A conversation group** is every row with the same thread key, whatever the
account. Thread ids are the root Message-ID (core/mail/threading.py), so the
same conversation carries the same ``thread_id`` string in both accounts and the
lists can merge on it with no new column. A message that was never threaded is
keyed by its own row id and therefore never merges with anything. Only the
lists in ``SPANNING_SCOPES`` merge. A folder belongs to one account, and search
is a list of hits rather than conversations, so neither changes.

**Twins** are two messages in different accounts that are the same mail
delivered twice. The reader shows them as one card, and Flagged, which lists
mails rather than conversations, as one row. The test is Message-ID, sender
and send time together:

* Never the content hash. Two deliveries differ in their Received and
  Delivered-To headers, so their bytes, and the hashes of them, always differ.
* Never the Message-ID alone. It is a header the sender writes, and a sender
  that reuses one would then hide one mail behind another in the reader, with
  nothing on screen to say a second mail existed. Adding From and Date makes
  an accidental match practically impossible.

FastAPI-free on purpose, like app/transport.py and app/staging.py, so the pure
parts can be tested without a server. The routers translate a bad request into
the HTTP answer.
"""

from __future__ import annotations

from sqlalchemy import String, and_, case, cast, func, literal_column

from core.models import Message

# The lists that already mix accounts, and so the only ones that merge one
# conversation across them. Flagged mixes accounts too, but it is a list of
# mails, so it collapses twins instead (see list_messages).
SPANNING_SCOPES = frozenset({"unified_inbox", "parked", "reminders"})

# How many accounts one conversation request may name. Far more than anyone
# has, and low enough that a malformed URL cannot turn one request into an
# unbounded IN list.
MAX_ACCOUNTS = 20

# How many other copies one message verb may carry along (the `also` list).
# A mail is rarely in more than two or three accounts. The ceiling is only
# there so a request is bounded.
MAX_ALSO = 50


def distinct_ids(values) -> list[int]:
    """The ids in the order they came, each once.

    Order matters for ``account_id`` on the thread route, where the first
    account named decides which copy of a twin the reader shows. A repeated id
    is a client being careless, not a request for the same thing twice.
    """
    out: list[int] = []
    for value in values or []:
        if value not in out:
            out.append(value)
    return out


def twin_key(message=Message):
    """A SQL expression equal for twins and unique for everything else.

    One text value rather than a tuple, so it can serve as a DISTINCT ON key, a
    window partition and a GROUP BY key alike. The parts are joined with
    chr(31), the ASCII unit separator, which no address, timestamp or
    Message-ID carries, so two different triples cannot run together into the
    same string. The Message-ID goes last, because it is the one part a sender
    can fill with anything.

    A mail without a Message-ID or a Date cannot be told apart from a different
    mail by this test, so it gets ``msg:<id>`` and is never anyone's twin.

    Written with literal SQL for the constants rather than bound parameters.
    Postgres matches a GROUP BY or DISTINCT ON expression against the select
    list textually, and two renderings of the same constant as two different
    placeholders do not match.

    This says nothing about accounts. Two copies of one mail in the same
    account (delivered to two of its aliases, or once directly and once through
    a mailing list) have the same key, and they are two messages there. Callers
    that collapse on this key pair copies across accounts and keep same-account
    copies apart (see collapse_twins and the flagged list).
    """
    sep = literal_column("chr(31)")
    return case(
        (and_(message.message_id.is_not(None), message.date_sent.is_not(None)),
         func.concat(func.lower(message.from_addr), sep,
                     cast(message.date_sent, String), sep, message.message_id)),
        else_=func.concat(literal_column("'msg:'"), message.id),
    )


def twin_key_of(msg) -> tuple | None:
    """The same test in Python, for a message already loaded. None means the
    message has no twin by definition (no Message-ID, or no Date)."""
    if not msg.message_id or msg.date_sent is None:
        return None
    return (msg.message_id, (msg.from_addr or "").lower(), msg.date_sent)


def collapse_twins(msgs, account_order) -> list[tuple[object, list]]:
    """Fold twins in a conversation into one entry each.

    Takes the messages in the order the reader shows them and returns
    ``(representative, [other copies])`` pairs in that same order. The other
    copies list is empty for nearly every message.

    The representative is the copy in the account named earliest in
    ``account_order``, then the lowest id. That is the account the list row
    was standing for, so the card that opens is the one the row described.

    Copies pair up across accounts only. Two copies of one mail in the same
    account (two aliases, or a list copy beside the direct one) stay two
    entries, because there they are two messages, and hiding one would hide a
    mail the account really holds twice. When each account holds the same
    number of copies they pair by id, first with first. That is a guess when
    the counts are above one, and a harmless one: every pairing shows each
    account's copies exactly once.
    """
    rank = {account_id: i for i, account_id in enumerate(account_order)}

    def preference(m):
        return (rank.get(m.account_id, len(rank)), m.id)

    by_key: dict[tuple, dict[int, list]] = {}
    for m in msgs:
        key = twin_key_of(m)
        if key is not None:
            by_key.setdefault(key, {}).setdefault(m.account_id, []).append(m)

    twins_of: dict[int, list] = {}
    absorbed: set[int] = set()
    for per_account in by_key.values():
        if len(per_account) < 2:
            continue                      # one account only, nothing to fold
        columns = [sorted(copies, key=lambda m: m.id) for copies in per_account.values()]
        for i in range(max(len(c) for c in columns)):
            rep, *others = sorted((c[i] for c in columns if len(c) > i), key=preference)
            twins_of[rep.id] = others
            absorbed.update(o.id for o in others)
    return [(m, twins_of.get(m.id, [])) for m in msgs if m.id not in absorbed]
