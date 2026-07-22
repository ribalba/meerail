from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session as DBSession

from core.database import get_db
from core.events import publish_command
from ..deps import require_ui_auth
from core.models import Account, Mailbox, MessageLocation, PendingAction

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
                           "imap_name": m.imap_name, "unread": unread, "total": total,
                           "favorite": m.favorite})
        out_accounts.append({
            "id": acc.id, "email": acc.email, "label": acc.label, "color": acc.color,
            "backfill_complete": acc.backfill_complete, "mailboxes": mb_out,
        })

    return {
        "accounts": out_accounts,
        "smart": {"unified_inbox_unread": int(unified_unread), "flagged_total": int(flagged_total),
                  "account_count": len(accounts)},
    }


class CreateMailbox(BaseModel):
    account_id: int
    name: str


def _clean_folder_name(raw: str) -> str:
    """Validate a user-typed folder name. Returns a bare leaf, never a path.

    "/" is rejected rather than treated as a nesting delimiter: on Bridge every
    real folder comes back \\Noinferiors, so it cannot hold children, and "/"
    inside a Proton folder name is an escaped literal ("A\\/B") rather than a
    separator. The agent prepends whatever namespace the server demands — see
    Bridge.user_folder_parent."""
    name = raw.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Folder name is required")
    if len(name) > 255:
        raise HTTPException(status_code=400, detail="Folder name is too long")
    if any(ord(ch) < 32 or ch == "\x7f" for ch in name):
        raise HTTPException(status_code=400, detail="Folder name contains invalid characters")
    if "/" in name:
        raise HTTPException(status_code=400,
                            detail="Folder name cannot contain / — nested folders are not supported")
    # IMAP quoting and LIST wildcards.
    if any(ch in name for ch in '"\\%*'):
        raise HTTPException(status_code=400, detail='Folder name cannot contain " \\ % or *')
    return name


@router.post("", status_code=202)
def create_mailbox(body: CreateMailbox, db: DBSession = Depends(get_db)):
    """Queue an IMAP folder creation for the agent.

    The Mailbox row is deliberately NOT written here: prune_mailboxes deletes
    any folder missing from the server's LIST, so an optimistic row would be
    wiped by the very pass that is meant to confirm it. The agent creates the
    folder, the LIST at the top of the same pass registers it, and the sidebar
    picks it up off the "folders" event seconds later."""
    account = db.get(Account, body.account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    name = _clean_folder_name(body.name)

    # display_name, not just imap_name: the agent stores the folder under
    # whatever namespace the server imposes ("Folders/Receipts"), so comparing
    # the bare leaf against imap_name alone would miss every Bridge folder.
    clash = db.execute(
        select(Mailbox).where(
            Mailbox.account_id == account.id,
            or_(Mailbox.imap_name == name, Mailbox.display_name == name),
        )
    ).scalars().first()
    if clash is not None:
        raise HTTPException(status_code=409, detail="That folder already exists")

    # A second click before the agent has run would otherwise queue a duplicate.
    already_queued = db.execute(
        select(PendingAction).where(
            PendingAction.account_id == account.id,
            PendingAction.type == "create_folder",
            PendingAction.status == "pending",
        )
    ).scalars().all()
    if any((a.payload or {}).get("name") == name for a in already_queued):
        raise HTTPException(status_code=409, detail="That folder is already being created")

    db.add(PendingAction(account_id=account.id, message_pk=None,
                         type="create_folder", payload={"name": name}))
    db.commit()
    publish_command({"type": "refresh", "email": account.email})
    return {"status": "queued", "name": name, "account_id": account.id}


@router.patch("/{mailbox_id}/favorite")
def set_favorite(mailbox_id: int, favorite: bool, db: DBSession = Depends(get_db)):
    """Pin/unpin a folder in the sidebar's Favorites section. UI-only state."""
    mb = db.get(Mailbox, mailbox_id)
    if mb is None:
        raise HTTPException(status_code=404, detail="Mailbox not found")
    mb.favorite = favorite
    db.commit()
    return {"id": mb.id, "favorite": mb.favorite}
