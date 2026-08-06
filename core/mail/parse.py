"""Parse raw RFC822 bytes into the structured pieces meerail stores.

Uses the stdlib ``email`` package with the modern ``policy.default`` so header
decoding, ``get_body`` and ``iter_attachments`` do the MIME-tree walking for us.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import getaddresses, parsedate_to_datetime

from selectolax.parser import HTMLParser

_MSGID_RE = re.compile(r"<[^>]+>")
# Leading reply/forward prefixes in many languages, for subject-based threading.
_SUBJECT_PREFIX_RE = re.compile(r"^\s*((re|fwd|fw|aw|wg|sv|antw|rif|ref)\s*(\[\d+\])?\s*:\s*)+", re.I)
_WS_RE = re.compile(r"\s+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_MAX_MESSAGE_ID = 998

# Tags whose boundaries are line breaks when HTML is flattened to text. The
# "tight" ones separate lines within a block (a Gmail body is one div per
# line); the rest separate blocks and get a blank line between them.
_TIGHT_TAGS = frozenset({"dd", "div", "dt", "li", "td", "th", "tr"})
_BLOCK_TAGS = _TIGHT_TAGS | frozenset({
    "address", "article", "aside", "blockquote", "dl", "fieldset", "figure",
    "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
    "main", "nav", "ol", "p", "pre", "section", "table", "tbody", "tfoot",
    "thead", "ul",
})
_SOFT_BREAK = "\x01"
_HARD_BREAK = "\x02"
_BREAK_RUN_RE = re.compile(f"[ \t]*[{_SOFT_BREAK}{_HARD_BREAK}\n][ \t{_SOFT_BREAK}{_HARD_BREAK}\n]*")
# Tags whose text is markup or metadata, never body copy.
_SKIP_TAGS = frozenset({"head", "noscript", "script", "style", "title"})
# Hrefs that name no destination worth spelling out next to the link text.
_HREF_SKIP_RE = re.compile(r"^(#|javascript:|data:)", re.I)
_URL_NOISE_RE = re.compile(r"^(?:https?://|mailto:)?(?:www\.)?", re.I)


def strip_nuls(value: str) -> str:
    """Drop NUL bytes, which PostgreSQL rejects in text columns.

    U+0000 is valid UTF-8, so charset decoding with ``errors="replace"`` passes
    it straight through, and ``\\x00`` is not ``\\s`` so the whitespace-collapsing
    regexes miss it too. Senders produce it by mislabelling UTF-16 bodies as
    UTF-8 (every other byte is NUL) or by attaching truncated binary as text.
    """
    return value.replace("\x00", "") if value else value


@dataclass
class ParsedAttachment:
    filename: str
    content_type: str
    content_id: str | None
    is_inline: bool
    payload: bytes


@dataclass
class ParsedEmail:
    message_id: str | None
    dedup_key: str
    # A hash of the bytes this was parsed from — what identifies the message when
    # its Message-ID turns out not to. See the two keys at the end of parse_email.
    content_key: str
    in_reply_to: str | None
    references: list[str]
    subject: str
    subject_norm: str
    from_name: str
    from_addr: str
    # kind -> list of (name, address)
    recipients: dict[str, list[tuple[str, str]]]
    date_sent: datetime | None
    body_text: str
    body_html: str
    snippet: str
    size_bytes: int
    attachments: list[ParsedAttachment] = field(default_factory=list)


def canonical_message_id(value: str | None) -> str | None:
    """Fit an untrusted Message-ID into the schema without losing identity.

    RFC 5322 headers are not trustworthy enough to assume every sender obeys
    the line-length limit. Hashing malformed oversized IDs also keeps their
    References/In-Reply-To values comparable during threading.

    NULs go first, before the length check: this is the funnel every Message-ID
    passes through, so stripping here covers message_id, in_reply_to, references,
    dedup_key and thread_id at once. ``.strip()`` will not do it -- U+0000 is not
    whitespace -- and ``references`` lands in JSONB, which rejects \\u0000 just as
    the text columns do.
    """
    value = strip_nuls(value or "").strip()
    if not value:
        return None
    if len(value) <= _MAX_MESSAGE_ID:
        return value
    return "oversize-sha256:" + hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def _msgids(value: str | None) -> list[str]:
    return [mid for m in _MSGID_RE.findall(value or "")
            if (mid := canonical_message_id(m[1:-1]))]


def _first(values: list[str]) -> str | None:
    return values[0] if values else None


def _content_type(part: EmailMessage) -> str:
    """The part's MIME type, safe to store.

    ``get_content_type()`` only falls back to text/plain when the header has no
    single slash, so ``text/pl\\x00ain`` is returned verbatim -- one slash, and the
    NUL rides along into Attachment.content_type. Sanitise before the [:255]
    bound, which caps length but says nothing about content.
    """
    ctype = strip_nuls(part.get_content_type() or "").lower().strip()
    return (ctype or "application/octet-stream")[:255]


def _addresses(msg: EmailMessage, header: str) -> list[tuple[str, str]]:
    raw = [str(v) for v in msg.get_all(header, [])]
    return [
        (strip_nuls(name).strip()[:512], strip_nuls(addr).strip().lower()[:320])
        for name, addr in getaddresses(raw) if addr
    ]


def _normalize_dt(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _get_body(msg: EmailMessage, subtype: str) -> str:
    part = msg.get_body(preferencelist=(subtype,))
    if part is None:
        return ""
    try:
        return strip_nuls(part.get_content())
    except (LookupError, ValueError, KeyError):
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return strip_nuls(payload.decode(charset, "replace"))
        except LookupError:
            return strip_nuls(payload.decode("utf-8", "replace"))


def html_to_text(html: str, links: bool = False) -> str:
    """Flatten HTML to plain text, keeping the sender's line structure.

    ``<br>`` and block boundaries become newlines: a reply quotes this text
    line by line, so a paragraphed mail flattened to a single line would come
    back as one unreadable ``>`` run. Snippets and the search corpus collapse
    whitespace themselves, so the extra newlines cost them nothing.

    With ``links`` on, each anchor is followed by its address in angle brackets
    — the convention plain-text mail has always used. It is off by default
    because a snippet is 240 characters of preview and one tracking URL would
    eat all of them; the reader's plain-text view asks for it explicitly.
    """
    if not html:
        return ""
    try:
        tree = HTMLParser(html)
        parts: list[str] = []
        _flatten(tree.body or tree.root, parts, links)
    except Exception:
        return ""
    text = _BREAK_RUN_RE.sub(_break_run, "".join(parts))
    return _BLANK_LINES_RE.sub("\n\n", text).strip()


def _break_run(match: re.Match[str]) -> str:
    """Turn one run of adjacent break markers into one or two newlines.

    Nested tags stack up boundaries — ``</div></td><td><div>`` is four — and
    they mean one line break between them, not four. Explicit ``<br>``s do
    count individually, so a ``<br>`` at the end of a line still opens the
    blank line the sender typed. Whitespace around the run goes with it.
    """
    run = match.group()
    lines = run.count("\n") + (1 if run.strip(" \t\n") else 0)
    return "\n" * min(max(lines, 2 if _HARD_BREAK in run else 1), 2)


def _flatten(node, parts: list[str], links: bool = False) -> None:
    for child in node.iter(include_text=True):
        tag = child.tag
        if tag == "-text":
            # Newlines in the source are not breaks — senders hard-wrap their
            # HTML. Only <br> and block boundaries start a new line.
            parts.append(_WS_RE.sub(" ", child.text_content or ""))
        elif tag in _SKIP_TAGS:
            continue
        elif tag == "br":
            parts.append("\n")
        elif tag in _BLOCK_TAGS:
            mark = _SOFT_BREAK if tag in _TIGHT_TAGS else _HARD_BREAK
            parts.append(mark)
            _flatten(child, parts, links)
            parts.append(mark)
        elif tag == "a" and links:
            start = len(parts)
            _flatten(child, parts, links)
            href = _spelled_href(child, "".join(parts[start:]))
            if href:
                parts.append(f" <{href}>")
        else:
            _flatten(child, parts, links)


def _bare_url(url: str) -> str:
    """A URL reduced to what makes two spellings of it the same link."""
    return _URL_NOISE_RE.sub("", url).rstrip("/").lower()


def _spelled_href(node, text: str) -> str:
    """The address to write after a link's text, or ``""`` to leave it implicit.

    A link whose text already *is* the address — which is most of them once a
    client has autolinked a URL — would otherwise be printed twice. Anchors
    with no text at all (an image, a bare tracking pixel) keep theirs: it is
    the only trace the link leaves in the flattened copy.
    """
    href = (node.attributes.get("href") or "").strip()
    if not href or _HREF_SKIP_RE.match(href):
        return ""
    shown = _WS_RE.sub("", text.replace(_SOFT_BREAK, "").replace(_HARD_BREAK, ""))
    return "" if shown and _bare_url(shown) == _bare_url(href) else href


def normalize_subject(subject: str) -> str:
    s = _SUBJECT_PREFIX_RE.sub("", subject or "")
    return _WS_RE.sub(" ", s).strip().lower()


def looks_like_reply(subject: str) -> bool:
    """Whether the subject carries a reply/forward prefix (``Re:``, ``Fwd:``, …).

    Subject-based threading uses this to tell a continuation from a fresh root:
    a message with no References *and* no prefix starts a conversation, it does
    not join one.
    """
    return bool(_SUBJECT_PREFIX_RE.match(subject or ""))


def make_snippet(text: str, limit: int = 240) -> str:
    s = _WS_RE.sub(" ", text or "").strip()
    return s[:limit]


def content_key(raw: bytes) -> str:
    """What these bytes are, as a name: a hash of the message itself.

    The one identity a sender cannot write. A Message-ID is a header and two
    different messages can carry the same one; this is what decides between them
    (core/mail/store.py::same_message), what a collision is filed under, and what
    the agent compares a copy against before it removes an original.

    Line endings are normalised out of it first. The same message reaches us
    through more than one transport — over IMAP with CRLF, out of an mbox with
    those endings already converted to LF — and hashing what arrived would call
    those two different messages, which for the importer means duplicating every
    message in an archive that has also been synced. What is left is still the
    message: every byte of it, in one spelling.
    """
    return "sha256:" + hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def header_identity(header_bytes: bytes | None) -> tuple[str, str] | None:
    """``(from_addr, subject_norm)`` out of a header block, or None if there is
    none to read.

    The same two fields ``parse_email`` derives from a whole message, derived the
    same way and from the same code, so that a comparison between a message on
    the wire and a message in the database is a comparison of like with like.
    That is the entire reason this exists as a function rather than as two lines
    at the call site: the agent decides whether to fetch a message at all by
    holding its headers up against a stored row, and a difference in how the two
    sides were normalised would read as a difference between the messages.
    """
    if not header_bytes:
        return None
    msg: EmailMessage = message_from_bytes(header_bytes, policy=default_policy)  # type: ignore[assignment]
    from_pairs = _addresses(msg, "From")
    subject = strip_nuls(str(msg.get("Subject", ""))).strip()
    return (from_pairs[0][1] if from_pairs else ""), normalize_subject(subject)[:512]


def parse_email(raw: bytes) -> ParsedEmail:
    msg: EmailMessage = message_from_bytes(raw, policy=default_policy)  # type: ignore[assignment]

    message_id = _first(_msgids(str(msg.get("Message-ID", "")))) or None
    in_reply_to = _first(_msgids(str(msg.get("In-Reply-To", ""))))
    references = _msgids(str(msg.get("References", "")))
    # Some clients omit References but chain via In-Reply-To.
    if in_reply_to and in_reply_to not in references:
        references = references + [in_reply_to]

    subject = strip_nuls(str(msg.get("Subject", ""))).strip()
    from_pairs = _addresses(msg, "From")
    from_name, from_addr = (from_pairs[0] if from_pairs else ("", ""))

    recipients = {
        "from": from_pairs,
        "to": _addresses(msg, "To"),
        "cc": _addresses(msg, "Cc"),
        "bcc": _addresses(msg, "Bcc"),
        "reply_to": _addresses(msg, "Reply-To"),
    }

    date_sent = None
    try:
        date_sent = _normalize_dt(parsedate_to_datetime(str(msg["Date"])))
    except (TypeError, ValueError, IndexError):
        date_sent = None

    body_text = _get_body(msg, "plain")
    body_html = _get_body(msg, "html")
    text_for_snippet = body_text or html_to_text(body_html)
    snippet = make_snippet(text_for_snippet)

    attachments: list[ParsedAttachment] = []
    seen_cids: set[str] = set()
    for part in msg.iter_attachments():
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:
            payload = b""
        cid = part.get("Content-ID")
        if cid:
            cid = strip_nuls(cid).strip().strip("<>").strip()
            seen_cids.add(cid)
        attachments.append(
            ParsedAttachment(
                filename=strip_nuls(part.get_filename() or "attachment")[:1024],
                content_type=_content_type(part),
                content_id=(cid[:512] if cid else None),
                is_inline=(part.get_content_disposition() == "inline"),
                payload=payload,
            )
        )

    # iter_attachments() skips inline images inside multipart/related (they count
    # as part of the body), so cid: references would have nothing to resolve to.
    # Capture any part carrying a Content-ID that we haven't already collected.
    for part in msg.walk():
        if part.is_multipart():
            continue
        raw_cid = part.get("Content-ID")
        if not raw_cid:
            continue
        cid = strip_nuls(raw_cid).strip().strip("<>").strip()
        if not cid or cid in seen_cids:
            continue
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:
            payload = b""
        if not payload:
            continue
        ctype = _content_type(part)
        filename = strip_nuls(part.get_filename() or "") or (cid + (mimetypes.guess_extension(ctype) or ""))
        seen_cids.add(cid)
        attachments.append(
            ParsedAttachment(filename=filename[:1024], content_type=ctype, content_id=cid[:512],
                             is_inline=True, payload=payload)
        )

    # Two keys, because a Message-ID answers a different question than it looks
    # like it does.
    #
    # dedup_key is what collapses one message seen several times into one row:
    # the Message-ID when there is one, since a Proton or Gmail account hands the
    # same message over once per label and each copy has its own UID. That works
    # because senders normally issue a unique Message-ID per message — normally,
    # and not always. Nothing stops two different messages carrying the same one:
    # a mailer with a broken generator, a list that re-sends under the old id, or
    # anybody who simply typed the header. So the id is a *candidate* for
    # sameness, checked against the message itself before anything is merged (see
    # core/mail/store.py::_same_message).
    #
    # content_key is what the check falls back to, and the whole key when there
    # is no Message-ID at all: the bytes, which cannot be two messages.
    key = content_key(raw)
    if message_id:
        dedup_key = (message_id if len(message_id) <= 255 else
                     "mid-sha256:" + hashlib.sha256(message_id.encode()).hexdigest())
    else:
        dedup_key = key

    return ParsedEmail(
        message_id=message_id,
        dedup_key=dedup_key,
        content_key=key,
        in_reply_to=in_reply_to,
        references=references,
        subject=subject,
        subject_norm=normalize_subject(subject)[:512],
        from_name=from_name,
        from_addr=from_addr,
        recipients=recipients,
        date_sent=date_sent,
        body_text=body_text,
        body_html=body_html,
        snippet=snippet,
        size_bytes=len(raw),
        attachments=attachments,
    )
