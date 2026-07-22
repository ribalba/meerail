"""Integration tests for compose: send enqueues a send action; reply-context."""

import uuid
from datetime import datetime, timezone

import dbfixture
from conftest import ingest_one
from core.models import DEFAULT_FOOTER
from helpers import api, build_pdf, make_message, upload_attachment

T0 = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)


def test_send_creates_outbound_and_send_action(account):
    email, aid = account["email"], account["id"]
    code, r = api("POST", "/api/compose/send", {
        "account_id": aid, "to": ["dest@example.com"],
        "subject": "Hi there", "body_text": "SENDBODY content"})
    assert code == 200 and r["state"] == "queued"

    sends = dbfixture.pending_actions(email, "send")
    assert sends and "outbound_id" in sends[0]["payload"]
    assert "dest@example.com" in sends[0]["payload"]["rcpt_to"]

    # The agent reads the raw MIME straight from the outbound row.
    assert dbfixture.outbound_mime(sends[0]["payload"]["outbound_id"])


def test_send_with_attachment_bakes_it_into_the_mime(account):
    email, aid = account["email"], account["id"]
    code, up = upload_attachment(build_pdf("ATTACHSEND report"), "report.pdf", "application/pdf")
    assert code == 200 and up["id"]

    code, _ = api("POST", "/api/compose/send", {
        "account_id": aid, "to": ["dest@example.com"], "subject": "With a file",
        "body_text": "see attached", "attachments": [up["id"]]})
    assert code == 200

    oid = dbfixture.pending_actions(email, "send")[0]["payload"]["outbound_id"]
    mime = dbfixture.outbound_mime(oid)
    assert "report.pdf" in mime
    assert "application/pdf" in mime


def test_discarded_attachment_cannot_be_sent(account):
    code, up = upload_attachment(b"draft attachment", "draft.txt", "text/plain")
    assert code == 200
    assert api("DELETE", f"/api/compose/attachments/{up['id']}")[0] == 204

    code, _ = api("POST", "/api/compose/send", {
        "account_id": account["id"], "to": ["dest@example.com"],
        "subject": "Discarded file", "body_text": "body", "attachments": [up["id"]]})
    assert code == 400


def test_send_requires_recipient(account):
    code, _ = api("POST", "/api/compose/send",
                  {"account_id": account["id"], "to": [], "subject": "x", "body_text": "y"})
    assert code == 400


def _raw_mime_of_last_send(email: str) -> tuple[dict, str]:
    """The most recent queued send action and the MIME the agent would relay."""
    send = dbfixture.pending_actions(email, "send")[-1]
    return send, dbfixture.outbound_mime(send["payload"]["outbound_id"])


def test_agent_reports_send_addresses(account):
    email, aid = account["email"], account["id"]
    alias = f"alias-{uuid.uuid4().hex[:8]}@example.com"
    dbfixture.report_sync(email, addresses=[email, alias])

    code, accounts = api("GET", "/api/accounts")
    acc = next(a for a in accounts if a["id"] == aid)
    # Primary is implicit; only the extra alias is stored.
    assert acc["send_addresses"] == [alias.lower()]


def test_send_from_alias_sets_from_and_envelope(account):
    email, aid = account["email"], account["id"]
    alias = f"alias-{uuid.uuid4().hex[:8]}@example.com"
    dbfixture.report_sync(email, addresses=[alias])

    code, r = api("POST", "/api/compose/send", {
        "account_id": aid, "from_address": alias, "to": ["dest@example.com"],
        "subject": "From alias", "body_text": "hi from the alias"})
    assert code == 200, r

    send, mime = _raw_mime_of_last_send(email)
    assert send["payload"]["mail_from"] == alias        # SMTP envelope sender
    assert f"From: {alias}" in mime                     # header


def test_send_rejects_unowned_from_address(account):
    code, _ = api("POST", "/api/compose/send", {
        "account_id": account["id"], "from_address": "stranger@evil.com",
        "to": ["dest@example.com"], "subject": "nope", "body_text": "x"})
    assert code == 400


def test_send_defaults_from_to_primary(account):
    email, aid = account["email"], account["id"]
    code, _ = api("POST", "/api/compose/send", {
        "account_id": aid, "to": ["dest@example.com"], "subject": "default from", "body_text": "x"})
    assert code == 200
    send, mime = _raw_mime_of_last_send(email)
    assert send["payload"]["mail_from"] == email
    assert f"From: {email}" in mime


def test_send_never_appends_the_footer(account):
    """The footer is prefilled into the composer, so the body is sent verbatim —
    a message the user stripped it out of really goes without one."""
    email, aid = account["email"], account["id"]
    footer = "Ada Lovelace\nNorthwind Analytics"
    code, acc = api("PATCH", f"/api/accounts/{aid}", {"footer": footer})
    assert code == 200 and acc["footer"] == footer

    code, _ = api("POST", "/api/compose/send", {
        "account_id": aid, "to": ["dest@example.com"],
        "subject": "Without a footer", "body_text": "Short note."})
    assert code == 200

    _, mime = _raw_mime_of_last_send(email)
    body = mime.split("\n\n", 1)[1]
    assert body.strip() == "Short note."
    assert "Northwind Analytics" not in mime


def test_body_that_carries_a_footer_is_sent_as_typed(account):
    """What the composer sends — footer included — is what goes on the wire."""
    email, aid = account["email"], account["id"]
    body_text = "Short note.\n\nAda Lovelace\nNorthwind Analytics"

    code, _ = api("POST", "/api/compose/send", {
        "account_id": aid, "to": ["dest@example.com"],
        "subject": "With a footer", "body_text": body_text})
    assert code == 200

    _, mime = _raw_mime_of_last_send(email)
    body = mime.split("\n\n", 1)[1]
    assert body.strip() == body_text


def test_new_accounts_start_with_the_default_footer(account):
    """The footer is still an account setting — the composer reads it from here."""
    _, acc = api("GET", f"/api/accounts/{account['id']}")
    assert acc["footer"] == DEFAULT_FOOTER


def test_footer_is_stored_per_account(account):
    """Each account carries its own footer, overriding the shared default."""
    _, acc = api("GET", f"/api/accounts/{account['id']}")
    assert acc["footer"] == DEFAULT_FOOTER

    other = f"other-{uuid.uuid4().hex[:8]}@example.com"
    second = dbfixture.create_account(other, label="Other")
    try:
        api("PATCH", f"/api/accounts/{account['id']}", {"footer": "FOOTER-ONE"})
        api("PATCH", f"/api/accounts/{second['id']}", {"footer": "FOOTER-TWO"})

        _, first = api("GET", f"/api/accounts/{account['id']}")
        _, other_acc = api("GET", f"/api/accounts/{second['id']}")
        assert first["footer"] == "FOOTER-ONE"
        assert other_acc["footer"] == "FOOTER-TWO"
    finally:
        api("DELETE", f"/api/accounts/{second['id']}")


def test_reply_context_prefills_headers(account):
    email, aid = account["email"], account["id"]
    mid, rfc = ingest_one(email, aid, "REPLYTOK" + uuid.uuid4().hex[:6], frm="alice@ex.com")

    _, ctx = api("GET", f"/api/compose/reply-context/{mid}?mode=reply")
    assert ctx["to"] == ["alice@ex.com"]
    assert ctx["subject"].startswith("Re:")
    assert ctx["in_reply_to"] == rfc          # the original Message-ID
    assert rfc in ctx["references"]


def test_reply_defaults_from_to_the_addressed_alias(account):
    """A message delivered to one of the account's aliases should reply from it."""
    email, aid = account["email"], account["id"]
    alias = f"alias-{uuid.uuid4().hex[:8]}@example.com"
    dbfixture.report_sync(email, addresses=[alias])

    # Ingest a message addressed To: the alias (not the primary).
    rfc = f"<aliasmsg-{uuid.uuid4().hex}@t>"
    raw = make_message(rfc, "Hi alias", "alice@ex.com", alias, "body", T0)
    dbfixture.ingest_raw_message(email, raw, uid=991)

    _, sr = api("GET", f"/api/search?q=alias&account_id={aid}")
    mid = next(r["id"] for r in sr["rows"] if r.get("subject") == "Hi alias")

    _, ctx = api("GET", f"/api/compose/reply-context/{mid}?mode=reply")
    assert ctx["from_address"] == alias.lower()


def test_reply_all_carries_cc_but_drops_the_account_itself(account):
    """Reply-all keeps third-party Cc recipients; it never addresses you back."""
    email, aid = account["email"], account["id"]
    rfc = f"<ccmsg-{uuid.uuid4().hex}@t>"
    raw = make_message(rfc, "Testing Cc", "alice@ex.com", email, "body", T0,
                       cc=f"bob@ex.com, {email}")
    dbfixture.ingest_raw_message(email, raw, uid=992)

    _, sr = api("GET", f"/api/search?q=Testing&account_id={aid}")
    mid = next(r["id"] for r in sr["rows"] if r.get("subject") == "Testing Cc")

    # The reader needs Cc on the detail payload to be able to show it at all.
    _, msg = api("GET", f"/api/messages/{mid}")
    assert [r["address"] for r in msg["recipients"]["cc"]] == ["bob@ex.com", email.lower()]

    _, ctx = api("GET", f"/api/compose/reply-context/{mid}?mode=replyall")
    assert ctx["to"] == ["alice@ex.com"]
    assert ctx["cc"] == ["bob@ex.com"]


def test_forward_context(account):
    email, aid = account["email"], account["id"]
    mid, _ = ingest_one(email, aid, "FWDTOK" + uuid.uuid4().hex[:6])

    _, ctx = api("GET", f"/api/compose/reply-context/{mid}?mode=forward")
    assert ctx["subject"].startswith("Fwd:")
    assert ctx["to"] == []
    assert "Forwarded message" in ctx["body_text"]
