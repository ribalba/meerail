from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from core.database import get_db
from ..deps import require_ui_auth
from core.models import Account, Mailbox, MessageLocation

router = APIRouter(prefix="/api/mailboxes", tags=["mailboxes"], dependencies=[Depends(require_ui_auth)])

# Sidebar ordering by role.
ROLE_ORDER = {"inbox": 0, "flagged": 1, "drafts": 2, "sent": 3, "archive": 4,
              "junk": 5, "trash": 6, "all": 7, "custom": 8}


@router.get("")
def list_mailboxes(db: DBSession = Depends(get_db)):
    """Sidebar data: accounts with their folders, plus unified/smart counts.

    Counts are computed LIVE (one grouped query) rather than read from the
    denormalized columns, so the sidebar can never drift out of sync."""
    accounts = db.execute(select(Account).order_by(Account.created_at)).scalars().all()

    # mailbox_id -> (total, unread) over non-deleted placements.
    counts: dict[int, tuple[int, int]] = {}
    for mid, total, unread in db.execute(
        select(
            MessageLocation.mailbox_id,
            func.count(),
            func.count().filter(MessageLocation.seen.is_(False)),
        )
        .where(MessageLocation.deleted.is_(False))
        .group_by(MessageLocation.mailbox_id)
    ).all():
        counts[mid] = (int(total), int(unread))

    flagged_total = db.scalar(
        select(func.count()).select_from(MessageLocation)
        .where(MessageLocation.flagged.is_(True), MessageLocation.deleted.is_(False))
    ) or 0

    out_accounts = []
    unified_unread = 0
    for acc in accounts:
        mbs = db.execute(select(Mailbox).where(Mailbox.account_id == acc.id)).scalars().all()
        mbs.sort(key=lambda m: (ROLE_ORDER.get(m.role, 8), m.display_name.lower()))
        mb_out = []
        for m in mbs:
            total, unread = counts.get(m.id, (0, 0))
            if m.role == "inbox":
                unified_unread += unread
            mb_out.append({"id": m.id, "role": m.role, "display_name": m.display_name,
                           "imap_name": m.imap_name, "unread": unread, "total": total})
        out_accounts.append({
            "id": acc.id, "email": acc.email, "label": acc.label, "color": acc.color,
            "backfill_complete": acc.backfill_complete, "mailboxes": mb_out,
        })

    return {
        "accounts": out_accounts,
        "smart": {"unified_inbox_unread": int(unified_unread), "flagged_total": int(flagged_total),
                  "account_count": len(accounts)},
    }
