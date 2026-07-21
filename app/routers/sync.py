from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from ..database import get_db
from ..deps import require_ui_auth
from ..models import Account, Mailbox

router = APIRouter(prefix="/api/sync", tags=["sync"], dependencies=[Depends(require_ui_auth)])


@router.get("/status")
def sync_status(db: DBSession = Depends(get_db)):
    accounts = db.execute(select(Account).order_by(Account.created_at)).scalars().all()
    out = []
    for acc in accounts:
        mailboxes = db.execute(
            select(Mailbox).where(Mailbox.account_id == acc.id).order_by(Mailbox.sort_order, Mailbox.imap_name)
        ).scalars().all()
        out.append({
            "account_id": acc.id,
            "email": acc.email,
            "label": acc.label,
            "backfill_complete": acc.backfill_complete,
            "last_agent_seen": acc.last_agent_seen,
            "last_sync_at": acc.last_sync_at,
            "total": sum(m.total_count for m in mailboxes),
            "unread": sum(m.unread_count for m in mailboxes if m.role == "inbox"),
            "mailboxes": [
                {"id": m.id, "imap_name": m.imap_name, "display_name": m.display_name,
                 "role": m.role, "unread": m.unread_count, "total": m.total_count,
                 "last_uid": m.last_uid}
                for m in mailboxes
            ],
        })
    return out
