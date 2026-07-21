"""Apply queued server actions to Bridge over IMAP/SMTP (the write-back half of
two-way sync)."""

from __future__ import annotations

import base64

import smtp


def apply_action(bridge, server, action: dict) -> None:
    t = action["type"]
    p = action.get("payload", {})
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

    elif t == "send":
        # Fetch the raw MIME by id (keeps large attachments out of the action queue).
        raw = base64.b64decode(server.get_outbound(p["outbound_id"])["raw_b64"])
        smtp.send_raw(bridge.acc, p["mail_from"], p["rcpt_to"], raw)

    else:
        raise ValueError(f"unknown action type: {t}")


def drain_actions(bridge, server, email: str) -> int:
    """Pull pending actions and apply them, ack'ing each. Returns count handled."""
    actions = server.get_actions(email)
    for a in actions:
        try:
            apply_action(bridge, server, a)
            server.ack_action(a["id"], True)
        except Exception as e:  # noqa: BLE001
            server.ack_action(a["id"], False, repr(e))
    return len(actions)
