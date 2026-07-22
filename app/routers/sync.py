from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from core import ingest
from core.database import get_db
from core.events import publish_command
from ..deps import require_ui_auth
from core.models import Account, Attachment, Mailbox, Message, utcnow

router = APIRouter(prefix="/api/sync", tags=["sync"], dependencies=[Depends(require_ui_auth)])

# How long an account may go without a sign of life before the UI calls the
# agent offline. Two limits, because "quiet" means different things:
#
#   HEALTHY — a working agent stamps last_agent_seen once per pass, i.e. every
#     poll_interval (30s by default), so three minutes of silence is already
#     well outside normal.
#   BACKING OFF — a failing agent retries on a backoff that tops out at 300s
#     (agent/sync.py), so it can legitimately be quiet for five minutes. Judging
#     it by the healthy limit would report "offline" for an agent that is very
#     much running and telling us exactly what is wrong.
STALE_AFTER_HEALTHY = 180
STALE_AFTER_FAILING = 600


def _account_state(acc: Account, last_ingest, now) -> tuple[str, str]:
    """Classify one account's agent into a state + a sentence explaining it.

    Decided here rather than in the browser so every consumer agrees, and so the
    thresholds sit next to the agent behaviour they are derived from.
    """
    if acc.last_agent_seen is None:
        return "never", "No agent has ever connected for this account."

    # A pass that is mid-flight stores messages without re-stamping
    # last_agent_seen (that happens once, at pass start). During the initial
    # backfill a single pass can run for many minutes, so mail landing in the
    # database counts as a sign of life in its own right — otherwise a busy
    # agent would be reported offline precisely while it works hardest.
    seen = max([t for t in (acc.last_agent_seen, last_ingest) if t is not None])
    quiet = (now - seen).total_seconds()
    limit = STALE_AFTER_FAILING if acc.last_error else STALE_AFTER_HEALTHY

    if quiet > limit:
        return "offline", (
            f"No sign of the agent for {_human(quiet)}. It is probably not running."
        )
    if acc.last_error:
        return "failing", (
            "The agent is running but its last sync pass failed. It keeps retrying "
            "on a backoff."
        )
    if not acc.backfill_complete:
        return "backfilling", "First full sync is still running."
    return "ok", "Syncing normally."


def _human(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    return f"{int(seconds // 3600)}h"


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

        state, detail = _account_state(acc, last_ingest, now)
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
    }


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
