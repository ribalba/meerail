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

Three verbs beyond looking, because looking at a message that will not go is
only half of what anyone wants:

  * **try now / send now** clears both clocks — the backoff, for the moment
    right after you fixed the port and do not want to wait out fifteen minutes
    to find out whether that was it, and the send delay, for the message you
    have decided is fine after all;
  * **cancel** parks a send that has not happened yet. The message stays here,
    with its envelope, and goes nowhere until it is sent by hand;
  * **delete** is the only way to take a message back out of the queue for
    good. The agent will not do it at any number of failures, which is right —
    but it does mean a message addressed to a domain that no longer resolves
    would otherwise be retried until the end of time.

The delay those first two are mostly about is `server.send_delay_seconds`,
overridable from the Settings modal through /api/outbox/settings: the window in
which a sent message is still recallable. What it actually does is written on
the queue row, in core/outbox.py.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession, defer

from core import outbox as outbox_core
from core.database import get_db
from core.events import publish_command
from core.models import Account, Outbound, PendingAction, Setting, utcnow
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

    # Two clocks, as in the agent's _due: the backoff after a failure, and the
    # delay a send was given when it was written. The row shows the later of
    # them, because that is when the message actually moves — but `send_at` is
    # reported separately, since "waiting because it was told to" and "waiting
    # because it failed" are not the same news.
    send_at = outbox_core.not_before(action)
    next_at = outbox_core.next_attempt_at(attempts, last_attempt)
    if send_at is not None and (next_at is None or send_at > next_at):
        next_at = send_at
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
        "next_attempt_at": next_at,
        # When the agent is allowed to send this at the earliest, for a message
        # being held back on purpose. None on everything else, which is what the
        # UI reads to tell "waiting to go" from "not going until then".
        "send_at": send_at,
        # A cancelled send: no queue row, no clock, and nothing will happen to
        # it until someone presses Send now. Distinct from `queued` below, which
        # is also false for the mail an old agent retired.
        "held": row.state == "held",
        # An agent has this row and is inside the SMTP conversation for it right
        # now (agent/actions.py::_lease). The UI greys out Cancel and Send now
        # while it is true, because by this point neither is honest any more —
        # and the endpoints below refuse them regardless of what the screen says.
        "sending": bool(action is not None and action.status == "leased"),
        # This one went out and the server never acknowledged it, so nobody can
        # say whether it arrived (agent/actions.py::_settle_unknown). It is
        # parked in the same state as a cancelled send and means the opposite of
        # one, which is why the UI has to be able to tell them apart.
        "delivery_unknown": bool(action is not None
                                 and (action.payload or {}).get("delivery_unknown")),
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


# Declared before /{outbound_id} so the path is read as a word rather than as a
# malformed message id.
@router.get("/settings")
def outbox_settings(db: DBSession = Depends(get_db)) -> dict:
    """How long a newly written message waits here before it may be sent."""
    return {"send_delay_seconds": outbox_core.send_delay(db),
            "max_delay_seconds": outbox_core.MAX_DELAY}


class DelaySetting(BaseModel):
    send_delay_seconds: int


@router.put("/settings")
def set_outbox_settings(body: DelaySetting, db: DBSession = Depends(get_db)) -> dict:
    """Set the delay, for messages written from now on.

    Deliberately not retroactive: the queue holds messages whose author already
    watched a countdown start, and moving that deadline underneath them — in
    either direction — is not something a settings field should do. Each of them
    can still be sent or cancelled by hand.
    """
    seconds = body.send_delay_seconds
    if seconds < 0 or seconds > outbox_core.MAX_DELAY:
        raise HTTPException(status_code=400,
                            detail=f"Delay must be between 0 and {outbox_core.MAX_DELAY} seconds")
    row = db.get(Setting, outbox_core.SETTING_KEY)
    if row is None:
        db.add(Setting(key=outbox_core.SETTING_KEY, value=str(seconds)))
    else:
        row.value = str(seconds)
    db.commit()
    return {"send_delay_seconds": seconds}


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


def _claim(db: DBSession, row: Outbound, verb: str) -> PendingAction | None:
    """The queue row for this message, held for the rest of this request — or a
    409 if the agent is sending it as we speak, or has just finished.

    The three verbs below all rewrite a send that the agent may be in the middle
    of. Until the lease existed they did it on a row they had only read: the
    agent picked up a "pending" send, spent a minute inside SMTP, and wrote its
    result at the end, so for that whole minute the row still said "pending" and
    the Outbox was happy to cancel it, re-queue it, or delete it. Cancel then
    reported success for a message that had already been delivered — the one
    outcome the undo window exists to prevent — and Send now re-queued mail that
    was mid-flight, which is how one message arrives twice.

    Three things close that. The lock makes this request queue up behind the
    agent's own claim rather than reading around it, so what comes back is the
    row's settled state and not a guess. "leased" is a state this refuses
    outright. And the *finished* state is refused too, which is the half a lock
    alone could not fix: the Outbound row these verbs are reached through was
    read before the lock was taken, so a send that succeeded in between leaves a
    request holding a row that says "queued" for a message that has gone. Without
    this it would find no live queue row, read that as "never queued", and put
    the send back — delivering it a second time.

    So both the row and its queue are re-read here, inside the lock, and anything
    that has moved on is an error rather than an action. There is no window left
    in which "cancelled" can mean "already sent".
    """
    actions = outbox_core.send_actions_for(db, row.id, lock=True)
    if any(a.status == "leased" for a in actions):
        raise HTTPException(
            status_code=409,
            detail=f"This message is being sent right now — it is too late to {verb} it.")
    live = [a for a in actions if a.status != "done"]
    # Re-read rather than trusting `row`: it was loaded before the lock, and this
    # is the same question asked at the only moment it can be answered.
    state = db.scalar(select(Outbound.state).where(Outbound.id == row.id))
    if not live and (actions or state not in outbox_core.UNSENT_STATES):
        raise HTTPException(
            status_code=409,
            detail=f"This message has already been sent — it is too late to {verb} it.")
    return live[0] if live else None


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
    """Send this now: at the end of neither its backoff nor its delay.

    One verb for three situations that all come down to "stop waiting" — a
    message that has been failing and has just had its cause fixed, one sitting
    out its send delay that the author has decided is fine after all, and one
    they cancelled and want back. The button says "Try now" for the first and
    "Send now" for the others, because those are different sentences to a reader
    even though they are the same instruction to the agent.

    The attempt count is deliberately kept: it is the record of how long this
    has been failing, and zeroing it would make a message that has failed forty
    times look freshly queued. Only the clocks are moved, which is what "now"
    means here.
    """
    row = _load(db, outbound_id)
    action = _claim(db, row, "re-queue")

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
        # And the other clock the agent checks. Replaced rather than mutated in
        # place: payload is a plain JSONB column, so SQLAlchemy only notices a
        # new dict — an edit to the existing one would be committed as nothing
        # at all, and the message would go on waiting with the UI saying it had
        # been released.
        if "not_before" in (action.payload or {}):
            action.payload = {k: v for k, v in action.payload.items() if k != "not_before"}

    row.state = "queued"
    db.commit()

    events.publish({"type": "outbox", "retry": 1})
    # And ask the agent to go now rather than at the end of its poll interval —
    # the whole point of this button is not waiting.
    account = db.get(Account, row.account_id)
    publish_command({"type": "refresh", "email": account.email if account else None})
    return {"id": row.id, "state": row.state, "attempts": action.attempts}


@router.post("/{outbound_id}/cancel")
def cancel(outbound_id: int, db: DBSession = Depends(get_db)) -> dict:
    """Stop this message going out, without throwing it away.

    The middle ground the delay exists to create: the second thought arrives
    before the send does, and what the author wants is not "delete it forever"
    but "not yet". So the queue row is parked rather than deleted — status
    "held", which drain_actions does not select — and the message stays in the
    Outbox with its envelope intact, including the alias it was to be sent from,
    which a rebuilt action could not know. Send now puts it straight back.

    Cancelling a send that has already failed a few times is allowed and means
    the same thing: stop trying until I say so. The attempt count survives, so
    the history of what went wrong is not lost by pausing it.

    Cancelling one an agent has already picked up is not allowed, because by
    then it is not true — see _claim.
    """
    row = _load(db, outbound_id)
    action = _claim(db, row, "cancel")
    if row.state == "held":
        # Already cancelled — from another window, or a double-click. Nothing to
        # do and nothing wrong: report the state rather than erroring at someone
        # who wants exactly what they already have.
        return {"id": row.id, "state": row.state, "held": True}

    if action is not None:
        action.status = "held"
    row.state = "held"
    db.commit()
    events.publish({"type": "outbox", "cancelled": 1})
    return {"id": row.id, "state": row.state, "held": True}


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
    action = _claim(db, row, "discard")
    if action is not None:
        db.delete(action)
    db.delete(row)
    db.commit()
    events.publish({"type": "outbox", "discarded": 1})
    return None
