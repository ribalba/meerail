"""The Outbox: mail this app has written and no mail server has taken yet.

The app cannot send. It builds the message, writes it down and hands it to the
agent, which relays it over SMTP whenever it next reaches a server — a second
on a working setup, days on a laptop that is shut. Until this folder existed
the difference between those two was invisible: a message queued against a
wrong SMTP port sat in exactly the same silence as one already delivered, and
the reported symptom was mail in `outbound` with "no way to know that they were
there and why they are not being sent".

So this endpoint answers both halves of that sentence. The list is what is
still waiting; every row carries the last error, how many attempts it has cost,
and when the next one is due. Nothing here is a lost message — the agent
retries forever (agent/actions.py) — so the wording throughout the UI is "not
sent yet", never "not sent".

Two verbs beyond looking, because looking at a message that will not go is only
half of what anyone wants:

  * **try now** clears the backoff, for the moment right after you fixed the
    port and do not want to wait out fifteen minutes to find out whether that
    was it;
  * **delete** is the only way to take a message back out of the queue. The
    agent will not do it at any number of failures, which is right — but it
    does mean a message addressed to a domain that no longer resolves would
    otherwise be retried until the end of time.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession, defer

from core import outbox as outbox_core
from core.database import get_db
from core.events import publish_command
from core.models import Account, Outbound, PendingAction, utcnow
from .. import events
from ..deps import require_ui_auth

router = APIRouter(prefix="/api/outbox", tags=["outbox"], dependencies=[Depends(require_ui_auth)])

# Enough of the body to tell two queued messages apart in the list, and no more.
SNIPPET = 200


def _row(row, action: PendingAction | None, account: Account | None) -> dict:
    attempts = action.attempts if action else 0
    # The action's updated_at is when its last attempt settled — _settle writes
    # it on every pass through, successful or not.
    last_attempt = action.updated_at if action and attempts else None
    body = (row.body_text or "").strip().replace("\n", " ")
    return {
        "id": row.id,
        "account_id": row.account_id,
        "account_email": account.email if account else "",
        "account_label": (account.label or account.email) if account else "",
        "account_color": account.color if account else "",
        "state": row.state,
        "to": list(row.to_addrs or []),
        "cc": list(row.cc_addrs or []),
        "bcc": list(row.bcc_addrs or []),
        "subject": row.subject or "",
        "snippet": body[:SNIPPET],
        "attachment_count": len(row.attachments or []),
        "created_at": row.created_at,
        # The last failure, kept while the message goes on being retried — not a
        # verdict on it. This is the "why" the folder exists to answer.
        "error": row.error,
        "attempts": attempts,
        "last_attempt_at": last_attempt,
        "next_attempt_at": outbox_core.next_attempt_at(attempts, last_attempt),
        # False means the queue row is gone: either an older agent retired it
        # (state "error") or something removed it. The message is still here and
        # "Try now" puts it back.
        "queued": action is not None,
    }


def _accounts(db: DBSession) -> dict[int, Account]:
    return {a.id: a for a in db.execute(select(Account)).scalars().all()}


@router.get("")
def list_outbox(db: DBSession = Depends(get_db)) -> dict:
    """Everything waiting to go out, oldest first — the agent's own order."""
    rows = outbox_core.unsent(db)
    actions = outbox_core.send_actions(db, [r.id for r in rows])
    accounts = _accounts(db)
    out = [_row(r, actions.get(r.id), accounts.get(r.account_id)) for r in rows]
    return {
        "rows": out,
        "total": len(out),
        # One flag so the sidebar does not have to re-derive "is something
        # actually wrong here" from the rows on every render.
        "failing": sum(1 for r in out if r["error"]),
    }


@router.get("/{outbound_id}")
def get_outbound(outbound_id: int, db: DBSession = Depends(get_db)) -> dict:
    """One queued message in full: its body, and the state of its delivery.

    raw_mime is deliberately never returned — it is the same message with the
    attachments base64'd into it, and the reader already has everything it can
    display. Its size is, because "why is this one still going" is often
    answered by the number.
    """
    row = db.execute(
        select(*outbox_core.UNSENT_COLUMNS, Outbound.body_html,
               func.length(Outbound.raw_mime).label("mime_len"))
        .where(Outbound.id == outbound_id,
               Outbound.state.in_(outbox_core.UNSENT_STATES))
    ).first()
    # 404 also covers the message that went out while this screen was open: it
    # is no longer the outbox's to show, and the UI says so rather than leaving
    # a stale copy of a delivered message on display.
    if row is None:
        raise HTTPException(status_code=404, detail="Not in the outbox")

    action = outbox_core.send_actions(db, [row.id]).get(row.id)
    detail = _row(row, action, db.get(Account, row.account_id))
    detail.update({
        "body_text": row.body_text or "",
        # Whether it goes out as HTML, not the markup itself: the composer's own
        # text is what the author wrote, and rendering the HTML here would mean
        # sanitizing outgoing mail as if it had arrived from a stranger.
        "html": bool((row.body_html or "").strip()),
        "size_bytes": int(row.mime_len or 0),
        # The envelope the agent will actually use. Worth showing because a
        # rejected sender is one of the two failures that are about the message
        # rather than the connection (see log.hint).
        "from_address": (action.payload or {}).get("mail_from") if action else None,
    })
    return detail


def _load(db: DBSession, outbound_id: int) -> Outbound:
    """The row for the two verbs below, without its raw MIME.

    Deferred rather than plainly loaded for the reason Message.raw_mime is
    deferred on the model: the bytes carry the attachments, and neither pressing
    "try now" nor discarding a message needs to read a video out of Postgres to
    do it.
    """
    row = db.execute(
        select(Outbound).options(defer(Outbound.raw_mime)).where(Outbound.id == outbound_id)
    ).scalars().first()
    if row is None or row.state not in outbox_core.UNSENT_STATES:
        raise HTTPException(status_code=404, detail="Not in the outbox")
    return row


@router.post("/{outbound_id}/retry")
def retry(outbound_id: int, db: DBSession = Depends(get_db)) -> dict:
    """Try this message again now, instead of at the end of its backoff.

    The attempt count is deliberately kept: it is the record of how long this
    has been failing, and zeroing it would make a message that has failed forty
    times look freshly queued. Only the clock the backoff is measured against is
    moved, which is what "now" means here.
    """
    row = _load(db, outbound_id)
    action = outbox_core.send_actions(db, [row.id]).get(row.id)

    if action is None:
        # No queue row: this is mail an older agent retired (state "error"), or
        # a queue row that was removed under it. The bytes are still here, so
        # put the send back rather than making the user rewrite the message.
        # The envelope is rebuilt from the addresses the message was composed
        # with; the sender falls back to the account's primary address, which is
        # the same default /send would have picked.
        account = db.get(Account, row.account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="Account not found")
        action = PendingAction(
            account_id=row.account_id, type="send",
            payload={"outbound_id": row.id, "mail_from": account.email,
                     "rcpt_to": outbox_core.recipients(row)},
        )
        db.add(action)
    else:
        action.status = "pending"
        # Explicit, and well past RETRY_CEILING: _due compares this against the
        # backoff, so dating it back is exactly "this action is due".
        action.updated_at = utcnow() - timedelta(seconds=outbox_core.RETRY_CEILING * 2)

    row.state = "queued"
    db.commit()

    events.publish({"type": "outbox", "retry": 1})
    # And ask the agent to go now rather than at the end of its poll interval —
    # the whole point of this button is not waiting.
    account = db.get(Account, row.account_id)
    publish_command({"type": "refresh", "email": account.email if account else None})
    return {"id": row.id, "state": row.state, "attempts": action.attempts}


@router.delete("/{outbound_id}", status_code=204)
def discard(outbound_id: int, db: DBSession = Depends(get_db)):
    """Take a message back out of the queue, for good.

    Nothing else in meerail deletes queued mail: the agent will not, at any
    number of failures, because a message the user asked to send is theirs and
    not the agent's to throw away. That leaves this as the only way out for the
    message nobody wants any more — the typo'd address, the mail that has been
    retrying at a domain that no longer exists. Asked for explicitly, from a
    screen that is showing the message, which is the difference between this and
    an attempt cap.
    """
    row = _load(db, outbound_id)
    # The queue row goes with it, or the agent would keep trying to send a
    # message that no longer exists. Only the live ones are looked at: a
    # finished send is history, and every one this install has ever made is
    # still in that table.
    action = outbox_core.send_actions(db, [row.id]).get(row.id)
    if action is not None:
        db.delete(action)
    db.delete(row)
    db.commit()
    events.publish({"type": "outbox", "discarded": 1})
    return None
