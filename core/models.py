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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    # Naive UTC everywhere internally; tz-aware input is normalized at the edges.
    return datetime.now(timezone.utc).replace(tzinfo=None)


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

    # Extra "send as" addresses for this account (Proton lets one account own
    # several addresses/aliases). Declared in the agent config and reported on
    # sync; the primary ``email`` is always a valid sender regardless of this.
    send_addresses: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    # Signature/disclaimer appended to every message sent from this account.
    # Empty disables it. Composition is the web app's job, so unlike the sync
    # settings above this is set in the UI, not the agent config.
    footer: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # Sync status (denormalized for the UI).
    backfill_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_agent_seen: Mapped[datetime | None] = mapped_column(DateTime)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime)

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

    unread_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    subscribed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

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
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    snippet: Mapped[str] = mapped_column(Text, default="", nullable=False)
    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # The original RFC822 bytes. Stored in the DB so the ingesting agent and the
    # serving web app share no filesystem — the DB is the only handoff.
    raw_mime: Mapped[bytes | None] = mapped_column(LargeBinary)
    body_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    body_html: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Concatenation indexed for regex/keyword search.
    search_text: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # Rollup of attachment text extraction: none | pending | done | error
    extract_status: Mapped[str] = mapped_column(String(16), default="none", nullable=False)

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


# --- Outbound + two-way sync -----------------------------------------------


class PendingAction(Base):
    """Outbox of local changes for the agent to apply to IMAP/SMTP.

    Types: setflags | move | delete | send. Payload carries the specifics
    (e.g. which flags, target folder, or the outbound message id).
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
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # pending | leased | done | error
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Outbound(Base):
    """A message being composed/sent (draft -> queued -> sent).

    The server builds the RFC822 MIME (``raw_mime``); a PendingAction of type
    ``send`` tells the agent to relay those bytes via SMTP.
    """

    __tablename__ = "outbound"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # draft | queued | sent | error
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
