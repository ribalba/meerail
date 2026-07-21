"""Unit tests for the email parser. Pure — no DB or server needed."""

from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime

from core.mail.parse import html_to_text, normalize_subject, parse_email
from helpers import PNG_1x1, make_message

WHEN = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)


def test_basic_headers_and_body():
    raw = make_message("<a@x>", "Hello there", "Alice <alice@example.com>",
                       "me@proton.me", "This is UNIQUEBODY content.", WHEN)
    p = parse_email(raw)
    assert p.message_id == "a@x"
    assert p.from_addr == "alice@example.com"
    assert p.from_name == "Alice"
    assert p.subject == "Hello there"
    assert "UNIQUEBODY" in p.body_text
    assert "UNIQUEBODY" in p.snippet
    assert p.size_bytes == len(raw)


def test_references_folds_in_reply_to():
    raw = make_message("<b@x>", "Re: Hello", "b@example.com", "me@proton.me", "reply",
                       WHEN, in_reply_to="<a@x>")
    p = parse_email(raw)
    assert p.in_reply_to == "a@x"
    assert "a@x" in p.references  # In-Reply-To folded in even without a References header


def test_subject_normalization():
    assert normalize_subject("Re: Fwd:  Project Falcon") == "project falcon"
    assert normalize_subject("AW: WG: Rechnung") == "rechnung"
    assert normalize_subject("No prefix here") == "no prefix here"


def test_dedup_key_prefers_message_id():
    raw = make_message("<id@host>", "s", "a@b.com", "c@d.com", "body", WHEN)
    assert parse_email(raw).dedup_key == "id@host"


def test_dedup_key_hashes_when_message_id_missing():
    m = EmailMessage()
    m["Subject"] = "no id"
    m["From"] = "a@b.com"
    m["To"] = "c@d.com"
    m["Date"] = format_datetime(WHEN)
    m.set_content("body")
    p = parse_email(m.as_bytes())
    assert p.message_id is None
    assert p.dedup_key.startswith("sha256:")


def test_attachments_parsed():
    raw = make_message("<c@x>", "with files", "a@b.com", "c@d.com", "see attached", WHEN,
                       text_attachment="ATTACHTOKEN ledger\n", pdf_text="SECRETPDF quarterly")
    p = parse_email(raw)
    names = sorted(a.filename for a in p.attachments)
    assert names == ["notes.txt", "report.pdf"]
    assert p.attachments[0].payload  # bytes captured
    types = {a.content_type for a in p.attachments}
    assert "application/pdf" in types


def test_multiple_recipients():
    m = EmailMessage()
    m["Message-ID"] = "<r@x>"
    m["Subject"] = "many"
    m["From"] = "a@b.com"
    m["To"] = "One <one@x.com>, two@x.com"
    m["Cc"] = "three@x.com"
    m["Date"] = format_datetime(WHEN)
    m.set_content("hi")
    p = parse_email(m.as_bytes())
    to_addrs = {addr for _, addr in p.recipients["to"]}
    assert to_addrs == {"one@x.com", "two@x.com"}
    assert p.recipients["cc"][0][1] == "three@x.com"


def test_html_only_body_feeds_snippet():
    m = EmailMessage()
    m["Message-ID"] = "<h@x>"
    m["Subject"] = "html"
    m["From"] = "a@b.com"
    m["To"] = "c@d.com"
    m["Date"] = format_datetime(WHEN)
    m.set_content("<html><body><b>BOLDHTML</b> body text</body></html>", subtype="html")
    p = parse_email(m.as_bytes())
    assert "BOLDHTML" in p.body_html
    assert "BOLDHTML" in p.snippet  # snippet derived from stripped HTML when no plain part


def test_html_to_text_strips_tags():
    assert html_to_text("<p>Hello <b>World</b></p>") == "Hello World"
    assert html_to_text("") == ""


def test_inline_cid_image_captured():
    # Inline images live inside multipart/related and are NOT yielded by
    # iter_attachments(); the parser must still capture them so cid: resolves.
    m = EmailMessage()
    m["Message-ID"] = "<cidmsg@x>"
    m["Subject"] = "cid"
    m["From"] = "a@b.com"
    m["To"] = "c@d.com"
    m["Date"] = format_datetime(WHEN)
    m.set_content("text fallback")
    m.add_alternative('<img src="cid:pic1">', subtype="html")
    m.get_payload()[1].add_related(PNG_1x1, maintype="image", subtype="png", cid="<pic1>")
    p = parse_email(m.as_bytes())
    inline = next((a for a in p.attachments if a.content_id == "pic1"), None)
    assert inline is not None
    assert inline.is_inline
    assert inline.payload == PNG_1x1
