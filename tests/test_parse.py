"""Unit tests for the email parser. Pure — no DB or server needed."""

from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime

from core.mail.parse import (header_message_id, html_to_text, is_calendar_part,
                             leading_prefix, normalize_subject, parse_email)
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


def test_leading_prefix_reads_only_the_first_prefix_and_only_its_kind():
    """What the composer asks before adding its own: a reply to "Re: x" stays
    "Re: x", a reply to "Fwd: x" becomes "Re: Fwd: x". The localised prefixes
    count as their English equivalents, as they do for threading."""
    assert leading_prefix("Re: Schlüssel") == "reply"
    assert leading_prefix("RE: x") == "reply"
    assert leading_prefix("Re[2]: x") == "reply"
    assert leading_prefix("AW: WG: x") == "reply"
    assert leading_prefix("Re: Fwd: x") == "reply"
    assert leading_prefix("Fwd: Re: x") == "forward"
    assert leading_prefix("Fw: x") == "forward"
    assert leading_prefix("WG: x") == "forward"
    assert leading_prefix("Recipe for x") is None      # "Re" is not a prefix here
    assert leading_prefix("plain") is None
    assert leading_prefix("") is None
    assert leading_prefix(None) is None


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


# A forward the way Thunderbird makes one when the account signs its mail: the
# original is attached whole (it has to be — the signature covers a message that
# cannot then be edited down into a quote), and the body the person wrote is one
# part of a multipart/mixed that is itself wrapped in the signature. Both halves
# of that shape used to lose something: get_body stopped at the covering note, so
# the forward never reached the reader, and iter_attachments handed back the
# mixed container as though it were a file, so the message showed one empty
# attachment and none of its real ones.
_SIGNED_FORWARD = """\
Message-ID: <signed@x>
Subject: Fwd: the mission
From: Markus <markus@example.com>
To: me@proton.me
Date: {date}
MIME-Version: 1.0
Content-Type: multipart/signed; boundary="sig"; micalg=pgp-sha512;
 protocol="application/pgp-signature"

--sig
Content-Type: multipart/mixed; boundary="mix"

--mix
Content-Type: text/plain; charset=utf-8

COVERNOTE is this for you?

--mix
Content-Type: message/rfc822; name="the mission"

Message-ID: <inner@x>
From: Christian <christian@example.com>
To: markus@example.com
Subject: INNERSUBJECT the mission
Date: {date}
Content-Type: text/plain; charset=utf-8

INNERBODY the whole forwarded thing

--mix--

--sig
Content-Type: application/pgp-signature; name=OpenPGP_signature
Content-Disposition: attachment; filename=OpenPGP_signature

-----BEGIN PGP SIGNATURE-----
SIGBYTES
-----END PGP SIGNATURE-----

--sig--
"""


def _signed_forward() -> bytes:
    return _SIGNED_FORWARD.format(date=format_datetime(WHEN)).encode()


def test_attached_message_is_inlined_into_the_body():
    p = parse_email(_signed_forward())
    assert "COVERNOTE" in p.body_text          # the note the forwarder wrote
    assert "INNERBODY" in p.body_text          # and the message they forwarded
    # Under the headers a client writes when it quotes a forward inline, so the
    # reader can tell whose words are whose.
    assert "Forwarded message" in p.body_text
    assert "christian@example.com" in p.body_text
    assert "INNERSUBJECT" in p.body_text
    assert p.body_text.index("COVERNOTE") < p.body_text.index("INNERBODY")


def test_forwarded_body_reaches_the_snippet():
    # Nothing that only looks at body_text needs its own descent into the MIME
    # tree: search_text, the snippet and the summariser all read what is stored.
    p = parse_email(_SIGNED_FORWARD.format(date=format_datetime(WHEN))
                    .replace("COVERNOTE is this for you?", "").encode())
    assert "INNERBODY" in p.snippet


def test_signed_message_lists_its_files_not_its_containers():
    p = parse_email(_signed_forward())
    types = sorted(a.content_type for a in p.attachments)
    assert types == ["application/pgp-signature", "message/rfc822"]
    assert not any(a.content_type.startswith("multipart/") for a in p.attachments)
    assert all(a.payload for a in p.attachments)      # never a zero-byte container


def test_attached_message_is_stored_as_a_readable_eml():
    p = parse_email(_signed_forward())
    eml = next(a for a in p.attachments if a.content_type == "message/rfc822")
    assert eml.filename.endswith(".eml")
    assert b"INNERBODY" in eml.payload
    # The saved file is a message: it parses, and it is the one that was attached.
    assert parse_email(eml.payload).message_id == "inner@x"


def test_html_forward_under_a_plain_note_composes_an_html_body():
    inner = EmailMessage()
    inner["Message-ID"] = "<i2@x>"
    inner["Subject"] = "html original"
    inner["From"] = "orig@example.com"
    inner["To"] = "fwd@example.com"
    inner["Date"] = format_datetime(WHEN)
    inner.set_content("INNERPLAIN fallback")
    inner.add_alternative("<p><b>INNERHTML</b> formatted</p>", subtype="html")

    outer = EmailMessage()
    outer["Message-ID"] = "<o2@x>"
    outer["Subject"] = "Fwd: html original"
    outer["From"] = "a@b.com"
    outer["To"] = "c@d.com"
    outer["Date"] = format_datetime(WHEN)
    outer.set_content("COVERNOTE plain")
    outer.add_attachment(inner)

    p = parse_email(outer.as_bytes())
    # The forwarded HTML is what other clients show, so it has to survive — and
    # the plain note has to come with it, converted, or the composed body would
    # open on the forward with no sign of who sent it on.
    assert "INNERHTML" in p.body_html
    assert "COVERNOTE" in p.body_html
    assert "INNERPLAIN" in p.body_text and "COVERNOTE" in p.body_text


def test_message_without_a_forward_is_left_exactly_as_it_was():
    # An empty body_text is a signal: it is what makes build_search_text and the
    # summariser fall back to flattening the HTML. Composing a body for mail that
    # has nothing attached to it would silence that for the whole mailbox.
    m = EmailMessage()
    m["Message-ID"] = "<plainhtml@x>"
    m["Subject"] = "html"
    m["From"] = "a@b.com"
    m["To"] = "c@d.com"
    m["Date"] = format_datetime(WHEN)
    m.set_content("<html><body>ONLYHTML</body></html>", subtype="html")
    p = parse_email(m.as_bytes())
    assert p.body_text == ""
    assert "ONLYHTML" in p.body_html
    assert p.attachments == []


def test_forward_nesting_is_bounded():
    # Depth is bounded, so a message built out of a thousand nested forwards
    # parses instead of exhausting the stack. What is past the bound is dropped,
    # not raised over.
    inner = EmailMessage()
    inner["Subject"] = "seed"
    inner["From"] = "a@b.com"
    inner["Date"] = format_datetime(WHEN)
    inner.set_content("DEEPSEED")
    for depth in range(60):
        outer = EmailMessage()
        outer["Subject"] = f"Fwd: {depth}"
        outer["From"] = "a@b.com"
        outer["Date"] = format_datetime(WHEN)
        outer.set_content(f"LEVEL{depth}")
        outer.add_attachment(inner)
        inner = outer
    p = parse_email(inner.as_bytes())
    assert "LEVEL59" in p.body_text
    assert "DEEPSEED" not in p.body_text


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


def test_html_to_text_quotes_blockquotes_per_level():
    html = ("<p>new</p><blockquote><p>one</p><p>two</p>"
            "<blockquote>deep<br>er</blockquote></blockquote>after")
    # Off by default: the body fingerprint is stored and must not move.
    assert html_to_text(html) == "new\n\none\n\ntwo\n\ndeep\ner\n\nafter"
    # A reply to an HTML-only message quotes the conversation with this, so
    # each earlier message has to keep its own depth.
    assert html_to_text(html, quotes=True) == (
        "new\n\n> one\n>\n> two\n>\n> > deep\n> > er\n\nafter")
    assert html_to_text("<blockquote> </blockquote>x", quotes=True) == "x"


def test_html_to_text_spells_out_hrefs():
    html = '<p>See <a href="https://example.com/a?b=1">the docs</a> please</p>'
    # Off by default: snippets and the search corpus must not fill up with URLs.
    assert html_to_text(html) == "See the docs please"
    assert html_to_text(html, links=True) == "See the docs <https://example.com/a?b=1> please"


def test_html_to_text_omits_redundant_hrefs():
    # An autolinked URL is its own text; printing it twice helps nobody.
    assert html_to_text('<a href="https://example.com/">https://example.com</a>',
                        links=True) == "https://example.com"
    assert html_to_text('<a href="https://www.example.com">example.com</a>',
                        links=True) == "example.com"
    assert html_to_text('<a href="mailto:a@b.com">a@b.com</a>', links=True) == "a@b.com"
    # Anchors and script URLs name no destination worth reading.
    assert html_to_text('<a href="#top">back to top</a>', links=True) == "back to top"
    # A link with no text of its own keeps its address: it is all that is left.
    assert html_to_text('<a href="https://example.com/x"><img src="i.png"></a>',
                        links=True) == "<https://example.com/x>"


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


def test_header_message_id_ignores_the_from_address():
    # The header block the agent fetches is MESSAGE-ID, DATE, FROM and SUBJECT,
    # returned in the order the message wrote them — so on mail that puts From
    # first, the first <...> in the block is the sender's address, not the id.
    # Reading it as the id is what put deleted messages back in the inbox: the
    # "is this being moved?" lookup was made against annie@ergo-outlet.co.uk,
    # matched no stored message, and the sweep restored the placement.
    block = (
        b"From: Annie <annie@ergo-outlet.co.uk>\r\n"
        b"Date: Wed, 20 Aug 2026 10:00:31 +0000\r\n"
        b"Subject: Custom Ergonomic Chairs\r\n"
        b"Message-ID: <202620081000.a6gq4x9yqxirj@brevo.net>\r\n\r\n"
    )
    assert header_message_id(block) == "202620081000.a6gq4x9yqxirj@brevo.net"


def test_header_message_id_matches_what_parse_email_stores():
    # The whole point of it living here: the agent compares this against a
    # stored Message.message_id, so the two have to come off the same code.
    raw = make_message("<a@x>", "Hello there", "Alice <alice@example.com>",
                       "me@proton.me", "body", WHEN)
    head = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
    assert header_message_id(head) == parse_email(raw).message_id


def test_header_message_id_none_without_one():
    assert header_message_id(b"From: <a@b>\r\nSubject: no id\r\n\r\n") is None
    assert header_message_id(b"") is None
    assert header_message_id(None) is None


# The event an invitation carries. Base64 on the wire (EmailMessage encodes bytes
# that way), so what the parser decodes has to come back as exactly these bytes.
_VCALENDAR = (
    b"BEGIN:VCALENDAR\r\n"
    b"VERSION:2.0\r\n"
    b"PRODID:-//Microsoft Corporation//Outlook 16.0 MIMEDIR//EN\r\n"
    b"METHOD:REQUEST\r\n"
    b"BEGIN:VEVENT\r\n"
    b"UID:040000008200E00074C5B7101A82E00800000000\r\n"
    b"DTSTART:20260310T090000Z\r\n"
    b"DTEND:20260310T093000Z\r\n"
    b"SUMMARY:Planning\r\n"
    b"END:VEVENT\r\n"
    b"END:VCALENDAR\r\n"
)


def _outlook_invite(mid: str = "<invite@x>", cid: str | None = None,
                    name: str | None = None, repeat: bool = False) -> EmailMessage:
    """An invitation the way Outlook and Exchange send one.

    The event is a third alternative next to the text and the HTML, and no file
    is attached anywhere. iter_attachments() yields nothing for an alternative,
    so this shape used to be stored with no attachment at all and the
    invitation could not be found.
    """
    m = EmailMessage()
    m["Message-ID"] = mid
    m["Subject"] = "Invitation: Planning"
    m["From"] = "Organizer <org@example.com>"
    m["To"] = "me@proton.me"
    m["Date"] = format_datetime(WHEN)
    m.set_content("OUTLOOKBODY you are invited")
    m.add_alternative("<p>OUTLOOKHTML you are invited</p>", subtype="html")
    params = {"method": "REQUEST", "charset": "utf-8"}
    if name:
        params["name"] = name
    for _ in range(2 if repeat else 1):
        m.add_alternative(_VCALENDAR, maintype="text", subtype="calendar",
                          params=params, cid=cid)
    return m


def _calendars(p):
    return [a for a in p.attachments if is_calendar_part(a.content_type, a.filename)]


def test_outlook_invitation_in_an_alternative_is_stored_as_a_file():
    p = parse_email(_outlook_invite().as_bytes())
    assert len(p.attachments) == 1
    att = p.attachments[0]
    assert att.filename == "invite.ics"
    assert att.content_type == "text/calendar"
    # Not inline: the reader lists only non-inline attachments, and an invitation
    # nobody can see is the fault this fixes.
    assert att.is_inline is False
    assert att.content_id is None
    assert att.payload == _VCALENDAR
    # The body is still the text the sender wrote, not the calendar.
    assert "OUTLOOKBODY" in p.body_text
    assert "BEGIN:VCALENDAR" not in p.body_text
    assert "OUTLOOKHTML" in p.body_html


def test_gmail_invitation_is_stored_once():
    # Gmail and Thunderbird send the event twice, as an alternative and as an
    # invite.ics attachment. The attachment was always stored, and lifting the
    # alternative as well would show two files for one meeting.
    m = _outlook_invite("<gmail-invite@x>")
    m.add_attachment(_VCALENDAR, maintype="application", subtype="ics",
                     filename="invite.ics")
    assert m.get_content_type() == "multipart/mixed"
    p = parse_email(m.as_bytes())
    calendars = _calendars(p)
    assert len(calendars) == 1
    assert calendars[0].content_type == "application/ics"
    assert calendars[0].filename == "invite.ics"
    assert calendars[0].payload == _VCALENDAR


def test_invitation_inside_a_forwarded_message_stays_in_it():
    # The forwarded invitation belongs to the forwarded message, which is kept
    # whole as its .eml. Lifting it out would offer the reader somebody else's
    # meeting as though it had been sent to them.
    outer = EmailMessage()
    outer["Message-ID"] = "<fwd-invite@x>"
    outer["Subject"] = "Fwd: Invitation: Planning"
    outer["From"] = "a@b.com"
    outer["To"] = "c@d.com"
    outer["Date"] = format_datetime(WHEN)
    outer.set_content("COVERNOTE see below")
    outer.add_attachment(_outlook_invite("<inner-invite@x>"))
    p = parse_email(outer.as_bytes())
    assert [a.content_type for a in p.attachments] == ["message/rfc822"]
    assert _calendars(p) == []
    # The invitation is still there, inside the .eml it came in.
    assert _VCALENDAR in parse_email(p.attachments[0].payload).attachments[0].payload


def test_calendar_alternative_keeps_its_own_filename():
    p = parse_email(_outlook_invite(name="Planning.ics").as_bytes())
    assert [a.filename for a in p.attachments] == ["Planning.ics"]
    assert p.attachments[0].content_type == "text/calendar"


def test_calendar_alternative_with_a_content_id_is_stored_once():
    # A Content-ID would otherwise make the cid sweep file the same part a
    # second time, as an inline copy nobody sees.
    p = parse_email(_outlook_invite(cid="<cal1@x>").as_bytes())
    assert len(p.attachments) == 1
    assert p.attachments[0].content_id == "cal1@x"
    assert p.attachments[0].is_inline is False


def test_repeated_calendar_alternative_is_stored_once():
    p = parse_email(_outlook_invite(repeat=True).as_bytes())
    assert len(p.attachments) == 1
    assert p.attachments[0].payload == _VCALENDAR


def test_is_calendar_part():
    assert is_calendar_part("text/calendar; method=REQUEST", None)
    assert is_calendar_part("text/calendar", "")
    assert is_calendar_part("Application/ICS", None)
    assert is_calendar_part(" application/ics ; name=invite.ics", None)
    assert is_calendar_part("application/octet-stream", "Meeting.ICS")
    assert is_calendar_part(None, "invite.ics ")
    assert not is_calendar_part("text/plain", "notes.txt")
    assert not is_calendar_part("application/octet-stream", "calendar.ics.zip")
    assert not is_calendar_part("message/rfc822", "Invitation.eml")
    assert not is_calendar_part(None, None)
    assert not is_calendar_part("", "")


def test_a_calendar_part_marked_inline_is_still_listed():
    """Some senders put the text/calendar beside the body in multipart/mixed
    and mark it inline. iter_attachments() collects it, but an inline row is
    left out of the reader's list, so the one file the mail is about would be
    stored and never shown. It is also nameless, and gets the name a lifted
    invitation gets."""
    m = EmailMessage()
    m["Message-ID"] = "<inline-cal@x>"
    m["Subject"] = "Invitation: Planning"
    m["From"] = "org@example.com"
    m["To"] = "me@proton.me"
    m["Date"] = format_datetime(WHEN)
    m.set_content("INLINEBODY you are invited")
    m.add_attachment(_VCALENDAR, maintype="text", subtype="calendar",
                     disposition="inline", params={"method": "REQUEST"})

    p = parse_email(m.as_bytes())

    cals = _calendars(p)
    assert len(cals) == 1
    assert cals[0].is_inline is False
    assert cals[0].filename == "invite.ics"
    assert cals[0].payload == _VCALENDAR
    assert "INLINEBODY" in p.body_text
