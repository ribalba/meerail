"""What a move took away, recorded so it can be put back.

Undo here is deliberately not a general-purpose command log. It covers the one
family of actions whose result is *somewhere else* — trash, archive, move, and
the bulk versions of all three — because those are the ones a mis-aimed keypress
files out of sight. The rest already have an answer that is better than an undo
stack: a message marked read is unmarked by marking it unread, a send waits in
the Outbox with a Cancel next to it, and emptying the Trash is gone from the mail
server, where no record kept here could reach it.

Two keys are written onto the queue row the action already creates
(``PendingAction.payload`` is JSONB and takes new keys without a migration):

``op_id``
    Groups every row one keypress produced. A bulk trash of two hundred
    conversations is two hundred queue rows and *one* operation, and both the
    panel and the undo work in operations — undoing half a keypress is not a
    thing anyone asked for.

``undo_from``
    The placements the move removed: folder, UID, the UID epoch that number was
    read under, and the flags it was carrying. This is the part that cannot be
    reconstructed afterwards. A move *deletes* its ``message_locations`` rows
    (app/routers/actions.py::_move_to), so once it has run the only surviving
    record of where the message was is the ``from_folder`` on the action — and on
    a server where folders are labels that is one of the three places it was,
    because those servers queue a single move for a message wearing three labels.

The epoch is what makes the record safe to act on later. A UID names a message
only within one UIDVALIDITY, so a snapshot taken before a folder was rebuilt
points at whatever message has since inherited that number; ``restore`` refuses
rather than putting a placement back on a guess, for the same reason the agent
refuses to apply a queued action across the same boundary (agent/actions.py::
_select_verified).
"""

from __future__ import annotations

import uuid

from .models import Mailbox, Message, MessageLocation

# The action types an operation is built out of. "delete" is here so that
# emptying the Trash still appears in the panel — as one line saying it cannot be
# undone, which is more use to someone reading their recent activity than not
# appearing at all.
LOGGED_TYPES = ("move", "delete")

# What the panel calls the operation. Carried on the row rather than inferred,
# because "move to Trash" and "trash" are the same IMAP command and very
# different sentences, and only the endpoint that was called knows which it was.
KINDS = ("trash", "archive", "move", "delete", "undo", "remind")


class Unrestorable(RuntimeError):
    """This snapshot can no longer be put back where it came from."""


def new_op_id() -> str:
    """One id per user keypress, however many queue rows it turns into."""
    return uuid.uuid4().hex


def fields(op_id: str | None, kind: str, undo_from: list[dict] | None) -> dict:
    """The undo keys to merge into an action's payload, or nothing at all.

    ``op_id`` of None means the caller is not offering an undo for this action,
    and the row is then invisible to the panel — which is what the flag-catchup
    actions queued by the sync want (core/mail/store.py::_queue_flag_catchup):
    nobody pressed anything, so there is nothing to take back.
    """
    if op_id is None:
        return {}
    return {"op_id": op_id, "op_kind": kind, "undo_from": undo_from or []}


def snapshot(db, loc: MessageLocation) -> dict:
    """Everything needed to put one placement back exactly as it was.

    The flags travel with it because they are per-placement and the move is what
    destroys them: a message read in the inbox and then archived carries its
    \\Seen on the archive copy, and putting it back has to put that back too or
    undoing an archive silently marks the mail unread.
    """
    mb = db.get(Mailbox, loc.mailbox_id)
    return {
        "mailbox_id": loc.mailbox_id,
        "imap_uid": loc.imap_uid,
        # None for a placement we wrote ourselves, which has no server epoch —
        # see restore(), which does not ask for one in that case.
        "uidvalidity": mb.uidvalidity if mb is not None else None,
        "seen": loc.seen,
        "flagged": loc.flagged,
        "answered": loc.answered,
        "draft": loc.draft,
        "deleted": loc.deleted,
        "keywords": list(loc.keywords or []),
    }


def restore(db, msg: Message, snaps: list[dict], touched: set[int]) -> int:
    """Put back the placements a move removed. Returns how many were restored.

    Only correct for a move the mail server was never told about — an action
    still sitting in the queue when the undo arrived. Once the move has been
    applied, the server's own copy is the truth and putting a row back here would
    only claim a placement the next sync would prune; that case queues a move in
    the opposite direction instead (app/routers/undo.py).

    Idempotent by folder: a placement already present is left alone rather than
    duplicated. Undo is a button people press twice.
    """
    restored = 0
    for snap in snaps:
        mailbox_id = snap.get("mailbox_id")
        uid = snap.get("imap_uid")
        mb = db.get(Mailbox, mailbox_id) if mailbox_id is not None else None
        if mb is None or uid is None:
            raise Unrestorable(
                "the folder this was moved out of is no longer in the database")
        if uid > 0:
            # A server UID, and so only meaningful in the epoch it was read
            # under. Refusing here costs an undo; guessing costs whichever
            # message has since been handed that number, which would show up in
            # the folder wearing this one's flags.
            known = snap.get("uidvalidity")
            if known is None or mb.uidvalidity != known:
                raise Unrestorable(
                    f"{mb.display_name or mb.imap_name} has been rebuilt since this "
                    f"was moved, so uid {uid} no longer names the same message")
        if any(loc.mailbox_id == mailbox_id for loc in msg.locations):
            touched.add(mailbox_id)
            continue
        msg.locations.append(MessageLocation(
            mailbox_id=mailbox_id,
            imap_uid=uid,
            seen=bool(snap.get("seen")),
            flagged=bool(snap.get("flagged")),
            answered=bool(snap.get("answered")),
            draft=bool(snap.get("draft")),
            deleted=bool(snap.get("deleted")),
            keywords=list(snap.get("keywords") or []),
        ))
        touched.add(mailbox_id)
        restored += 1
    return restored
