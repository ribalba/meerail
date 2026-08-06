"""Integration tests for compose: send enqueues a send action; reply-context."""

import uuid
from datetime import datetime, timedelta, timezone
from email import message_from_string, policy

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


def test_agent_reports_display_names(account):
    email, aid = account["email"], account["id"]
    alias = f"alias-{uuid.uuid4().hex[:8]}@example.com"
    dbfixture.report_sync(email, addresses=[f"Arne Tarara <{email}>",
                                            f"Work Arne <{alias}>"])

    code, accounts = api("GET", "/api/accounts")
    acc = next(a for a in accounts if a["id"] == aid)
    assert acc["send_addresses"] == [alias.lower()]
    # Keyed by address, primary included, and the case of the name survives —
    # only the address is lower-cased.
    assert acc["send_names"] == {email.lower(): "Arne Tarara",
                                 alias.lower(): "Work Arne"}


def test_send_puts_the_display_name_on_the_from_header(account):
    email, aid = account["email"], account["id"]
    alias = f"alias-{uuid.uuid4().hex[:8]}@example.com"
    dbfixture.report_sync(email, addresses=[f"Work Arne <{alias}>"])

    code, r = api("POST", "/api/compose/send", {
        "account_id": aid, "from_address": alias, "to": ["dest@example.com"],
        "subject": "Named", "body_text": "hi"})
    assert code == 200, r

    send, mime = _raw_mime_of_last_send(email)
    assert f"From: Work Arne <{alias}>" in mime
    # The envelope sender stays a bare address — Proton relays on that.
    assert send["payload"]["mail_from"] == alias


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


# --- presentation pinned in meerail.toml -------------------------------------
#
# Name, colour and footer normally belong to Settings. An agent config may take
# any of them over, in which case the agent writes the value on every pass and
# the API has to refuse edits rather than accept one the next sync would undo.


def test_pinned_fields_are_written_and_declared(account):
    aid = account["id"]
    dbfixture.report_presentation(account["email"],
                                  {"label": "Configured", "color": "#ff8800"})

    _, acc = api("GET", f"/api/accounts/{aid}")
    assert (acc["label"], acc["color"]) == ("Configured", "#ff8800")
    # What the UI reads to show them as set-elsewhere instead of editable.
    assert acc["config_fields"] == ["color", "label"]
    # Untouched by the file, so still the UI's to edit.
    assert acc["footer"] == DEFAULT_FOOTER


def test_patching_a_pinned_field_is_refused(account):
    aid = account["id"]
    dbfixture.report_presentation(account["email"], {"label": "Configured"})

    code, body = api("PATCH", f"/api/accounts/{aid}", {"label": "By hand"})
    assert code == 409 and "meerail.toml" in body["detail"]
    _, acc = api("GET", f"/api/accounts/{aid}")
    assert acc["label"] == "Configured"


def test_fields_the_file_leaves_alone_stay_editable(account):
    dbfixture.report_presentation(account["email"], {"label": "Configured"})

    code, acc = api("PATCH", f"/api/accounts/{account['id']}", {"color": "#00ff00"})
    assert code == 200 and acc["color"] == "#00ff00"


def test_a_pinned_footer_overrides_one_saved_in_settings(account):
    """The file is the source of truth for what it names — including over a
    footer the UI saved before the key was added."""
    api("PATCH", f"/api/accounts/{account['id']}", {"footer": "FROM-SETTINGS"})
    dbfixture.report_presentation(account["email"], {"footer": "FROM-THE-FILE"})

    _, acc = api("GET", f"/api/accounts/{account['id']}")
    assert acc["footer"] == "FROM-THE-FILE"
    assert acc["config_fields"] == ["footer"]


def test_dropping_a_key_hands_the_field_back(account):
    """Removing it from the file unlocks the field, keeping the value the file
    last gave it — nothing silently reverts to a default."""
    dbfixture.report_presentation(account["email"], {"label": "Configured"})
    dbfixture.report_presentation(account["email"], {})

    _, acc = api("GET", f"/api/accounts/{account['id']}")
    assert acc["config_fields"] == []
    assert acc["label"] == "Configured"
    code, acc = api("PATCH", f"/api/accounts/{account['id']}", {"label": "By hand"})
    assert code == 200 and acc["label"] == "By hand"


def test_a_pinned_empty_footer_means_no_footer(account):
    """`footer = ""` in the file is an answer, not a missing value: it must not
    be read as "unset" and refilled with the default."""
    dbfixture.report_presentation(account["email"], {"footer": ""})

    _, acc = api("GET", f"/api/accounts/{account['id']}")
    assert acc["footer"] == ""
    assert acc["config_fields"] == ["footer"]


# --- "Send as HTML email" ----------------------------------------------------
#
# The button makes the message an HTML one, not an HTML alternative to a
# plain-text one. multipart/alternative is the textbook shape and is what this
# sent at first, but a message carrying both renderings arrives as raw markdown:
# Proton keeps one body per message and, handed the pair, keeps the plain text.
# So what these pin down is that a formatted message is text/html with nothing
# to choose between — and that with the button off, nothing changed.


def _parts(mime: str) -> dict[str, str]:
    """Every leaf part of a message, keyed by content type."""
    parsed = message_from_string(mime, policy=policy.default)
    return {p.get_content_type(): p.get_content()
            for p in parsed.walk() if not p.is_multipart()}


def test_formatted_send_is_an_html_message(account):
    """A single text/html body — no alternative to pick the wrong half of."""
    email, aid = account["email"], account["id"]
    code, _ = api("POST", "/api/compose/send", {
        "account_id": aid, "to": ["dest@example.com"], "subject": "Formatted",
        "body_text": "# Heading\n\nSome **bold** text.",
        "body_html": "<html><body><h1>Heading</h1><p>Some <strong>bold</strong> text.</p></body></html>"})
    assert code == 200

    _, mime = _raw_mime_of_last_send(email)
    parsed = message_from_string(mime, policy=policy.default)
    assert parsed.get_content_type() == "text/html"
    assert not parsed.is_multipart()
    assert "<strong>bold</strong>" in parsed.get_content()
    assert "multipart" not in mime


def test_formatted_send_still_records_the_markdown_source(account):
    """The source stops being on the wire, so the outbound row is the only place
    left that knows what was actually typed. It has to keep it."""
    email, aid = account["email"], account["id"]
    api("POST", "/api/compose/send", {
        "account_id": aid, "to": ["dest@example.com"], "subject": "Source",
        "body_text": "# Heading\n\nSome **bold** text.",
        "body_html": "<html><body><h1>Heading</h1></body></html>"})

    send = dbfixture.pending_actions(email, "send")[-1]
    assert dbfixture.outbound_body_text(send["payload"]["outbound_id"]) == \
        "# Heading\n\nSome **bold** text."


def test_unformatted_send_stays_a_plain_text_message(account):
    """Without the button the message is exactly what it always was — no HTML
    anywhere, and no multipart wrapper to make a plain note look like one."""
    email, aid = account["email"], account["id"]
    api("POST", "/api/compose/send", {
        "account_id": aid, "to": ["dest@example.com"],
        "subject": "Plain", "body_text": "just text"})

    _, mime = _raw_mime_of_last_send(email)
    assert list(_parts(mime)) == ["text/plain"]
    assert "multipart" not in mime
    assert "text/html" not in mime


def test_no_body_part_declares_its_own_mime_version(account):
    """MIME-Version describes the message, not a piece of it. Python stamps one
    on the parts it builds for an attachment, and a part carrying it can read to
    a gateway as an encapsulated message rather than as content to display."""
    email, aid = account["email"], account["id"]
    code, up = upload_attachment(build_pdf("MIMEVERSION"), "v.pdf", "application/pdf")
    assert code == 200
    api("POST", "/api/compose/send", {
        "account_id": aid, "to": ["dest@example.com"], "subject": "Headers",
        "body_text": "text", "body_html": "<html><body><p>text</p></body></html>",
        "attachments": [up["id"]]})

    _, mime = _raw_mime_of_last_send(email)
    parsed = message_from_string(mime, policy=policy.default)
    assert parsed["MIME-Version"] == "1.0"          # the message still declares it
    assert [p["MIME-Version"] for p in parsed.walk() if p is not parsed] == [None, None]


def test_formatted_send_still_carries_attachments(account):
    """A file makes it multipart/mixed with the HTML as the body inside it.
    Mixed is a different question from alternative — nothing has to choose
    between these parts, so nothing can choose wrong."""
    email, aid = account["email"], account["id"]
    code, up = upload_attachment(build_pdf("FORMATTEDATTACH"), "note.pdf", "application/pdf")
    assert code == 200

    code, _ = api("POST", "/api/compose/send", {
        "account_id": aid, "to": ["dest@example.com"], "subject": "Formatted with a file",
        "body_text": "see attached", "body_html": "<html><body><p>see attached</p></body></html>",
        "attachments": [up["id"]]})
    assert code == 200

    _, mime = _raw_mime_of_last_send(email)
    parsed = message_from_string(mime, policy=policy.default)
    assert parsed.get_content_type() == "multipart/mixed"
    leaves = [p.get_content_type() for p in parsed.walk() if not p.is_multipart()]
    assert leaves == ["text/html", "application/pdf"]
    assert "<p>see attached</p>" in parsed.get_body(("html",)).get_content()
    assert "note.pdf" in mime
    assert "multipart/alternative" not in mime


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


# --- Sender suggestion ------------------------------------------------------


def _sent(email: str, frm: str, to: str, uid: int, when=T0, cc: str | None = None) -> None:
    """Record one message the user sent, the way a Sent-folder sync would."""
    raw = make_message(f"<sent-{uuid.uuid4().hex}@t>", "Prior mail", frm, to, "body", when, cc=cc)
    dbfixture.ingest_raw_message(email, raw, uid=uid, folder="Sent", role_hint="sent")


def _sender_for(*addresses: str):
    query = "&".join(f"address={a}" for a in addresses)
    code, body = api("GET", f"/api/compose/sender-for?{query}")
    assert code == 200
    return body


def test_sender_suggestion_follows_past_mail(account):
    """The From offered for a recipient is the address they were written to from."""
    email = account["email"]
    alias = f"alias-{uuid.uuid4().hex[:8]}@example.com"
    dbfixture.report_sync(email, addresses=[alias])
    bob = f"bob-{uuid.uuid4().hex[:8]}@ex.test"
    _sent(email, alias, bob, uid=801)

    hit = _sender_for(bob)
    assert hit["address"] == alias.lower()
    assert hit["account_id"] == account["id"]


def test_sender_suggestion_is_silent_without_history(account):
    """An unknown recipient gets no answer, so the composer keeps its own From."""
    dbfixture.report_sync(account["email"], addresses=[f"alias-{uuid.uuid4().hex[:8]}@example.com"])
    assert _sender_for(f"stranger-{uuid.uuid4().hex[:8]}@ex.test") is None


def test_sender_suggestion_prefers_the_address_used_most(account):
    """Two addresses have written to this person; the habitual one wins."""
    email = account["email"]
    alias = f"alias-{uuid.uuid4().hex[:8]}@example.com"
    dbfixture.report_sync(email, addresses=[alias])
    carol = f"carol-{uuid.uuid4().hex[:8]}@ex.test"
    _sent(email, alias, carol, uid=811)
    _sent(email, alias, carol, uid=812)
    _sent(email, email, carol, uid=813)

    assert _sender_for(carol)["address"] == alias.lower()


def test_sender_suggestion_covers_as_many_recipients_as_it_can(account):
    """An address that has written to both people beats one that knows only one,
    however often that one has been written to."""
    email = account["email"]
    alias = f"alias-{uuid.uuid4().hex[:8]}@example.com"
    dbfixture.report_sync(email, addresses=[alias])
    dave, erin = (f"{n}-{uuid.uuid4().hex[:8]}@ex.test" for n in ("dave", "erin"))
    for uid in (821, 822, 823):
        _sent(email, email, dave, uid=uid)          # primary: three mails, one of the two
    _sent(email, alias, dave, uid=824)              # alias: one each, but both of them
    _sent(email, alias, erin, uid=825)

    assert _sender_for(dave)["address"] == email                 # alone, the primary
    assert _sender_for(dave, erin)["address"] == alias.lower()   # together, the alias


def test_sender_suggestion_prefers_a_habit_you_still_have(account):
    """Years of mail from an address since moved off must not outvote the
    handful sent from the one in use now."""
    email = account["email"]
    alias = f"alias-{uuid.uuid4().hex[:8]}@example.com"
    dbfixture.report_sync(email, addresses=[alias])
    ivan = f"ivan-{uuid.uuid4().hex[:8]}@ex.test"
    # Dated against the clock, not the suite's fixed T0: the year the server
    # counts as "recent" runs backwards from today, not from 2026.
    now = datetime.now(timezone.utc)
    for uid in (851, 852, 853, 854):
        _sent(email, email, ivan, uid=uid, when=now - timedelta(days=900))
    _sent(email, alias, ivan, uid=855, when=now - timedelta(days=30))

    assert _sender_for(ivan)["address"] == alias.lower()


def test_sender_suggestion_counts_cc_recipients(account):
    """Being Cc'd is being written to — the From that did it still counts."""
    email = account["email"]
    alias = f"alias-{uuid.uuid4().hex[:8]}@example.com"
    dbfixture.report_sync(email, addresses=[alias])
    frank = f"frank-{uuid.uuid4().hex[:8]}@ex.test"
    _sent(email, alias, "someone@ex.test", uid=831, cc=frank)

    assert _sender_for(frank)["address"] == alias.lower()


def test_sender_suggestion_ignores_mail_the_user_did_not_send(account):
    """Mail *from* a contact says nothing about which address to answer from —
    only the user's own sent mail is evidence."""
    email = account["email"]
    dbfixture.report_sync(email, addresses=[f"alias-{uuid.uuid4().hex[:8]}@example.com"])
    grace = f"grace-{uuid.uuid4().hex[:8]}@ex.test"
    raw = make_message(f"<in-{uuid.uuid4().hex}@t>", "Incoming", grace, email, "body", T0)
    dbfixture.ingest_raw_message(email, raw, uid=841)

    assert _sender_for(grace) is None


def test_forward_context(account):
    email, aid = account["email"], account["id"]
    mid, _ = ingest_one(email, aid, "FWDTOK" + uuid.uuid4().hex[:6])

    _, ctx = api("GET", f"/api/compose/reply-context/{mid}?mode=forward")
    assert ctx["subject"].startswith("Fwd:")
    assert ctx["to"] == []
    assert "Forwarded message" in ctx["body_text"]
    assert ctx["attachments"] == []          # nothing was attached to forward


def test_forward_carries_the_attachments(account):
    """A forward goes out with the original's files, not just its text."""
    email, aid = account["email"], account["id"]
    token = "FWDATT" + uuid.uuid4().hex[:6]
    raw = make_message(f"<{uuid.uuid4().hex}@t>", f"Subj {token}", "sender@ex.com", email,
                       f"{token} body text", T0, pdf_text="FWDATT report")
    dbfixture.ingest_raw_message(email, raw, uid=871)
    _, found = api("GET", f"/api/search?q={token}&account_id={aid}")
    mid = found["rows"][0]["id"]

    _, ctx = api("GET", f"/api/compose/reply-context/{mid}?mode=forward")
    assert [a["filename"] for a in ctx["attachments"]] == ["report.pdf"]
    assert ctx["attachments"][0]["content_type"] == "application/pdf"
    assert ctx["attachments"][0]["size"] > 0
    assert ctx["attachments_missing"] == 0

    # The composer sends the staged ids back like any other attachment.
    code, _ = api("POST", "/api/compose/send", {
        "account_id": aid, "to": ["dest@example.com"], "subject": ctx["subject"],
        "body_text": ctx["body_text"], "attachments": [a["id"] for a in ctx["attachments"]]})
    assert code == 200

    oid = dbfixture.pending_actions(email, "send")[0]["payload"]["outbound_id"]
    mime = dbfixture.outbound_mime(oid)
    assert "report.pdf" in mime and "application/pdf" in mime


def test_the_outbox_count_reports_what_is_still_waiting(account):
    """The UI's outbox strip reads this: sending is the agent's job, so between
    pressing send and the agent relaying it there is a real interval — seconds
    with a connection, days without one — and it used to be invisible."""
    aid = account["id"]
    _, before = api("GET", "/api/sync/status")
    start = before["outbox"]["queued"]

    api("POST", "/api/compose/send", {
        "account_id": aid, "to": ["dest@example.com"],
        "subject": "Queued", "body_text": "waiting for the agent"})

    _, after = api("GET", "/api/sync/status")
    ob = after["outbox"]
    assert ob["queued"] == start + 1
    assert ob["oldest_at"]
    # Nothing has failed, so there is no reason for the strip to go red.
    assert ob["error"] is None
    assert ob["abandoned"] == 0


def test_a_failing_send_says_why_without_leaving_the_queue(account):
    """A send that fails keeps its place in the outbox and reports the reason —
    the state issue #7 had no way of showing at all."""
    email, aid = account["email"], account["id"]
    api("POST", "/api/compose/send", {
        "account_id": aid, "to": ["dest@example.com"],
        "subject": "Stuck", "body_text": "no smtp here"})
    oid = dbfixture.pending_actions(email, "send")[0]["payload"]["outbound_id"]

    # What the agent writes when an attempt fails: still queued, with a reason.
    dbfixture.record_send_failure(oid, "TimeoutError('timed out')")

    _, body = api("GET", "/api/sync/status")
    assert body["outbox"]["queued"] >= 1
    assert "timed out" in body["outbox"]["error"]


# --- What the upload route does with a body it cannot parse -------------------
#
# This route reads the multipart itself rather than declaring `UploadFile`,
# because a declared body is parsed *before* dependencies run — so the ordinary
# signature spooled a stranger's upload to disk and only then checked whether
# they were allowed to make the request at all. Parsing it here also means
# owning the errors FastAPI used to turn into responses on the way in.


def _post_raw(body: bytes, content_type: str):
    import json as _json
    import urllib.error
    import urllib.request

    from helpers import SERVER

    req = urllib.request.Request(SERVER + "/api/compose/attachments", data=body,
                                 method="POST", headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, _json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def test_a_body_that_is_not_the_multipart_it_claims_is_a_400(account):
    """Not a 500. The request is wrong, and saying which is the difference
    between a client bug someone can fix and a server that looks broken."""
    code, _ = _post_raw(b"this is not multipart at all",
                        "multipart/form-data; boundary=----nope")

    assert code == 400


def test_a_multipart_with_no_file_in_it_says_so(account):
    """A well-formed body carrying only fields. There is nothing to stage, and
    the caller needs to know that rather than getting an id for nothing."""
    boundary = "----meerailempty"
    body = (f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="note"\r\n\r\nhello\r\n'
            f"--{boundary}--\r\n").encode()
    code, _ = _post_raw(body, f"multipart/form-data; boundary={boundary}")

    assert code in (400, 422)
