"""Near-duplicate fingerprints for message bodies — ``messages.body_sig``.

What this exists for: the Cleanup panel groups mail that is *the same kind of
mail*. Grouping by sender and subject template catches most of it, but a good
share of bulk mail writes a fresh subject every time — "3 neue Angebote:
Reihenhaus in …" — while the body stays the same template underneath. Those
only group by what the body says.

Doing that by comparing bodies is not an option at mailbox scale: similarity is
a property of a *pair*, and a hundred thousand messages have five billion
pairs. So each body is reduced once, on the way in, to a short fingerprint with
the property that two similar bodies collide — MinHash over word shingles,
banded (the standard LSH construction). Grouping then costs a scan and a hash
join instead of a comparison per pair, and the whole thing survives being asked
for on a modal open.

Three consequences worth knowing before changing anything here:

  * **The fingerprint is stored, so its definition is frozen.** Change the
    normalisation, the shingle width or the slicing and every value already in
    the table describes a different function — old and new mail stop colliding,
    silently, and the panel quietly stops grouping across the upgrade. A change
    to any of it has to come with a re-run over the whole table: set
    ``body_sig`` to NULL and let the backfill loop have it.
  * **``hash()`` is not usable.** Python salts it per process, so the same body
    would fingerprint differently after a restart. Everything here goes through
    blake2b for that reason.
  * **Empty string is a real answer.** NULL means "not computed yet" and is what
    the backfill looks for; ``''`` means "computed, and there is nothing here to
    fingerprint" — a two-line note, a message stored headers-only, a body that
    is one image. Without the distinction the backfill would re-read those rows
    on every probe, forever.
"""

from __future__ import annotations

import hashlib
import re
import struct

from sqlalchemy import bindparam, func, select
from sqlalchemy.orm import Session

from .mail.parse import html_to_text
from .models import Message

# Words per shingle. Shingles, rather than bare words, are what make this about
# phrasing instead of vocabulary: two different property listings from the same
# generator share nearly every *word* (preis, zimmer, kaufpreis, immowelt) and
# would look identical on a bag of words. Five is the usual choice for prose —
# long enough that a shared shingle means a shared sentence.
SHINGLE_WORDS = 5

# How much of the body is read. Bulk mail puts its template at the top and its
# variable payload — the listings, the job ads, the invoice lines — below, so a
# window at the front is both the cheapest part to read and the part that says
# which template this is. It also bounds the work: without it a single 200-page
# newsletter would cost more than a thousand notifications.
MAX_WORDS = 200

# Below this there is not enough text for a fingerprint to mean anything: a
# handful of words collides with every other handful of words, and a panel that
# offers to delete "all your two-line messages" as one group is worse than one
# that says nothing about them.
MIN_WORDS = SHINGLE_WORDS + 8

# MinHash size and band width. 12 hashes in bands of 3 gives 4 bands, and two
# bodies are grouped when any band matches — probability 1-(1-s^3)^4 for Jaccard
# similarity s. That curve is ~0.98 at s=0.9, ~0.8 at s=0.7, ~0.4 at s=0.5 and
# ~0.1 at s=0.3: firmly yes for a shared template, firmly no for two unrelated
# mails, and deliberately steep in between. Raising BAND makes it stricter,
# raising BANDS looser; both change the stored value.
HASHES = 12
BAND = 3
BANDS = HASHES // BAND

# The 12 hash functions are 12 disjoint 4-byte slices of one blake2b digest,
# rather than 12 multiply-mod-prime passes over every shingle. Same construction
# in the ways that matter — the slices of a cryptographic digest are independent
# — and about a tenth of the work, which is the difference between a backfill
# that takes minutes on a 113k mailbox and one that takes the better part of an
# hour.
_DIGEST = HASHES * 4
_UNPACK = struct.Struct(f">{HASHES}I").unpack
_BAND_PACK = struct.Struct(f">{BAND}I").pack

# URLs go before anything else: a tracking link is different in every copy of
# the same newsletter, and its path segments survive word-splitting as a fistful
# of nonsense shingles that make two identical mails look unrelated.
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_ADDR_RE = re.compile(r"\S+@\S+\.\w+")
# Everything that is not a letter becomes a gap. Digits go with it — the whole
# point is that "3 neue Angebote" and "7 neue Angebote" are the same mail — and
# so does punctuation, which quoting and line-wrapping move around. `\W` is
# Unicode-aware here, so German text keeps its umlauts rather than being cut in
# half at every one.
_NOISE_RE = re.compile(r"[\W\d_]+", re.UNICODE)


def words(text: str) -> list[str]:
    """The body reduced to the word sequence the fingerprint is taken over."""
    if not text:
        return []
    s = _URL_RE.sub(" ", text)
    s = _ADDR_RE.sub(" ", s)
    s = _NOISE_RE.sub(" ", s.lower())
    return s.split()[:MAX_WORDS]


def fingerprint(text: str) -> str:
    """Band tokens for one body, space-separated. ``''`` when there is no body.

    The return value is what goes in ``messages.body_sig``: four 8-hex-digit
    tokens. Two messages are near-duplicates when they share a sender and any
    one token — see app/routers/cleanup.py, which does the grouping.
    """
    w = words(text)
    if len(w) < MIN_WORDS:
        return ""

    # Running minimum per slot, over the shingle set. Kept as a running min
    # rather than a list of digests because a 200-word body has ~196 shingles
    # and the whole mailbox goes through here.
    mins = [0xFFFFFFFF] * HASHES
    seen: set[bytes] = set()
    for i in range(len(w) - SHINGLE_WORDS + 1):
        digest = hashlib.blake2b(
            " ".join(w[i:i + SHINGLE_WORDS]).encode(), digest_size=_DIGEST
        ).digest()
        # MinHash is defined over a *set*: a template that repeats a line should
        # not get to vote twice for the shingles in it.
        if digest in seen:
            continue
        seen.add(digest)
        for slot, value in enumerate(_UNPACK(digest)):
            if value < mins[slot]:
                mins[slot] = value

    return " ".join(
        hashlib.blake2b(_BAND_PACK(*mins[i * BAND:(i + 1) * BAND]), digest_size=4).hexdigest()
        for i in range(BANDS)
    )


def shares_band(column, sig: str):
    """SQL for "this fingerprint resembles `sig`" — they agree on some band.

    The one definition of near-duplicate in the app, so that the Cleanup panel's
    groups, the `:similar` search filter and the delete that follows a group all
    mean the same thing by it. When they did not, the panel offered a group of
    438 and the search it opened showed 142 of them.

    `sig` is either a whole fingerprint, in which case each token is matched in
    the band it belongs to, or a single token, which is matched in any band —
    the difference is only that a bare token arrives without its position. The
    looseness of the second form is theoretical: bands are 32-bit, so a token
    colliding with a different band of unrelated mail is a one-in-four-billion
    event. Returns None when `sig` says nothing, which callers must read as
    "matches nothing" rather than as "no condition".
    """
    from sqlalchemy import func, or_

    tokens = (sig or "").split()
    if not tokens:
        return None
    if len(tokens) > 1:
        return or_(*[func.split_part(column, " ", i + 1) == token
                     for i, token in enumerate(tokens[:BANDS])])
    return or_(*[func.split_part(column, " ", i + 1) == tokens[0] for i in range(BANDS)])


def body_of(body_text: str | None, body_html: str | None) -> str:
    """The text a message's fingerprint is taken from.

    The same fallback the search corpus uses (core/mail/store.py), and for the
    same reason: half of bulk mail carries no plain-text part at all, and it is
    exactly the half this feature exists for.
    """
    if body_text and body_text.strip():
        return body_text
    return html_to_text(body_html or "")


def sig_for(body_text: str | None, body_html: str | None) -> str:
    return fingerprint(body_of(body_text, body_html))


# --- Backfill -----------------------------------------------------------------
# Mail stored before this column existed has NULL in it. Same shape as
# core/searchindex.py, and for the same reason: the work is proportional to the
# mailbox, so it is owed in the background rather than paid at startup.

# Smaller than the search index's batch because each row here is read in full —
# the body is detoasted, the HTML is flattened — rather than being handed to a
# function inside Postgres. A couple of hundred is a fraction of a second of
# work and a couple of megabytes of read.
BATCH = 200


def pending(db: Session) -> int:
    """How many messages still have no fingerprint.

    Answered from ``ix_messages_body_sig_missing``, a partial index that covers
    exactly these rows and is empty once the backfill is done — so the Cleanup
    panel can say "still building" on every open without that question costing a
    scan of the mailbox.
    """
    return db.scalar(
        select(func.count()).select_from(Message).where(Message.body_sig.is_(None))
    ) or 0


def build_batch(db: Session, limit: int = BATCH) -> int:
    """Fingerprint the next `limit` messages that have none. Returns how many."""
    rows = db.execute(
        select(Message.id, Message.body_text, Message.body_html)
        .where(Message.body_sig.is_(None))
        .order_by(Message.id)
        .limit(limit)
    ).all()
    if not rows:
        return 0

    params = [{"b_id": r.id, "b_sig": sig_for(r.body_text, r.body_html)} for r in rows]
    messages = Message.__table__
    db.execute(
        messages.update()
        .where(messages.c.id == bindparam("b_id"))
        # Held still on purpose, exactly as the search-index backfill does: the
        # column carries onupdate, and this write says nothing about the mail
        # itself — restamping the whole mailbox as changed today would be a lie
        # that every "what is new" query in the app would then repeat.
        .values(body_sig=bindparam("b_sig"), updated_at=messages.c.updated_at),
        params,
    )
    db.commit()
    return len(rows)
