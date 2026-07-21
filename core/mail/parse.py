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


def _msgids(value: str | None) -> list[str]:
    return [m[1:-1].strip() for m in _MSGID_RE.findall(value or "")]


def _first(values: list[str]) -> str | None:
    return values[0] if values else None


def _addresses(msg: EmailMessage, header: str) -> list[tuple[str, str]]:
    raw = [str(v) for v in msg.get_all(header, [])]
    return [(name.strip(), addr.strip().lower()) for name, addr in getaddresses(raw) if addr]


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
        return part.get_content()
    except (LookupError, ValueError, KeyError):
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, "replace")
        except LookupError:
            return payload.decode("utf-8", "replace")


def html_to_text(html: str) -> str:
    if not html:
        return ""
    try:
        return HTMLParser(html).text(separator=" ", strip=True)
    except Exception:
        return ""


def normalize_subject(subject: str) -> str:
    s = _SUBJECT_PREFIX_RE.sub("", subject or "")
    return _WS_RE.sub(" ", s).strip().lower()


def make_snippet(text: str, limit: int = 240) -> str:
    s = _WS_RE.sub(" ", text or "").strip()
    return s[:limit]


def parse_email(raw: bytes) -> ParsedEmail:
    msg: EmailMessage = message_from_bytes(raw, policy=default_policy)  # type: ignore[assignment]

    message_id = _first(_msgids(str(msg.get("Message-ID", "")))) or None
    in_reply_to = _first(_msgids(str(msg.get("In-Reply-To", ""))))
    references = _msgids(str(msg.get("References", "")))
    # Some clients omit References but chain via In-Reply-To.
    if in_reply_to and in_reply_to not in references:
        references = references + [in_reply_to]

    subject = str(msg.get("Subject", "")).strip()
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
            cid = cid.strip().strip("<>").strip()
            seen_cids.add(cid)
        attachments.append(
            ParsedAttachment(
                filename=part.get_filename() or "attachment",
                content_type=(part.get_content_type() or "application/octet-stream").lower(),
                content_id=cid or None,
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
        cid = raw_cid.strip().strip("<>").strip()
        if not cid or cid in seen_cids:
            continue
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:
            payload = b""
        if not payload:
            continue
        ctype = (part.get_content_type() or "application/octet-stream").lower()
        filename = part.get_filename() or (cid + (mimetypes.guess_extension(ctype) or ""))
        seen_cids.add(cid)
        attachments.append(
            ParsedAttachment(filename=filename, content_type=ctype, content_id=cid,
                             is_inline=True, payload=payload)
        )

    # dedup_key: Message-ID when present, else a content hash so re-fetches of the
    # same bytes (e.g. from another Proton label) collapse to one Message row.
    if message_id:
        dedup_key = message_id[:255]
    else:
        dedup_key = "sha256:" + hashlib.sha256(raw).hexdigest()

    return ParsedEmail(
        message_id=message_id,
        dedup_key=dedup_key,
        in_reply_to=in_reply_to,
        references=references,
        subject=subject,
        subject_norm=normalize_subject(subject),
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
