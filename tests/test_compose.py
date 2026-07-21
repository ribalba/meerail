"""Integration tests for compose: send enqueues a send action; reply-context."""

import base64
import uuid

from conftest import ingest_one
from helpers import api, build_pdf, upload_attachment


def test_send_creates_outbound_and_send_action(account):
    email, aid = account["email"], account["id"]
    code, r = api("POST", "/api/compose/send", {
        "account_id": aid, "to": ["dest@example.com"],
        "subject": "Hi there", "body_text": "SENDBODY content"})
    assert code == 200 and r["state"] == "queued"

    _, actions = api("GET", f"/api/agent/actions?account={email}")
    sends = [a for a in actions if a["type"] == "send"]
    assert sends and "outbound_id" in sends[0]["payload"]
    assert "dest@example.com" in sends[0]["payload"]["rcpt_to"]

    # The agent fetches the raw MIME by id.
    code, raw = api("GET", f"/api/agent/outbound/{sends[0]['payload']['outbound_id']}")
    assert code == 200 and "raw_b64" in raw


def test_send_with_attachment_bakes_it_into_the_mime(account):
    email, aid = account["email"], account["id"]
    code, up = upload_attachment(build_pdf("ATTACHSEND report"), "report.pdf", "application/pdf")
    assert code == 200 and up["id"]

    code, _ = api("POST", "/api/compose/send", {
        "account_id": aid, "to": ["dest@example.com"], "subject": "With a file",
        "body_text": "see attached", "attachments": [up["id"]]})
    assert code == 200

    _, actions = api("GET", f"/api/agent/actions?account={email}")
    oid = next(a["payload"]["outbound_id"] for a in actions if a["type"] == "send")
    _, raw = api("GET", f"/api/agent/outbound/{oid}")
    mime = base64.b64decode(raw["raw_b64"]).decode("utf-8", "replace")
    assert "report.pdf" in mime
    assert "application/pdf" in mime


def test_send_requires_recipient(account):
    code, _ = api("POST", "/api/compose/send",
                  {"account_id": account["id"], "to": [], "subject": "x", "body_text": "y"})
    assert code == 400


def test_reply_context_prefills_headers(account):
    email, aid = account["email"], account["id"]
    mid, rfc = ingest_one(email, aid, "REPLYTOK" + uuid.uuid4().hex[:6], frm="alice@ex.com")

    _, ctx = api("GET", f"/api/compose/reply-context/{mid}?mode=reply")
    assert ctx["to"] == ["alice@ex.com"]
    assert ctx["subject"].startswith("Re:")
    assert ctx["in_reply_to"] == rfc          # the original Message-ID
    assert rfc in ctx["references"]


def test_forward_context(account):
    email, aid = account["email"], account["id"]
    mid, _ = ingest_one(email, aid, "FWDTOK" + uuid.uuid4().hex[:6])

    _, ctx = api("GET", f"/api/compose/reply-context/{mid}?mode=forward")
    assert ctx["subject"].startswith("Fwd:")
    assert ctx["to"] == []
    assert "Forwarded message" in ctx["body_text"]
