"""Apply queued actions to Bridge over IMAP/SMTP (the write-back half of
two-way sync).

The UI enqueues PendingAction rows when you mark read, flag, move or send; the
agent drains them here and reports the outcome back on the same rows.
"""

from __future__ import annotations

from sqlalchemy import select

from core.models import Outbound, PendingAction, utcnow

import smtp

MAX_ACTION_ATTEMPTS = 5


def apply_action(db, bridge, account, action: PendingAction) -> None:
    t = action.type
    p = action.payload or {}
    c = bridge.client

    if t == "setflags":
        c.select_folder(p["folder"])          # readwrite
        if p.get("add"):
            c.add_flags([p["uid"]], p["add"])
        if p.get("remove"):
            c.remove_flags([p["uid"]], p["remove"])

    elif t == "move":
        c.select_folder(p["from_folder"])
        c.copy([p["uid"]], p["to_folder"])
        c.delete_messages([p["uid"]])
        c.expunge()

    elif t == "delete":
        c.select_folder(p["folder"])
        c.delete_messages([p["uid"]])
        c.expunge()

    elif t == "create_folder":
        # Idempotent: a retry after a timeout that actually landed must not fail
        # the action permanently on an ALREADYEXISTS. The folder row itself is
        # created by the LIST pass that follows this drain, not here.
        # Where a user folder is allowed to live is the server's business, not
        # the web app's, and only this side can see it — so the name arrives as
        # a bare leaf and gets its namespace here.
        parent = bridge.user_folder_parent()
        name = p["name"]
        if parent and not name.startswith(parent):
            name = parent + name
        if not c.folder_exists(name):
            c.create_folder(name)
        # Best-effort: Bridge subscribes on create and then rejects the
        # redundant SUBSCRIBE outright ("already subscribed to this mailbox").
        # Letting that propagate would fail — and endlessly retry — an action
        # whose folder is already sitting on the server.
        try:
            c.subscribe_folder(name)
        except Exception:  # noqa: BLE001
            pass

    elif t == "send":
        outbound = db.get(Outbound, p["outbound_id"])
        if outbound is None or not outbound.raw_mime:
            raise ValueError(f"outbound {p.get('outbound_id')} has no MIME to send")
        smtp.send_raw(bridge.acc, p["mail_from"], p["rcpt_to"], outbound.raw_mime.encode("utf-8"))

    else:
        raise ValueError(f"unknown action type: {t}")


def _settle(db, action: PendingAction, ok: bool, error: str | None = None) -> None:
    """Record an attempt's outcome, retiring the action once it succeeds or has
    burned through its retries."""
    action.attempts += 1
    terminal_error = not ok and action.attempts >= MAX_ACTION_ATTEMPTS
    action.status = "done" if ok else ("error" if terminal_error else "pending")
    action.error = error

    # A successful send flips its Outbound to "sent" (Proton then auto-saves it
    # to Sent, which the next folder sync ingests normally).
    if action.type == "send":
        outbound = db.get(Outbound, (action.payload or {}).get("outbound_id"))
        if outbound:
            outbound.state = "sent" if ok else ("error" if terminal_error else "queued")
            outbound.error = error
            if ok:
                outbound.sent_at = utcnow()


def drain_actions(db, bridge, account) -> int:
    """Apply every pending action for this account. Returns the count handled."""
    actions = db.execute(
        select(PendingAction)
        .where(PendingAction.account_id == account.id, PendingAction.status == "pending")
        .order_by(PendingAction.created_at)
        .limit(50)
    ).scalars().all()

    for action in actions:
        try:
            apply_action(db, bridge, account, action)
            _settle(db, action, True)
        except Exception as e:  # noqa: BLE001
            _settle(db, action, False, repr(e))
    db.commit()
    return len(actions)
