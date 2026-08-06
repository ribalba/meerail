from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session as DBSession

from core import ingest
from core.database import get_db
from core.events import publish_command
from core.mail.store import STUCK_AFTER
from ..deps import require_ui_auth
from ..syncstate import account_state
from .actions import _archive_mailbox

# What the "Create an Archive folder" button asks the agent for. A bare leaf:
# where a user folder is allowed to live is the server's business and only the
# agent can see it, so Bridge's "Folders/" namespace is applied there
# (agent/actions.py::apply_action). The name is what makes it work afterwards —
# core.ingest.derive_role reads "Archive" as the archive role even from a server
# that publishes no \Archive flag for it.
ARCHIVE_FOLDER_NAME = "Archive"
from core.models import (Account, Attachment, Mailbox, Message, Outbound, PendingAction,
                         Setting, utcnow)

router = APIRouter(prefix="/api/sync", tags=["sync"], dependencies=[Depends(require_ui_auth)])


@router.post("/refresh")
def request_refresh(email: str | None = None):
    """Ask the agent to sync now instead of waiting out its poll interval.

    The agent owns the IMAP connection and the server never has one, so this can
    only ever be a request. It is not an error for no agent to be listening —
    the UI reloads from the database either way, which is all it could show.
    """
    publish_command({"type": "refresh", "email": email})
    return {"requested": True}


@router.post("/recheck")
def request_recheck(email: str | None = None, db: DBSession = Depends(get_db)):
    """Ask the agent to re-walk every folder from the start, not just the new mail.

    The repair button. Normal syncing only ever looks above each folder's UID
    cursor, so anything lost or corrupted below it stays lost however many times
    you press refresh — this rewinds the cursors so the next pass sees the whole
    mailbox again. Re-ingest is idempotent, so nothing gets duplicated.

    Unlike /refresh this is written to the database rather than sent as a
    notification: it is the button you reach for when the agent looks unhealthy,
    so it has to keep until an agent is actually there to serve it. The NOTIFY
    afterwards is only an optimisation — it saves waiting out the poll interval
    if the agent happens to be listening right now.
    """
    flagged = ingest.request_recheck(db, email)
    db.commit()
    if not flagged:
        raise HTTPException(404, "No such account")
    publish_command({"type": "refresh", "email": email})
    return {"requested": True, "accounts": flagged}


@router.get("/status")
def sync_status(db: DBSession = Depends(get_db)):
    """Per-account agent health and ingest stats, for the UI's status modal.

    The agent never talks to the server (see docker-compose.yml) — everything
    here is inferred from what it writes to the database as it works.
    """
    now = utcnow()
    hour_ago = now - timedelta(hours=1)
    day_ago = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)

    accounts = db.execute(select(Account).order_by(Account.created_at)).scalars().all()
    out = []
    for acc in accounts:
        mailboxes = db.execute(
            select(Mailbox).where(Mailbox.account_id == acc.id).order_by(Mailbox.sort_order, Mailbox.imap_name)
        ).scalars().all()

        # One pass over the ingest-time index for every counter we show.
        stored = db.execute(
            select(
                func.count(Message.id),
                func.count(Message.id).filter(Message.created_at >= hour_ago),
                func.count(Message.id).filter(Message.created_at >= day_ago),
                func.count(Message.id).filter(Message.created_at >= week_ago),
                func.max(Message.created_at),
            ).where(Message.account_id == acc.id)
        ).one()
        total_stored, last_hour, last_day, last_week, last_ingest = stored

        state, detail = account_state(acc, last_ingest, now)
        out.append({
            "account_id": acc.id,
            "email": acc.email,
            "label": acc.label,
            "backfill_complete": acc.backfill_complete,
            "last_agent_seen": acc.last_agent_seen,
            "last_sync_at": acc.last_sync_at,
            "last_message_at": last_ingest,
            "last_error": acc.last_error,
            "last_error_at": acc.last_error_at,
            "recheck_requested": acc.recheck_requested,
            "recheck_requested_at": acc.recheck_requested_at,
            "sync_progress": acc.sync_progress,
            "state": state,
            "state_detail": detail,
            "stored_total": total_stored,
            "stored_last_hour": last_hour,
            "stored_last_day": last_day,
            "stored_last_week": last_week,
            "total": sum(m.total_count for m in mailboxes),
            "unread": sum(m.unread_count for m in mailboxes if m.role == "inbox"),
            "mailbox_count": len(mailboxes),
            "mailboxes": [
                {"id": m.id, "imap_name": m.imap_name, "display_name": m.display_name,
                 "role": m.role, "unread": m.unread_count, "total": m.total_count,
                 "last_uid": m.last_uid}
                for m in mailboxes
            ],
        })
    # A single flag saves every caller from re-deriving "is anything wrong".
    return {
        "accounts": out,
        "healthy": all(a["state"] in ("ok", "backfilling") for a in out),
        "indexing": _indexing_status(db),
        "outbox": _outbox_status(db),
        "actions": _actions_status(db, now),
    }


# How long a dropped action stays worth mentioning. The row is never deleted —
# nothing prunes the queue — so an all-time count would only ever go up and the
# warning it drives would never clear. A day is long enough to be seen by
# somebody who was not at the screen when it happened.
_DROPPED_WINDOW = timedelta(hours=24)

# When the dropped-actions notice was last dismissed, ISO-8601.
#
# A window alone is not enough, because this notice reports something that has
# already finished happening. Unlike a stuck queue — which stops being true the
# moment the queue drains — a refusal is a fact about the past, so the notice
# stands for a full day whatever the user does about it, including fixing the
# cause. Read once, understood, and then unclearable for another twenty hours is
# how a warning becomes furniture.
#
# A timestamp rather than the obvious high-water action id: a row that is still
# queued now keeps its id when it fails later, so an id watermark would silently
# swallow the next failure. ``updated_at`` is written by whichever settle wrote
# the status (agent/actions.py::_settle_refused), so a later failure always
# sorts after a dismissal and a past one never does.
_DISMISSED_KEY = "dropped_actions_dismissed_at"


def _dismissed_at(db) -> datetime | None:
    row = db.get(Setting, _DISMISSED_KEY)
    if row is None or not row.value:
        return None
    try:
        return datetime.fromisoformat(row.value)
    except ValueError:
        return None


def _actions_status(db, now) -> dict:
    """Changes to folders and flags that are not reaching the server.

    The counterpart to _outbox_status, for everything that is not a send. A
    queued move or flag change has no folder of its own to sit in and no row
    anywhere in the UI — the message it belongs to is already showing the change,
    optimistically — so a queue that stops draining was, until this, visible only
    in the agent's log. That is the shape of the failure this exists for: two
    archived messages that the app had filed, the server had not, and nothing on
    screen disagreed with for a day.

    Two numbers, because they need different sentences. `stuck` is still being
    retried and may yet land — it is only counted once the retries have gone on
    long enough that a passing outage no longer explains them (STUCK_AFTER, the
    same point at which the local view stops taking the move on trust). `dropped`
    did not happen and never will: the folder was rebuilt under it, or the server
    refused the destination outright.
    """
    is_stuck = (PendingAction.type != "send",
                PendingAction.status.in_(("pending", "leased")),
                PendingAction.attempts >= STUCK_AFTER)
    stuck, oldest = db.execute(
        select(func.count(PendingAction.id), func.min(PendingAction.created_at))
        .where(*is_stuck)
    ).one()
    # The oldest one's reason, not an arbitrary one: it is the failure that has
    # been going on longest, and the rest are nearly always the same sentence.
    error = db.scalar(
        select(PendingAction.error).where(*is_stuck, PendingAction.error.is_not(None))
        .order_by(PendingAction.created_at).limit(1)
    ) if stuck else None

    since = now - _DROPPED_WINDOW
    dismissed = _dismissed_at(db)
    if dismissed is not None and dismissed > since:
        since = dismissed
    was_dropped = (PendingAction.status.in_(("stale", "refused")),
                   PendingAction.updated_at > since)
    # Grouped, not counted: the two are dropped for opposite reasons and want
    # opposite advice. A refusal is a fault in the account's setup and will
    # happen again until it is fixed; a stale UID is a folder that was rebuilt
    # underneath a queued change, where doing it again is the whole of the fix.
    # Telling the user which of the two they are looking at is the difference
    # between a warning they can act on and one they can only read.
    by_status = dict(db.execute(
        select(PendingAction.status, func.count(PendingAction.id))
        .where(*was_dropped).group_by(PendingAction.status)
    ).all())
    dropped = sum(by_status.values())
    kind = (list(by_status)[0] if len(by_status) == 1
            else "mixed" if by_status else None)
    # The newest here, where the oldest was right above: a drop is a finished
    # event rather than an ongoing one, and the last thing to happen is the one
    # somebody coming to the screen is looking for.
    #
    # A refusal first, though, whenever the window holds both. It is the half
    # with something to be done about it — the button below the notice is aimed
    # at exactly this row — and a mixed window that happened to end on a stale
    # UID printed "the folder was rebuilt" above a button offering to create an
    # Archive folder, which is two unrelated faults reading as one.
    dropped_error = db.scalar(
        select(PendingAction.error).where(*was_dropped, PendingAction.error.is_not(None))
        .order_by((PendingAction.status == "refused").desc(),
                  PendingAction.updated_at.desc())
        .limit(1)
    ) if dropped else None

    return {"stuck": int(stuck or 0), "dropped": int(dropped),
            "dropped_kind": kind, "error": error, "dropped_error": dropped_error,
            "dropped_fix": _dropped_fix(db, was_dropped) if dropped else None,
            "oldest_at": oldest}


def _dropped_fix(db, was_dropped) -> dict | None:
    """The account this notice can be repaired for, and how — or None.

    Every refusal the \\All path produces reads the same on screen, and until
    this they all carried the same advice: create an Archive folder. That advice
    is only right for the account that has not got one. Said to an account that
    *has* — where the refusal came from somewhere else aiming at \\All, an undo's
    reverse move being the way it happened here — it sends the user to build a
    folder they already have, and the notice still will not clear afterwards.

    So the question is asked of the account rather than of the message: is there
    anywhere for this account to archive to? Only when the answer is no is there
    a button that changes anything, and then it is the one the sentence has been
    describing in prose all along. Everything else gets Dismiss and nothing else,
    which is the honest offer for a record of something already over.
    """
    account_ids = db.execute(
        select(PendingAction.account_id)
        .join(Mailbox, and_(Mailbox.account_id == PendingAction.account_id,
                            Mailbox.imap_name == PendingAction.payload["to_folder"].astext))
        .where(*was_dropped, PendingAction.status == "refused", Mailbox.role == "all")
        .distinct()
    ).scalars().all()
    for account_id in account_ids:
        if _archive_mailbox(db, account_id) is not None:
            continue
        account = db.get(Account, account_id)
        if account is None:
            continue
        return {"kind": "create_archive", "account_id": account_id,
                "email": account.email, "label": account.label or account.email,
                "name": ARCHIVE_FOLDER_NAME}
    return None


@router.post("/actions/dismiss")
def dismiss_dropped(db: DBSession = Depends(get_db)):
    """Clear the "changes could not be made" notice.

    Not a way of hiding a fault: nothing here is still going to be retried, and
    nothing is waiting on being acknowledged. The notice reports a thing that has
    finished happening, so the only state left to change is whether the user has
    read it — and without this the answer was "for the next twenty-four hours,
    no", including for the user who has just fixed the cause.

    Anything that fails *after* this press is stamped with a later ``updated_at``
    by the settle that drops it, so it comes back on its own. See _DISMISSED_KEY.
    """
    stamp = utcnow().isoformat()
    row = db.get(Setting, _DISMISSED_KEY)
    if row is None:
        db.add(Setting(key=_DISMISSED_KEY, value=stamp))
    else:
        row.value = stamp
    db.commit()
    return {"ok": True, "dismissed_at": stamp}


@router.post("/actions/create-archive")
def create_archive_folder(account_id: int, db: DBSession = Depends(get_db)):
    """Make the Archive folder whose absence the notice is about.

    The button behind the sentence this has been printing all along ("create one
    on the server, or mark an existing one \\Archive, and sync again"), which
    until now was a thing to go and do in another program — Proton's web UI, or
    Thunderbird — with no way back here to say it was done.

    Queued rather than done: only the agent holds an IMAP connection, and only
    the agent can see which namespace a server allows user folders in. The
    Mailbox row is deliberately not written here either — prune_mailboxes would
    delete an optimistic one on the very pass meant to confirm it. See
    mailboxes.create_mailbox, which this is the one-click form of.
    """
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    existing = _archive_mailbox(db, account_id)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"This account already archives into "
                   f"{existing.display_name or existing.imap_name}.")
    # A second press before the agent's next pass would otherwise queue a second
    # CREATE. "leased" counts as queued — that is a row being acted on right now.
    queued = db.execute(
        select(PendingAction).where(
            PendingAction.account_id == account_id,
            PendingAction.type == "create_folder",
            PendingAction.status.in_(("pending", "leased")))
    ).scalars().all()
    if not any((a.payload or {}).get("name") == ARCHIVE_FOLDER_NAME for a in queued):
        db.add(PendingAction(account_id=account_id, message_pk=None,
                             type="create_folder",
                             payload={"name": ARCHIVE_FOLDER_NAME}))
        db.commit()
    publish_command({"type": "refresh", "email": account.email})
    return {"status": "queued", "name": ARCHIVE_FOLDER_NAME, "account_id": account_id}


def _outbox_status(db) -> dict:
    """Mail written here and not yet on its way out.

    Sending is not something the server can do — it hands the message to the
    agent, which relays it over SMTP whenever it next can. That is normally a
    second or two and can be days, because the agent is expected to be offline
    for days; until this existed, the difference between the two was invisible
    from the app. A message queued against a wrong SMTP port sat in exactly the
    same silence as one already delivered (issue #7).

    ``error`` is the last thing that went wrong on the oldest queued message,
    and it does not mean the message has been given up on — nothing gives up on
    a queued message. It is there so the UI can say *why* the count is not
    going down.

    "queued" and not every unsent state, so a message whose send was cancelled
    is not in this number: the strip exists to say that mail is on its way out
    and may be stuck, and a message somebody stopped by hand is neither. It is
    still in the Outbox, and the folder's own count — which is about what is in
    the folder rather than what is in flight — does include it.
    """
    queued, oldest = db.execute(
        select(func.count(Outbound.id), func.min(Outbound.created_at))
        .where(Outbound.state == "queued")
    ).one()
    error = db.scalar(
        select(Outbound.error)
        .where(Outbound.state == "queued", Outbound.error.is_not(None))
        .order_by(Outbound.created_at)
        .limit(1)
    ) if queued else None
    # Written by no current version: what the agent's old five-attempt cap left
    # behind. Surfaced because those messages are still here and still unsent,
    # and `meerail-agent --requeue-abandoned` puts them back.
    abandoned = db.scalar(
        select(func.count(Outbound.id)).where(Outbound.state == "error")
    ) or 0
    return {"queued": queued or 0, "oldest_at": oldest, "error": error,
            "abandoned": abandoned}


def _indexing_status(db) -> dict:
    """Progress of attachment text extraction, which the agent drains on its own
    thread (agent/sync.py run_indexer_forever).

    Reported separately from sync_progress on purpose: extraction is not mail
    sync. A mailbox can be fully fetched with thousands of attachments still
    queued behind Tika, and showing that as "syncing" reads as unfetched mail.
    """
    counts = dict(
        db.execute(
            select(Attachment.extract_status, func.count(Attachment.id))
            .group_by(Attachment.extract_status)
        ).all()
    )
    pending = counts.get("pending", 0)
    done = counts.get("done", 0)
    error = counts.get("error", 0)
    skipped = counts.get("skipped", 0)
    # 'skipped' is excluded from the denominator: those were never queued (they
    # predate the feature, or are types Tika is not asked about), so counting
    # them would leave the bar short of full with nothing left to do.
    settled = done + error
    return {
        "active": pending > 0,
        "pending": pending,
        "done": done,
        "error": error,
        "skipped": skipped,
        "total": settled + pending,
    }
