"""Unit tests for the email parser. Pure — no DB or server needed."""

from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime

from core.mail.parse import html_to_text, normalize_subject, parse_email
from core.mail.threading import _new_thread_id
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


def test_long_headers_are_bounded_without_truncation_collisions():
    def parsed(suffix: str):
        m = EmailMessage()
        m["Message-ID"] = f"<{'x' * 280}{suffix}@host>"
        m["Subject"] = "Re: " + "S" * 700
        m["From"] = "a@b.com"
        m["To"] = "c@d.com"
        m["Date"] = format_datetime(WHEN)
        m.set_content("body")
        return parse_email(m.as_bytes())

    first = parsed("a")
    second = parsed("b")
    assert len(first.dedup_key) <= 255
    assert first.dedup_key != second.dedup_key
    assert len(first.subject_norm) == 512
    assert len(_new_thread_id(first.message_id)) <= 255


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


def test_html_to_text_keeps_line_structure():
    # A reply quotes this text line by line, so the sender's breaks must survive.
    assert html_to_text("one<br>two") == "one\ntwo"
    # One div per line is how Gmail composes: single break, not a blank line.
    assert html_to_text("<div>one</div><div>two</div>") == "one\ntwo"
    # An empty div, or a trailing <br>, is how a sender types a blank line.
    assert html_to_text("<div>one</div><div><br></div><div>two</div>") == "one\n\ntwo"
    assert html_to_text("<p>one</p><p>two</p>") == "one\n\ntwo"
    assert html_to_text("<ul><li>one</li><li>two</li></ul>") == "one\ntwo"
    # Markup and metadata are not body copy.
    assert html_to_text("<head><style>p{color:red}</style></head><p>hi</p>") == "hi"
    # Inline tags and runs of whitespace still collapse to single spaces.
    assert html_to_text("<p>a  <b>b</b>\n  c</p>") == "a b c"


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


def test_nul_bytes_stripped_from_all_text_fields():
    # PostgreSQL rejects NUL (0x00) in text columns, but U+0000 is valid UTF-8 so
    # charset decoding passes it straight through. A single such message used to
    # wedge the whole account's sync loop with a DataError.
    raw = (
        b"Message-ID: <nul@x>\r\n"
        b"Subject: Sub\x00ject\r\n"
        b"From: Al\x00ice <alice@example.com>\r\n"
        b"To: me@proton.me\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"body\x00with\x00nuls\r\n"
    )
    p = parse_email(raw)
    for field in (p.subject, p.subject_norm, p.from_name, p.from_addr,
                  p.body_text, p.body_html, p.snippet):
        assert "\x00" not in field, repr(field)
    # Stripping must remove only the NULs, not the surrounding text.
    assert p.subject == "Subject"
    assert p.from_name == "Alice"
    assert "bodywithnuls" in p.body_text


def test_nul_bytes_stripped_from_message_id_family():
    # The Message-ID family was the one decoded-header path that skipped
    # strip_nuls: <[^>]+> happily matches \x00 and str.strip() does not remove it
    # (U+0000 is not whitespace). These five fields all derive from
    # canonical_message_id, and references lands in JSONB, which rejects escaped
    # NULs exactly as the text columns reject raw ones.
    raw = (
        b"Message-ID: <nu\x00l@x>\r\n"
        b"In-Reply-To: <par\x00ent@x>\r\n"
        b"References: <gran\x00dparent@x> <par\x00ent@x>\r\n"
        b"Subject: Re: thread\r\n"
        b"From: alice@example.com\r\n"
        b"To: me@proton.me\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"body\r\n"
    )
    p = parse_email(raw)
    for field in (p.message_id, p.in_reply_to, p.dedup_key, *p.references):
        assert "\x00" not in field, repr(field)
    # Only the NULs go: the ID must still match the parent's cleaned Message-ID,
    # or threading silently breaks instead of raising.
    assert p.message_id == "nul@x"
    assert p.in_reply_to == "parent@x"
    assert p.references == ["grandparent@x", "parent@x"]


def test_nul_bytes_stripped_from_attachment_content_type():
    # get_content_type() only falls back to text/plain when the header lacks a
    # single slash, so "text/pl\x00ain" comes back verbatim. The filename beside
    # it was already stripped; the type was not.
    raw = (
        b"Message-ID: <ct@x>\r\n"
        b"Subject: with attachment\r\n"
        b"From: alice@example.com\r\n"
        b"To: me@proton.me\r\n"
        b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
        b"--b\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"body\r\n"
        b"--b\r\n"
        b'Content-Type: text/pl\x00ain; name="no\x00te.txt"\r\n'
        b'Content-Disposition: attachment; filename="no\x00te.txt"\r\n\r\n'
        b"payload\r\n"
        b"--b--\r\n"
    )
    p = parse_email(raw)
    assert len(p.attachments) == 1
    att = p.attachments[0]
    assert "\x00" not in att.content_type, repr(att.content_type)
    assert "\x00" not in att.filename, repr(att.filename)
    assert att.content_type == "text/plain"
