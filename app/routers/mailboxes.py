from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from core import ingest, outbox as outbox_core
from core.database import get_db
from core.ingest import FOLDER_SEP
from .. import events, mailops
from ..deps import require_ui_auth
from core.models import (
    Account, Mailbox, MessageLocation, Outbound, PendingAction, Reminder, utcnow,
)

router = APIRouter(prefix="/api/mailboxes", tags=["mailboxes"], dependencies=[Depends(require_ui_auth)])

# Sidebar ordering by role.
ROLE_ORDER = {"inbox": 0, "flagged": 1, "drafts": 2, "sent": 3, "archive": 4,
              "junk": 5, "trash": 6, "all": 7, "custom": 8}


def server_sep(account: Account) -> str:
    """What separates a parent from a child in this account's folder names.

    The agent reads it off the server's LIST and writes it here — "/" on Bridge
    and Gmail, "." on many Dovecot installs — and it is empty until an agent has
    reported, or for an account no agent syncs at all. Both of those fall back
    to "/", which is what the importer writes and what the folder dialog reads
    from the user.
    """
    return account.folder_delimiter or FOLDER_SEP


def _chains(account: Account, mailboxes: list[Mailbox]) -> dict[int, list[Mailbox]]:
    """For each folder, the folders it hangs under — itself last.

    Nesting is read off imap_name, but only where the parent is a folder that
    actually exists. That distinction is the whole of it: Bridge stores every
    user folder as "Folders/<leaf>" without there being any folder called
    "Folders", so indenting on the separator alone would put a mailbox's entire
    contents one level in under a heading that is not there. An "Archive/2024"
    made beside a real "Archive" is the case that does nest, and it nests
    because both rows exist.

    Returns chains rather than depths because the sidebar needs both: the depth
    to indent by, and the ancestry to sort a child directly beneath its parent
    instead of alphabetically among its aunts.
    """
    sep = server_sep(account)
    by_name = {m.imap_name: m for m in mailboxes}
    chains: dict[int, list[Mailbox]] = {}
    for m in mailboxes:
        parts = m.imap_name.split(sep)
        chain = [by_name[sep.join(parts[:i])]
                 for i in range(1, len(parts))
                 if sep.join(parts[:i]) in by_name]
        chains[m.id] = chain + [m]
    return chains


def _paths(account: Account, mailboxes: list[Mailbox]) -> dict[int, str]:
    """Each folder as a person reads it: the display names down its chain,
    joined with "/" whatever the server's own separator is.

    This is the name the sidebar, the move menu and the New Folder box all deal
    in, so it is also what a typed name has to be compared against — see
    create_mailbox, where comparing against imap_name alone would let somebody
    make a second "Receipts" on Bridge because the first one is stored as
    "Folders/Receipts".
    """
    return {mid: FOLDER_SEP.join(a.display_name for a in chain)
            for mid, chain in _chains(account, mailboxes).items()}


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

    # Messages, not placements: the Flagged list is a list of flagged mail (see
    # app/routers/messages.py::list_messages), and on a label server one such
    # mail is filed in two or three folders at once. Counting the placements
    # made this number say four where the list had three rows in it.
    flagged_total = db.scalar(
        select(func.count(func.distinct(MessageLocation.message_pk)))
        .where(MessageLocation.flagged.is_(True), MessageLocation.deleted.is_(False))
    ) or 0

    # The Outbox is not an IMAP folder — it is this app's own queue — but it is
    # a folder to whoever wrote the mail sitting in it, so the sidebar renders
    # it as one. `failing` rides along because the row goes red on it, and the
    # alternative would be the sidebar fetching the whole outbox to find out.
    outbox_unsent, outbox_failing = db.execute(
        select(func.count(Outbound.id),
               func.count(Outbound.id).filter(Outbound.error.is_not(None)))
        .where(Outbound.state.in_(outbox_core.UNSENT_STATES))
    ).one()

    # Conversations put off until later. Like the Outbox this is not an IMAP
    # folder — the mail is sitting in Archive — but it is a place to the person
    # who put something there, so the sidebar gives it a row. `overdue` counts
    # the ones whose time has come and which have not landed: the reminder is
    # late, and the row says so the way the Outbox says a send is failing.
    reminders_pending, reminders_overdue = db.execute(
        select(func.count(Reminder.id),
               func.count(Reminder.id).filter(Reminder.due_at <= utcnow()))
        .where(Reminder.state == "pending")
    ).one()

    out_accounts = []
    unified_unread = 0
    for acc in accounts:
        mbs = db.execute(select(Mailbox).where(Mailbox.account_id == acc.id)).scalars().all()
        chains = _chains(acc, mbs)
        mbs.sort(key=lambda m: (ROLE_ORDER.get(chains[m.id][0].role, 8),
                                tuple(a.display_name.lower() for a in chains[m.id])))
        mb_out = []
        for m in mbs:
            total, unread = counts.get(m.id, (0, 0))
            if m.role == "inbox":
                unified_unread += unread
            mb_out.append({"id": m.id, "role": m.role, "display_name": m.display_name,
                           "imap_name": m.imap_name, "unread": unread, "total": total,
                           "favorite": m.favorite, "depth": len(chains[m.id]) - 1,
                           # The path as a person would read it — what the move
                           # menu shows, where a bare "2024" under three parents
                           # would not say which one.
                           "path": FOLDER_SEP.join(a.display_name for a in chains[m.id])})
        out_accounts.append({
            "id": acc.id, "email": acc.email, "label": acc.label, "color": acc.color,
            "backfill_complete": acc.backfill_complete, "local": acc.local,
            # Whether the New Folder box may offer "Parent/Child" here. Per
            # account, because one install commonly holds a Bridge account that
            # cannot nest beside an IMAP account that can.
            "nesting": acc.local or acc.folder_nesting,
            "mailboxes": mb_out,
        })

    return {
        "accounts": out_accounts,
        "smart": {"unified_inbox_unread": int(unified_unread), "flagged_total": int(flagged_total),
                  "account_count": len(accounts),
                  "outbox_unsent": int(outbox_unsent or 0),
                  "outbox_failing": int(outbox_failing or 0),
                  "reminders_pending": int(reminders_pending or 0),
                  "reminders_overdue": int(reminders_overdue or 0)},
    }


class CreateMailbox(BaseModel):
    account_id: int
    name: str


# How deep a typed path may nest. No server rule behind it — most IMAP servers
# have no limit — just a number past which "Archive/2024/Q1/July/week 2/…" is a
# typo rather than a filing scheme.
MAX_FOLDER_DEPTH = 8

# What the New Folder box says when the account's server cannot nest. Named
# rather than inline because it is the one refusal here that is about the
# server rather than about the name, and the sentence has to say which.
NO_NESTING = ("This account's mail server does not allow folders inside folders, "
              "so a name cannot contain /.")


def _clean_folder_name(raw: str, nested: bool = False) -> str:
    """Validate a user-typed folder name. Returns a bare leaf, or a path.

    ``nested`` is whether "/" may mean what it looks like, and it is the
    server's answer rather than a constant — Account.folder_nesting, read off
    IMAP's LIST by the agent (agent/imap.py::_folder_capabilities). It used to
    be "never", which was Proton Bridge's answer applied to everybody: there
    every user folder comes back \\Noinferiors and a "/" inside a Proton folder
    name is an escaped literal ("A\\/B") rather than a separator. Gmail, Dovecot
    and the university IMAP servers people run beside it nest perfectly well,
    and refusing them was refusing something that works.

    Where it is allowed, each segment is checked as a name in its own right, so
    "Archive//2024" and "Archive/ /2024" are refused rather than quietly
    collapsed into something the user did not type. The path is always in "/",
    whatever the server's own delimiter is: the agent joins the segments with
    that when it creates the folder, so nobody typing a name has to know it.
    """
    name = raw.strip().strip(FOLDER_SEP) if nested else raw.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Folder name is required")
    if len(name) > 255:
        raise HTTPException(status_code=400, detail="Folder name is too long")
    if any(ord(ch) < 32 or ch == "\x7f" for ch in name):
        raise HTTPException(status_code=400, detail="Folder name contains invalid characters")
    if not nested and FOLDER_SEP in name:
        raise HTTPException(status_code=400, detail=NO_NESTING)
    segments = name.split(FOLDER_SEP) if nested else [name]
    if len(segments) > MAX_FOLDER_DEPTH:
        raise HTTPException(status_code=400,
                            detail=f"Folders can be nested at most {MAX_FOLDER_DEPTH} deep")
    for segment in segments:
        if not segment.strip():
            raise HTTPException(status_code=400, detail="Folder name has an empty part")
        if segment != segment.strip():
            raise HTTPException(status_code=400,
                                detail="Folder names cannot start or end with a space")
        # IMAP quoting and LIST wildcards. Meaningless for a folder no server
        # will ever be told about, but a local account can stop being one — an
        # agent configured for the address later takes over these folders — and
        # a name that cannot survive that is not worth allowing here.
        if any(ch in segment for ch in '"\\%*'):
            raise HTTPException(status_code=400, detail='Folder name cannot contain " \\ % or *')
    return FOLDER_SEP.join(segments)


@router.post("", status_code=202)
def create_mailbox(body: CreateMailbox, response: Response, db: DBSession = Depends(get_db)):
    """Make a folder: here and now if nothing syncs this account, queued if
    something does.

    For an account with an agent the Mailbox row is deliberately NOT written
    here: prune_mailboxes deletes any folder missing from the server's LIST, so
    an optimistic row would be wiped by the very pass that is meant to confirm
    it. The agent creates the folder, the LIST at the top of the same pass
    registers it, and the sidebar picks it up off the "folders" event seconds
    later.

    An imported account has no agent and never will (Account.local), so that
    queue has no reader: the request used to answer 202 "queued" and the folder
    then never appeared, which is the same thing as the + button not working.
    There the row is the folder — written now, returned 201, on screen on the
    way back from the click.

    Whether the name may nest is the *server's* answer either way — see
    _clean_folder_name — and for a queued create the path travels as segments,
    because "/" is what a person types and not necessarily what the server puts
    between a parent and a child (agent/actions.py joins them with the real
    one)."""
    account = db.get(Account, body.account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    name = _clean_folder_name(body.name, nested=account.local or account.folder_nesting)
    segments = name.split(FOLDER_SEP)

    # Compared as paths — the name as the sidebar prints it — rather than
    # against imap_name, which is the server's spelling of it. Bridge stores a
    # folder called Receipts as "Folders/Receipts" and a Dovecot child as
    # "Archive.2024", and a person typing either would otherwise be told they
    # were making something new. Leaves are free to repeat: "2024" under Archive
    # does not make "2024" under Receipts a duplicate, which is the point of
    # nesting.
    mbs = db.execute(select(Mailbox).where(Mailbox.account_id == account.id)).scalars().all()
    if name in set(_paths(account, mbs).values()):
        raise HTTPException(status_code=409, detail="That folder already exists")

    if account.local:
        mailbox = ingest.ensure_local_folder(db, account, name)
        db.commit()
        response.status_code = 201
        return {"status": "created", "name": name, "account_id": account.id,
                "mailbox_id": mailbox.id}

    # A second click before the agent has run would otherwise queue a duplicate.
    # "leased" counts as queued: that is a row an agent is creating the folder
    # for right now (agent/actions.py::_lease), which is as good a reason not to
    # ask for it again as one still waiting.
    already_queued = db.execute(
        select(PendingAction).where(
            PendingAction.account_id == account.id,
            PendingAction.type == "create_folder",
            PendingAction.status.in_(("pending", "leased")),
        )
    ).scalars().all()
    if any((a.payload or {}).get("name") == name for a in already_queued):
        raise HTTPException(status_code=409, detail="That folder is already being created")

    # Both: `segments` is what the agent builds the folder from, `name` is the
    # sentence the log and the dropped-actions notice print — and is what an
    # agent from before nesting existed still reads, which for a flat name is
    # the same thing.
    db.add(PendingAction(account_id=account.id, message_pk=None, type="create_folder",
                         payload={"name": name, "segments": segments}))
    db.commit()
    mailops.wake_agent(db, account.id)
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


# How many placements one delete-a-folder request takes out at a time. The same
# number the bulk routes chunk on, and for the same reason: a mis-aimed import
# is tens of thousands of rows, and the IN list of a single DELETE over all of
# them is not a statement anybody wants to see in a log.
PURGE_CHUNK = 2000

# What deleting a folder is refused with off an account something syncs, and
# why. The folder is the server's, not ours: dropping the row here would delete
# this app's copy of the mail in it and the next LIST would put the folder
# straight back, empty, having achieved nothing except losing the local copy of
# everything that was in it. Same shape as the refusal on permanently deleting
# mail (app/routers/actions.py::NOT_LOCAL) and for the same reason — a delete
# that the next sync undoes is a delete that silently failed.
NOT_LOCAL_FOLDER = (
    "This folder lives on a mail server, so it has to be deleted there — meerail "
    "would only drop its own copy and the next sync would list the folder straight "
    "back. Delete it in your mail provider's client and it goes from here on the "
    "next pass.")


def _subtree(account: Account, mailboxes: list[Mailbox], root: Mailbox) -> list[Mailbox]:
    """The folder, and every folder filed underneath it.

    Deleting "old" is deleting the twenty folders inside it: a child left behind
    would be a row whose parent is gone, which the sidebar draws at the top
    level under a name that no longer says where it came from. Resolved through
    _chains rather than by matching name prefixes, so it means the same thing
    the sidebar's indentation does — a folder is only a child where the parent
    is itself a folder that exists.
    """
    chains = _chains(account, mailboxes)
    return [m for m in mailboxes if any(a.id == root.id for a in chains[m.id])]


@router.delete("/{mailbox_id}")
def delete_mailbox(mailbox_id: int, confirm: bool = False, db: DBSession = Depends(get_db)):
    """Delete a folder, with what is filed in it. Imported accounts only.

    The other half of the ``+`` button, and the question a mis-aimed import
    actually asks: an mbox that went in under the wrong name is twenty folders
    somebody never meant to have, several of them empty, and until now there was
    no button anywhere in meerail that removed one. Emptying them one by one
    left the folders themselves standing.

    Children go with it (see _subtree), and so does the mail, through
    mailops.purge: dropping the Mailbox row alone would take every placement
    with it down the foreign key's ON DELETE CASCADE and leave the messages
    behind, filed in no folder at all — invisible, unreachable and still holding
    their raw MIME and every attachment byte. That is the bug purge was written
    for, arriving by a different door. A message that is also filed somewhere
    else keeps that copy; only mail this folder was the last home of is
    destroyed.

    ``confirm`` is the caller saying out loud that it means it, and it is asked
    for only when there is something to lose — a folder holding neither mail nor
    other folders is deleted on the first request, because "are you sure" about
    an empty folder is a dialog that teaches people to click through dialogs.
    "Holding mail" means mail the user can still see: an empty folder here has
    to be the same empty folder the sidebar drew, or the confirmation arrives as
    a contradiction.
    Anything else answers 409 with the counts in it, which is the sentence the
    browser then puts in front of the user rather than a number it worked out
    for itself.
    """
    mb = db.get(Mailbox, mailbox_id)
    if mb is None:
        raise HTTPException(status_code=404, detail="Mailbox not found")
    account = db.get(Account, mb.account_id)
    if account is None or not account.local:
        raise HTTPException(status_code=400, detail=NOT_LOCAL_FOLDER)

    mbs = db.execute(select(Mailbox).where(Mailbox.account_id == account.id)).scalars().all()
    doomed = _subtree(account, mbs, mb)
    ids = [m.id for m in doomed]

    # Counted the way every other read path counts — non-deleted placements
    # only, the same filter the sidebar's own totals use (list_mailboxes) and
    # the one core.models.still_filed applies everywhere else. A placement
    # carrying \Deleted is mail the user cannot see in this folder or anywhere
    # else, and an mbox import makes them by the hundred: the Status letters an
    # export carries include "D", and tools/import_mbox.py stores that flag as
    # it finds it. Counting those here asked "delete the 2 messages in Old?"
    # about a folder the sidebar had just called empty — a dialog contradicting
    # the screen behind it, over mail that is already gone. They still go with
    # the folder (the purge below takes every placement, flagged or not); they
    # are simply not something to warn about losing.
    held = db.scalar(select(func.count()).select_from(MessageLocation)
                     .where(MessageLocation.mailbox_id.in_(ids),
                            MessageLocation.deleted.is_(False))) or 0
    if not confirm and (held or len(doomed) > 1):
        raise HTTPException(status_code=409, detail=_what_goes(mb, doomed, held))

    # Chunked, and re-queried each pass rather than walked once: purge expires
    # the session as it goes, and the next chunk is simply "whatever is still
    # filed here".
    touched: set[int] = set()
    destroyed = 0
    while True:
        loc_ids = list(db.execute(
            select(MessageLocation.id).where(MessageLocation.mailbox_id.in_(ids))
            .limit(PURGE_CHUNK)).scalars().all())
        if not loc_ids:
            break
        destroyed += mailops.purge(db, loc_ids, touched)

    for row in doomed:
        db.delete(row)
    db.commit()
    events.publish({"type": "folders", "account": account.email, "removed": len(doomed)})
    return {"ok": True, "folders": len(doomed), "deleted": destroyed, "held": held}


def _what_goes(mb: Mailbox, doomed: list[Mailbox], held: int) -> str:
    """The sentence a folder delete has to be confirmed against.

    Written here because only this side knows what is actually in the subtree —
    the sidebar has a count per folder and no idea which of the others hang off
    this one — and because the two numbers mean different things: folders is how
    much of the tree disappears, and messages is what stops existing.
    """
    name = mb.display_name or mb.imap_name
    parts = []
    if held:
        parts.append(f"{held} message{'' if held == 1 else 's'}")
    kids = len(doomed) - 1
    if kids:
        parts.append(f"{kids} folder{'' if kids == 1 else 's'} inside it")
    what = " and ".join(parts)
    return (f"{name} holds {what}. Deleting the folder deletes {'them' if held else 'those'} "
            f"— this mail was imported, so meerail holds the only copy of it, and mail that "
            f"is not also filed somewhere else is gone for good.")
