"""What "written here but not yet sent" means, for both halves of meerail.

Sending is a two-process affair: the server builds the message and writes an
``Outbound`` row plus a ``send`` PendingAction, and the agent relays it over
SMTP the next time it can reach a mail server. Everything about the wait — how
long until the next attempt, what went wrong on the last one, which messages
are still stuck — is therefore knowledge the two processes have to share, and
neither owns.

Before this module the retry cadence lived only in ``agent/actions.py``, where
the server could not see it, so the app could say "2 messages waiting" and
nothing more: not when they would next be tried, not what the last error was,
not which message it belonged to. The Outbox folder (``app/routers/outbox.py``)
and the agent's own log lines are the same facts read from opposite ends, and
this is the one place they are defined.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from .models import Outbound, PendingAction

# --- Retry cadence ---------------------------------------------------------
#
# How long after a failure an action may be tried again: doubling from
# RETRY_BASE up to RETRY_CEILING, from the attempt count the row already
# carries.
#
# Not politeness towards the server — every failed send costs the sync pass its
# full SMTP timeout (a minute by default), at the head of the pass, before any
# mail is fetched. Retried every pass, one message with a wrong port would slow
# every sync for as long as it stayed wrong, which is how issue #7 read in the
# log: passes of 65s and 180s where a healthy one takes half a second. Backing
# off keeps a permanently-failing action at a cost that tends to nothing while
# still retrying it forever.
#
# The ceiling is deliberately short. An hour would be tidier arithmetic, but the
# case that matters is the laptop that has just come back online after four days
# — everything in the queue is long overdue by then, so the ceiling only ever
# decides how long the *last* stretch of a broken spell lasts once it is fixed.
RETRY_BASE = 60          # seconds, after the first failure
RETRY_CEILING = 900      # 15 minutes
RETRY_DOUBLINGS = 8      # 60s * 2**8 is well past the ceiling; keeps it finite

# The states an Outbound row can be in while the message is still the user's to
# worry about. "sent" is gone from here; "draft" is not queued for anything.
# "error" is written by no current version — it is what the agent's old
# five-attempt cap left behind — but those rows are still unsent mail and the
# Outbox has to show them.
UNSENT_STATES = ("queued", "error")


def retry_delay(attempts: int) -> timedelta:
    """How long an action that has failed ``attempts`` times waits before the
    next try. Doubles from RETRY_BASE, flat at RETRY_CEILING thereafter."""
    doublings = min(max(attempts - 1, 0), RETRY_DOUBLINGS)
    return timedelta(seconds=min(RETRY_BASE * 2 ** doublings, RETRY_CEILING))


def next_attempt_at(attempts: int, last_attempt: datetime | None) -> datetime | None:
    """When the agent may try this action again, or None if it is due now.

    Mirrors ``_due`` in agent/actions.py: a row that has never been tried is due
    immediately, and ``last_attempt`` is when the previous attempt settled.
    """
    if not attempts or last_attempt is None:
        return None
    return last_attempt + retry_delay(attempts)


# --- Queries ---------------------------------------------------------------

# Everything the Outbox needs about a message, minus the two columns that can
# be enormous. ``raw_mime`` carries the attachments base64-encoded, so five
# queued messages with a video between them is a hundred megabytes that the
# sidebar's count would otherwise drag out of Postgres every few seconds.
UNSENT_COLUMNS = (
    Outbound.id,
    Outbound.account_id,
    Outbound.state,
    Outbound.to_addrs,
    Outbound.cc_addrs,
    Outbound.bcc_addrs,
    Outbound.subject,
    Outbound.body_text,
    Outbound.attachments,
    Outbound.error,
    Outbound.created_at,
    Outbound.updated_at,
)


def unsent(db, account_id: int | None = None) -> list:
    """Every message written here and not yet handed to a mail server, oldest
    first — the queue in the order the agent will work through it."""
    q = select(*UNSENT_COLUMNS).where(Outbound.state.in_(UNSENT_STATES))
    if account_id is not None:
        q = q.where(Outbound.account_id == account_id)
    return db.execute(q.order_by(Outbound.created_at, Outbound.id)).all()


def send_actions(db, outbound_ids: list[int]) -> dict[int, PendingAction]:
    """The queue rows driving those sends, keyed by outbound id.

    An Outbound row is the message; the PendingAction beside it is the attempt
    to deliver it, and it is the one that knows how many tries it has had and
    when the next one is due. Matched in Python rather than with a JSONB
    predicate because the pending queue is small by construction — everything in
    it is either about to succeed or is being reported as stuck.
    """
    if not outbound_ids:
        return {}
    wanted = set(outbound_ids)
    rows = db.execute(
        select(PendingAction).where(PendingAction.type == "send",
                                    PendingAction.status != "done")
    ).scalars().all()
    found: dict[int, PendingAction] = {}
    for action in rows:
        oid = (action.payload or {}).get("outbound_id")
        if oid in wanted:
            found[oid] = action
    return found


def recipients(row) -> list[str]:
    """To + Cc + Bcc, in the order a reader expects to see them."""
    return [a for group in (row.to_addrs, row.cc_addrs, row.bcc_addrs)
            for a in (group or [])]


def describe(row) -> str:
    """One line naming a stuck message the way its author would: who it is for
    and what it says, not the primary key of a queue row."""
    to = ", ".join(recipients(row)) or "(no recipients)"
    subject = (row.subject or "").strip() or "(no subject)"
    return f"to {to} — {subject}"
