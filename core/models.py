"""SQLAlchemy ORM models for meerail.

Design notes
------------
* A message's *content* is stored once per ``(account_id, dedup_key)``. Its
  placement in IMAP folders (and per-folder flags/UID) lives in
  ``message_locations`` — this models Proton Bridge exposing labels as folders,
  where one Message-ID appears in several folders.
* High-volume rows (messages, locations, recipients, attachments) use integer
  surrogate keys for compact joins; accounts/mailboxes too.
* ``messages.search_text`` (subject + participants + body + extracted attachment
  text) carries a GIN pg_trgm index so real regex (``~*``) can use the index when
  the pattern contains a literal substring; a btree on ``date_sent`` bounds the
  time-window scans.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    # Naive UTC everywhere internally; tz-aware input is normalized at the edges.
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Footer new accounts start with. Editable per account in Settings; clearing it
# there sticks (see ``footer_customized``), so this is a starting value and not
# a floor. The composer prefills it into the editor — the user can edit or
# delete it before sending — so it is never forced onto a message. No RFC 3676
# "-- " marker, since that makes some clients collapse it out of sight.
DEFAULT_FOOTER = (
    "----\n"
    "This mail was sent using https://meerail.com/ "
    "- the email management tool for professional users"
)


# --- Accounts & folders ----------------------------------------------------


class Account(Base):
    """One mail account, served by an agent connected to its Bridge.

    Bridge credentials live in the agent's own config by default (they never
    leave the host); this row is identity + display + sync status. The agent
    references an account by ``email``.
    """

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    label: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    # Accent colour for the account dot in the unified inbox (hex or name).
    color: Mapped[str] = mapped_column(String(32), default="#1d6ff2", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # No agent syncs this account: its mail came off disk (tools/import_mbox.py)
    # and there is no server on the other end of it. Everything that would
    # otherwise be queued for an agent — creating a folder, moving a message —
    # is applied here instead, because nothing is ever going to drain that queue.
    #
    # Set by the importer when it creates the account, and cleared by
    # ``ingest.get_or_create_account`` the moment an agent does turn up for it,
    # so a wrong guess corrects itself on the first sync pass rather than
    # standing forever. See app/routers/mailboxes.py and app/mailops.py.
    local: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Extra "send as" addresses for this account (Proton lets one account own
    # several addresses/aliases). Declared in the agent config and reported on
    # sync; the primary ``email`` is always a valid sender regardless of this.
    send_addresses: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    # Display name per sendable address — lower-cased address to the name that
    # goes on the From header when sending as it, the primary ``email``
    # included. Kept beside ``send_addresses`` rather than inside it because
    # that list is compared against message addresses all over the app (see
    # analytics, compose) and those comparisons want bare addresses.
    #
    # Not ``label``: that names the *account* in the sidebar and never leaves
    # the UI. This is what recipients see, so it comes from the agent config
    # alongside the addresses it names, and is rewritten on every sync pass.
    send_names: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # Signature/disclaimer appended to every message sent from this account.
    # Empty disables it. Composition is the web app's job, so unlike the sync
    # settings above this is set in the UI, not the agent config.
    footer: Mapped[str] = mapped_column(Text, default=DEFAULT_FOOTER, nullable=False)

    # True once the footer has been saved from Settings. Guards the one-time
    # backfill in init_db: without it, an account whose footer the user cleared
    # would have DEFAULT_FOOTER put back on every restart.
    footer_customized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Which of ``label``, ``color`` and ``footer`` are pinned in the agent's
    # meerail.toml rather than owned by Settings. The agent rewrites both the
    # values and this list on every pass (``ingest.record_presentation``), which
    # is how a web app on another machine — one that never sees the file — knows
    # to show those fields as configured elsewhere and refuse to PATCH them.
    # Empty is the ordinary case: nothing pinned, everything editable.
    config_fields: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    # What the mail server will let a folder be called, read off its LIST by the
    # agent on every pass (agent/imap.py::folder_capabilities) because only the
    # agent has a connection to ask.
    #
    # ``folder_delimiter`` is what goes between a parent and a child — "/" on
    # Bridge and Gmail, "." on plenty of Dovecot installs — and "" until an
    # agent has said. ``folder_nesting`` is whether a folder may hold another
    # one at all: false on Proton Bridge, where every user folder comes back
    # \\Noinferiors, true on most other servers.
    #
    # The web app reads both to decide whether "Archive/2024" is a name it may
    # accept. It used to answer that for every account with Bridge's answer,
    # which refused nesting on servers that do it perfectly well. False is the
    # right default: an account no agent has reported on yet keeps the old
    # behaviour rather than offering something that might fail on the server.
    folder_delimiter: Mapped[str] = mapped_column(String(8), default="", nullable=False)
    folder_nesting: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Sync status (denormalized for the UI).
    backfill_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Stamped at the start of every pass, including passes that then fail — so
    # this tracks "the agent process is alive", not "syncing works".
    last_agent_seen: Mapped[datetime | None] = mapped_column(DateTime)
    # Stamped only when a pass completes. Lagging well behind last_agent_seen
    # means passes are starting and dying.
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Last sync failure, cleared on the next successful pass. Without this a
    # wedged agent is indistinguishable from an idle one: the retry loop in
    # agent/sync.py swallows its exceptions, so nothing else records them.
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Set from the UI to ask the agent for a full recheck: rewind every folder's
    # UID cursor so the next pass re-walks the mailbox from the start instead of
    # only fetching what is new. For repairing a database that lost or corrupted
    # messages the cursor would otherwise skip straight past.
    #
    # A column rather than a NOTIFY (which is how the plain refresh button asks)
    # because this is the button you press when the agent is unhealthy — it has
    # to survive the agent being down, mid-restart, or in its retry backoff. The
    # agent clears it only once a full pass has finished.
    recheck_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    recheck_requested_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Where the agent is in its current (or last) sync pass — folder counter,
    # per-folder done/total, and pass-level tallies. Written once per ingested
    # batch, in that batch's own transaction, so it can never claim progress a
    # rollback took back. See ``agent/sync.py``'s PassProgress for the shape.
    #
    # A JSONB blob rather than columns because nothing queries it: it is read
    # whole, by one panel, and the fields are free to change without a migration.
    # It survives the pass ending (with ``active`` false) so the UI can show what
    # the last pass did instead of blanking the moment it finishes.
    sync_progress: Mapped[dict | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    mailboxes: Mapped[list["Mailbox"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class Mailbox(Base):
    """An IMAP folder within an account, with the sync cursor for it."""

    __tablename__ = "mailboxes"
    __table_args__ = (
        UniqueConstraint("account_id", "imap_name", name="uq_mailbox_account_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    imap_name: Mapped[str] = mapped_column(String(1024), nullable=False)  # full IMAP path
    display_name: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    # inbox | sent | drafts | archive | junk | trash | flagged | all | custom
    role: Mapped[str] = mapped_column(String(32), default="custom", nullable=False)

    # Sync cursor. last_uid = highest UID ingested for stateless agent resume.
    uidvalidity: Mapped[int | None] = mapped_column(BigInteger)
    uidnext: Mapped[int | None] = mapped_column(BigInteger)
    last_uid: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # When the server's LIST stopped mentioning this folder, or NULL while it is
    # still being listed. A folder is not removed on the strength of one absence
    # — a Bridge that is still loading answers LIST with part of the mailbox, and
    # reading that as "the rest were deleted" takes their mail with them. See
    # core/ingest.py::prune_mailboxes.
    missing_since: Mapped[datetime | None] = mapped_column(DateTime)

    # When the server refused mail into this folder, or NULL for every folder it
    # has never refused — which is nearly all of them. Set by the agent, read by
    # the app, because only one of the two has a connection and only the other
    # one picks where a message is filed: a \\All folder that cannot be moved
    # into (Proton Bridge) is otherwise chosen again on every archive. See
    # agent/actions.py::_mark_write_refused.
    writes_refused_at: Mapped[datetime | None] = mapped_column(DateTime)

    # A folder that exists only in meerail — made here, never listed by any
    # server. It is the one thing prune_mailboxes must not remove: that pass
    # deletes every folder missing from the server's LIST, and a local folder is
    # missing from it by definition, so without this flag the folder and (with
    # the last placement of each message) its mail would go on the first pass
    # after it was made. Kept on the folder rather than inferred from
    # ``Account.local`` so it stays true even if that guess is later corrected.
    local: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    unread_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    subscribed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Pinned by the user into the sidebar's Favorites section.
    favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    account: Mapped["Account"] = relationship(back_populates="mailboxes")


# --- Messages --------------------------------------------------------------


class Message(Base):
    """Parsed message content, stored once per (account, dedup_key)."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("account_id", "dedup_key", name="uq_message_account_dedup"),
        Index("ix_messages_account_date", "account_id", "date_sent"),
        # Ingest time, not send time — powers the "downloaded in the last hour/day"
        # counters in /api/sync/status, which would otherwise seq-scan the table.
        Index("ix_messages_account_created", "account_id", "created_at"),
        # Partial: read_at is set only from the moment a read is observed, so on
        # any mailbox with history most rows are NULL and indexing them would be
        # most of the index. See the column, and analytics' read heatmap.
        Index("ix_messages_account_read", "account_id", "read_at",
              postgresql_where=text("read_at IS NOT NULL")),
        Index("ix_messages_thread", "thread_id"),
        Index("ix_messages_message_id", "message_id"),
        # GIN trigram index: lets Postgres use the index for ~*/LIKE when the
        # regex/pattern contains an extractable literal substring (>=3 chars).
        Index(
            "ix_messages_search_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # RFC Message-ID (nullable/duplicable in the wild) + a guaranteed dedup key
    # (message_id when present, else a hash synthesized from headers/body).
    message_id: Mapped[str | None] = mapped_column(String(998))
    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False)

    # A hash of the bytes this message was stored from, with line endings
    # normalised so the same mail hashes the same over IMAP and out of an mbox.
    # This is what says whether two messages claiming one Message-ID are one
    # message — the id is a header the sender wrote, the content is the message.
    # NULL where there was nothing to hash: rows stored from headers alone, and
    # rows that predate the column. See core/mail/store.py::same_message.
    content_hash: Mapped[str | None] = mapped_column(String(80))

    # Threading
    thread_id: Mapped[str | None] = mapped_column(String(255))
    in_reply_to: Mapped[str | None] = mapped_column(String(998))
    references: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    subject: Mapped[str] = mapped_column(Text, default="", nullable=False)
    subject_norm: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    from_name: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    from_addr: Mapped[str] = mapped_column(String(320), default="", nullable=False)

    date_sent: Mapped[datetime | None] = mapped_column(DateTime)
    date_received: Mapped[datetime | None] = mapped_column(DateTime)
    # When this message was first *observed* to become read, naive UTC.
    #
    # IMAP stores whether a message is seen, never when it became seen, so this
    # is the only record of it there can be. It is stamped on a transition —
    # the reader marking a message read, or a sync finding a placement that was
    # unread last time and is read now — and never on first sight of a message
    # that already carried \Seen, because that read happened at an unknown time
    # (usually years ago, during backfill). It therefore only fills in from the
    # moment the column existed, and stays NULL for the mailbox's whole history
    # before that: the "when mail is read" panel says so out loud rather than
    # drawing a blank grid that looks like "you never read anything".
    #
    # First read only. A message re-marked unread and read again keeps the
    # original stamp, because the question the panel answers is when mail
    # reaches you, not how often you revisit it.
    read_at: Mapped[datetime | None] = mapped_column(DateTime)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    snippet: Mapped[str] = mapped_column(Text, default="", nullable=False)
    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # The original RFC822 bytes. Stored in the DB so the ingesting agent and the
    # serving web app share no filesystem — the DB is the only handoff.
    #
    # Deferred, and this is not an optimisation but the difference between the
    # reader opening and the reader hanging: nothing outside ingest ever reads
    # these two, yet `select(Message)` — which is how the thread view, archive,
    # trash and rethread all load mail — dragged both across the wire for every
    # message in the conversation. Attachments live in here base64-encoded, so a
    # four-message thread carrying a couple of videos is 130MB of raw_mime read,
    # detoasted and materialised in Python to render 14kB of HTML: a ~17s stall
    # holding a pooled connection and a threadpool slot throughout. Deferring
    # means the column is simply not in the SELECT list; assigning to it still
    # works, so ingest is unaffected, and the search WHERE clauses reference
    # search_text in SQL rather than through the attribute.
    raw_mime: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    body_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    body_html: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Concatenation indexed for regex/keyword search.
    search_text: Mapped[str] = mapped_column(Text, default="", nullable=False, deferred=True)

    # Rollup of attachment text extraction: none | pending | done | error
    extract_status: Mapped[str] = mapped_column(String(16), default="none", nullable=False)

    # Whether this row's content is here at all, and if not, why:
    #   full     the body, attachments and (optionally) raw MIME are stored
    #   skipped  older than the content window when it was first seen, so only
    #            the headers were ever fetched
    #   pruned   fetched in full, then stripped back to headers when it aged out
    # The two absent states are kept apart for the operator, not the reader: the
    # UI says the same thing for both, but "did we ever have this?" is the first
    # question anyone asks of a mailbox with holes in it.
    content_status: Mapped[str] = mapped_column(String(16), default="full", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    locations: Mapped[list["MessageLocation"]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )
    recipients: Mapped[list["Recipient"]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )
    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )


class MessageLocation(Base):
    """Placement of a message in one IMAP folder, with that folder's flags/UID.

    The unit two-way flag/move/delete sync operates on. A message with the same
    Message-ID in three Proton folders has three rows here, one Message row.
    """

    __tablename__ = "message_locations"
    __table_args__ = (
        UniqueConstraint("mailbox_id", "imap_uid", name="uq_location_mailbox_uid"),
        Index("ix_location_message", "message_pk"),
        Index("ix_location_mailbox", "mailbox_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_pk: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    mailbox_id: Mapped[int] = mapped_column(
        ForeignKey("mailboxes.id", ondelete="CASCADE"), nullable=False
    )
    imap_uid: Mapped[int] = mapped_column(BigInteger, nullable=False)

    seen: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    answered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    draft: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    keywords: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    message: Mapped["Message"] = relationship(back_populates="locations")


class Recipient(Base):
    __tablename__ = "recipients"
    __table_args__ = (
        Index("ix_recipient_message", "message_pk"),
        Index("ix_recipient_address", "address"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_pk: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # from|to|cc|bcc|reply_to
    name: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    address: Mapped[str] = mapped_column(String(320), default="", nullable=False)

    message: Mapped["Message"] = relationship(back_populates="recipients")


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (Index("ix_attachment_message", "message_pk"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_pk: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    content_id: Mapped[str | None] = mapped_column(String(512))  # inline cid
    is_inline: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    content: Mapped[bytes | None] = mapped_column(LargeBinary)

    extracted_text: Mapped[str | None] = mapped_column(Text)
    # pending | done | error | skipped
    extract_status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)

    # Precomputed WebP preview for PDFs and images (see core/mail/thumbs.py).
    thumb: Mapped[bytes | None] = mapped_column(LargeBinary)
    # pending | done | error | skipped
    thumb_status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    message: Mapped["Message"] = relationship(back_populates="attachments")


class Contact(Base):
    """Materialized address book for compose autocomplete, rebuilt periodically
    from every from/to/cc/bcc address seen within the configured time window."""

    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # times corresponded
    last_seen: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class ContactPair(Base):
    """Materialized co-recipient graph: who you address together.

    One row per ordered pair, so a lookup for the addresses already in the
    composer is a plain index scan on address_a. Rebuilt alongside `contacts`
    and over the same window, so the two stay consistent — `weight` here and
    `Contact.count` are only comparable because of that.
    """

    __tablename__ = "contact_pairs"
    __table_args__ = (
        UniqueConstraint("address_a", "address_b", name="uq_contact_pair"),
        Index("ix_contact_pair_a", "address_a"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    address_a: Mapped[str] = mapped_column(String(320), nullable=False)
    address_b: Mapped[str] = mapped_column(String(320), nullable=False)
    # Messages the two appeared on together, and the same counted with mail the
    # user sent weighted double — deliberately addressing people together is a
    # stronger signal than having been put on the same thread by someone else.
    count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    weight: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Thread(Base):
    """Denormalized conversation record for fast list rendering + analytics."""

    __tablename__ = "threads"
    __table_args__ = (Index("ix_thread_account_latest", "account_id", "latest_date"),)

    id: Mapped[str] = mapped_column(String(255), primary_key=True)  # thread_id
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    subject_norm: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    latest_date: Mapped[datetime | None] = mapped_column(DateTime)
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    participants: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


# --- Reminders -------------------------------------------------------------


class Reminder(Base):
    """A conversation the user asked to be shown again later.

    Set on one message and applied to its whole conversation, for the same
    reason archive and trash are (see actions._thread_move): mail arrives as a
    thread, and "not now" means all of it.

    The waiting is this app's, not the agent's. Nothing here reaches IMAP:
    parking a conversation and bringing it back both queue the ordinary
    ``PendingAction`` a move queues, so a mail server that is unreachable on
    Monday morning delays a reminder rather than losing one. The clock is
    watched by the worker in ``app/workers.py``; this row is the whole record of
    what was asked for.

    ``parked`` is what makes the return trip possible: for each message that was
    moved, the folders it was moved *out of*, snapshotted before the move
    because afterwards there is nothing left to read it off. A message that was
    already filed away when the reminder was set records the inbox instead —
    "remind me about this" said over an archived mail means put it in front of
    me, and there is nowhere else it could sensibly land.

    Folder ids in ``parked`` and ``park_mailbox_id`` are plain integers rather
    than foreign keys, which is deliberate. A folder the server stops listing is
    deleted (core/ingest.py::prune_mailboxes), and neither answer a foreign key
    can give is the right one: CASCADE would throw a reminder away because a
    folder got renamed, and SET NULL would quietly drop the half that says where
    the mail goes back to while leaving the row looking healthy. So they are
    carried as data and re-checked at the one moment the answer matters, which
    is when the reminder fires.
    """

    __tablename__ = "reminders"
    __table_args__ = (
        # The worker's only query: "what is pending and due". Leading with
        # `state` keeps it off the far larger tail of reminders already fired.
        Index("ix_reminder_due", "state", "due_at"),
        Index("ix_reminder_message", "message_pk"),
        Index("ix_reminder_thread", "account_id", "thread_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # The message the reminder was set on. Its conversation is what actually
    # moves, but this is what the UI points at and what a second "remind me" on
    # the same mail finds in order to re-schedule rather than park twice.
    message_pk: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    thread_id: Mapped[str | None] = mapped_column(String(255))

    # Naive UTC, like every other instant in meerail (see utcnow). The browser
    # works out what "next Monday, 9am" is in the reader's own timezone and
    # sends the resulting absolute instant, so a reminder means the same thing
    # from whichever machine it is later looked at.
    due_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    park_mailbox_id: Mapped[int | None] = mapped_column(Integer)
    # [{"message": <message id>, "from": [<mailbox id>, ...]}, ...]
    parked: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    # pending | done | cancelled. A reminder that cannot be applied — the folder
    # it should go back to is gone, a move is still in flight — stays "pending"
    # with the reason in `error` and is tried again on the next tick, because
    # everything that stops it is a thing that can stop being true. Nothing
    # retires a reminder except firing it or the user taking it back.
    state: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    fired_at: Mapped[datetime | None] = mapped_column(DateTime)

    # --- Which install brings this one back (journal installs only) ----------
    #
    # All three machines hold this reminder and all three watch the clock, so at
    # nine on Monday all three would move the same conversation back into the
    # inbox and mark it unread — three times, and twice against a message the
    # first move has already taken out of Archive.
    #
    # So firing is claimed, and the journal's sequence number is what decides it:
    # every install appends "I will take this one", and the lowest number wins,
    # because the server that hands out those numbers is the only participant
    # with an opinion all three trust. See app/journal.py::claim.
    #
    # NULL on an install with no journal configured, which is every install by
    # default — there is nobody to race, and run_due fires without asking.
    claim_seq: Mapped[int | None] = mapped_column(BigInteger)
    claim_by: Mapped[str | None] = mapped_column(String(64))
    # When the claim was made. A claim expires (app/journal.py::CLAIM_TTL): the
    # machine that won may be shut before it fires, and a promise that can only
    # be kept by a laptop that is now in a bag is not a promise. After the TTL
    # the others may claim it again.
    claim_at: Mapped[datetime | None] = mapped_column(DateTime)


# --- App settings ----------------------------------------------------------


class UiSession(Base):
    """One browser login, so that logging out can end it.

    The cookie a browser holds is signed and carries an expiry, which is enough
    to tell a forgery from a session this server issued — and not enough to take
    one back. Without a row to delete, "Log out" could only clear the cookie in
    the browser doing the asking: a copy taken off the machine beforehand went on
    working for the rest of its thirty days, and there was nothing anywhere that
    could stop it. This table is that something.

    One row per login rather than one per password, because the two questions
    differ: changing the password invalidates every token at once (the signing
    key is derived from it), while logging out on the laptop should not sign you
    out on the phone.
    """

    __tablename__ = "ui_sessions"

    # The id inside the cookie's signed payload — random, and the only thing that
    # links a cookie to this row. See app/sessions.py.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # Roughly when this session was last used. Written at most once every few
    # minutes (see app/deps.py): it is here so a person can see what is signed
    # in, not as an audit trail worth a write per request.
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


def still_filed():
    """A message still filed somewhere the user hasn't deleted it from.

    Every read path applies this, and they have to apply the same one. The list
    only ever joins non-deleted locations, so the thread view, the single-message
    endpoints and search all need it too — otherwise a message the user emptied
    out of Trash is still there to be opened by id and still comes back in search
    results, which is not what "deleted permanently" means to anyone who pressed
    it. The row survives that keypress by minutes or hours (the placement goes
    at once, the row when the agent's next completed pass collects it — see
    core.ingest.delete_orphan_messages), and this is what makes that interval
    invisible instead of merely unlisted.

    Here rather than in the router that reads it most, because it is a fact about
    the schema and there is now more than one reader: app/routers/messages.py
    calls it `_not_deleted`, and app/threadtext.py needs the same answer before
    it hands a conversation to a language model.
    """
    return select(MessageLocation.id).where(
        MessageLocation.message_pk == Message.id,
        MessageLocation.deleted.is_(False),
    ).exists()


class Setting(Base):
    """App-wide key/value settings — the ones that belong to the install rather
    than to an account (which keeps its own on ``accounts``).

    Deliberately schemaless: these are a handful of strings set from the
    Settings modal, and a table per setting (or a column added for each) buys
    nothing when nothing else joins against them. Values are stored verbatim;
    the router that owns a key is what validates it.
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


# --- Outbound + two-way sync -----------------------------------------------


class PendingAction(Base):
    """Outbox of local changes for the agent to apply to IMAP/SMTP.

    Types: setflags | move | delete | send. Payload carries the specifics
    (e.g. which flags, target folder, or the outbound message id).

    A row leaves this queue by succeeding, and by no other route: ``attempts``
    and ``error`` say how it is going, not whether it is still wanted. The agent
    spaces the retries out from the attempt count (agent/actions.py) and keeps
    going for as long as it takes — days offline, a mail server down all
    weekend. See ``status`` for the one exception, which is historical.
    """

    __tablename__ = "pending_actions"
    __table_args__ = (Index("ix_action_status", "status", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    message_pk: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE")
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Type-specific detail. A send carries the envelope (mail_from, rcpt_to) and
    # the outbound id, and optionally "not_before": an ISO instant before which
    # the agent must not attempt it. See core/outbox.py. A move or a delete
    # carries "op_id" and "undo_from", which are what the Recent actions panel
    # lists and what Undo puts back — see core/undo.py.
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # pending | leased | held | stale | undone | done.
    #
    # "leased" is an agent applying this row right now. It is written and
    # committed before the first SMTP or IMAP command and cleared when the
    # attempt settles, which is what lets the Outbox refuse to cancel or re-queue
    # a send that is already going down the wire (agent/actions.py::_lease).
    #
    # "held" is a send the user cancelled: the agent only selects "pending", so
    # parking a row there stops it going out while keeping the envelope it was
    # built with.
    #
    # "stale" is the one thing that is dropped rather than retried: the folder's
    # UIDVALIDITY changed, so the UID on the row no longer names the message the
    # action was written for and no retry can ever make it again. See
    # agent/actions.py::StaleUid.
    #
    # "undone" is a move the user took back before any agent applied it
    # (app/routers/undo.py). The row is kept rather than deleted so the panel can
    # show the operation as undone instead of it silently vanishing off the list;
    # the agent never selects it again.
    #
    # "error" is written by nothing current: it is what the version with a
    # five-attempt cap left on rows it gave up on, and those rows are still
    # here — agent --requeue-abandoned puts them back.
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # The last failure, kept while the row goes on being retried — not a verdict.
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Outbound(Base):
    """A message being composed/sent (draft -> queued -> sent).

    The server builds the RFC822 MIME (``raw_mime``); a PendingAction of type
    ``send`` tells the agent to relay those bytes via SMTP.

    "queued" is where a message stays until it has actually been sent, however
    long that takes and however many attempts it costs. ``error`` alongside it
    is the last thing that went wrong, not a state — the bytes are still here
    and the agent is still going to send them.
    """

    __tablename__ = "outbound"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # draft | queued | held | sent. "held" is a send that was cancelled before
    # it went out: still the user's mail, still in the Outbox, but with nothing
    # coming for it until they say so. "error" is historical, as on
    # PendingAction.status.
    state: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)

    to_addrs: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    cc_addrs: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    bcc_addrs: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    subject: Mapped[str] = mapped_column(Text, default="", nullable=False)
    body_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    body_html: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # Reply/forward threading headers.
    in_reply_to: Mapped[str | None] = mapped_column(String(998))
    references: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # Local paths of attachments staged for this message.
    attachments: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    raw_mime: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)


# --- Cross-install sync ----------------------------------------------------


class JournalOutbox(Base):
    """A record this install owes the journal server, held until it is taken.

    The same shape of promise as PendingAction and Outbound, for the same
    reason: the thing that has to happen (a reminder set here becoming a
    reminder everywhere) must not depend on a network being up at the moment a
    key was pressed. Setting a reminder writes a row here and returns; the
    journal loop drains it whenever the server is reachable, and a laptop that
    was on a train publishes what it did when it lands.

    Rows are kept after they are sent rather than deleted, because ``seq`` is
    worth having: it is what the log said this record was numbered, and the one
    thing that makes a claim on a due reminder resolvable (see
    app/journal.py::claim). The sweep in the loop drops the old ones.

    ``body`` is the *plain* record — this is our own database, and sealing it
    here would only mean a row we cannot read while debugging. It is sealed on
    the way out.
    """

    __tablename__ = "journal_outbox"
    __table_args__ = (
        # The loop's only query: the unsent ones, oldest first.
        Index("ix_journal_outbox_pending", "status", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    body: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # Told to the server in the clear, and only so that it can ever delete
    # anything — it cannot read a record to discover that a later one restates
    # it. See journal/server.py::_prune.
    snapshot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # pending | sent. Nothing else: a record that could not be posted is still
    # wanted, exactly as a PendingAction that could not be applied is, and the
    # reason lives in ``error`` while it goes on being retried.
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    # What the server numbered it. NULL until it has been taken.
    seq: Mapped[int | None] = mapped_column(BigInteger)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)


class JournalDeferred(Base):
    """A record from the journal that this install cannot apply *yet*.

    The common case is not an error and not rare: a reminder set on the laptop
    names a conversation by its Message-ID, and the desktop has not finished
    syncing that mail. There is nothing wrong with either machine — one is simply
    ahead — and the record has to wait rather than be dropped, because dropping
    it means the desktop never learns about that reminder at all.

    It cannot wait by stalling the cursor: everything appended after it would
    wait too, and one message that never arrives (deleted on the server before
    this install ever saw it) would freeze the whole log. So the cursor moves on
    and the record is parked here, retried on every pass, and given up on after
    ``MAX_TRIES`` — by which point the mail it names is not coming.
    """

    __tablename__ = "journal_deferred"
    __table_args__ = (Index("ix_journal_deferred_seq", "seq"),)

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    record: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    tries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
