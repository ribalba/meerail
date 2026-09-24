"""Compose + send. The server builds the RFC822 message (including attachments
staged via /attachments); the agent fetches it and relays over SMTP. Reply/forward
prefill (recipients, quoting, threading headers) is computed here."""

from __future__ import annotations

import mimetypes
import re
import uuid
from datetime import timedelta
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from html import escape as html_escape
from pathlib import Path
from typing import NamedTuple
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy import distinct, func, select, tuple_
from sqlalchemy.orm import Session as DBSession, defer
from selectolax.lexbor import LexborHTMLParser
# Starlette's own, not the FastAPI subclass: the form is parsed by Starlette
# here (see upload_attachment) and produces the base class, which a FastAPI
# UploadFile is not an instance of.
from starlette.datastructures import UploadFile
# Both, because a malformed multipart raises one of two unrelated exceptions and
# neither is caught for us any more. Starlette raises MultiPartException for the
# limits it enforces itself (too many files, a part over its cap, no boundary);
# python-multipart raises MultipartParseError from inside the parser when the
# bytes do not match the boundary that was declared. FastAPI used to turn both
# into a 400 on the way in, and does not see this body at all now.
from starlette.formparsers import MultiPartException
from python_multipart.exceptions import MultipartParseError

_MALFORMED_MULTIPART = (MultiPartException, MultipartParseError)

from core import outbox as outbox_core
from core.config import get_settings
from core.database import get_db
from .. import events, mailops, staging
from ..deps import require_ui_auth
from .messages import _readable
from core.models import Account, Attachment, Message, Outbound, PendingAction, Recipient, utcnow
from core.mail.parse import html_to_text, leading_prefix

router = APIRouter(prefix="/api/compose", tags=["compose"], dependencies=[Depends(require_ui_auth)])
settings = get_settings()
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(name: str) -> str:
    name = _UNSAFE.sub("_", (name or "file").strip()).strip("._") or "file"
    return name[:180]


def _staged_path(staging_id: str) -> Path:
    # staging_id is "<uuid>__<safe filename>"; reject anything that isn't a bare
    # basename. The rules live in app/staging.py, shared with the code that reads
    # ids back out of drafts and must never be handed a path outside the area.
    path = staging.staged_path(staging_id)
    if path is None:
        raise HTTPException(status_code=400, detail="Invalid attachment id")
    return path


class SendRequest(BaseModel):
    account_id: int
    from_address: str | None = None      # a "send as" address owned by the account
    to: list[EmailStr]
    cc: list[EmailStr] = []
    bcc: list[EmailStr] = []
    subject: str = ""
    body_text: str = ""
    # A rendering of body_text, built by the composer when "Send as HTML email"
    # is on. Present, it *is* the message body — see _build_mime for why it
    # cannot be an alternative alongside the text.
    body_html: str = ""
    in_reply_to: str | None = None
    references: list[str] = []
    attachments: list[str] = []          # staging ids from /attachments
    # The message this one forwards as HTML, if it does. body_text (or
    # body_html) is then only the note above it: /send reads the original's
    # markup and pictures back out of the database. See "Forwarding as HTML".
    forward_of: int | None = None
    # The draft this message was written in, if it was autosaved. Sending
    # consumes it in the same transaction that queues the mail (see send).
    draft_id: int | None = None


def _sender_addresses(account: Account) -> list[str]:
    """Every address this account may send as: primary first, then extras."""
    out = [account.email]
    for a in account.send_addresses or []:
        if a.lower() not in {x.lower() for x in out}:
            out.append(a)
    return out


def _resolve_from(account: Account, requested: str | None) -> str:
    """Pick the From address, defaulting to the primary and rejecting any address
    the account does not own."""
    if not requested:
        return account.email
    allowed = {a.lower(): a for a in _sender_addresses(account)}
    chosen = allowed.get(requested.strip().lower())
    if chosen is None:
        raise HTTPException(
            status_code=400,
            detail=f"'{requested}' is not a sender address for {account.email}",
        )
    return chosen


def _from_header(account: Account, from_addr: str) -> str:
    """The From header for that address: `Name <addr>`, or the bare address when
    the agent config gave this one no name.

    Deliberately not the account `label`. That is the account's name in the
    sidebar, it defaults to the local part of the primary address, and it is
    shared by every alias the account owns — so putting it here would sign mail
    from three different addresses with one name the user never chose to send
    under.
    """
    name = (account.send_names or {}).get(from_addr.lower(), "")
    return formataddr((name, from_addr))


@router.post("/attachments")
async def upload_attachment(request: Request):
    """Stage a file for an outgoing message; returns an id to include in /send.

    Takes the `Request` and parses the form itself rather than declaring
    `file: UploadFile = File(...)`, and that is the whole security of this
    route rather than a style preference. FastAPI reads and parses a declared
    body *before* it resolves dependencies — so with the ordinary signature,
    `await request.form()` had already run, and Starlette had already spooled
    the upload to a temporary file, by the time `require_ui_auth` on this router
    got to say 401. A stranger could fill the disk of a password-protected
    install one anonymous POST at a time, and the cap below never even ran.

    Declaring no body field leaves `body_field` unset, so FastAPI skips the
    parse entirely; the dependency runs, and an unauthenticated request is
    turned away having had nothing read. The form below is parsed inside the
    handler, which is to say after the gate. app/limits.py caps the size of what
    reaches it.

    Parsing it here also means owning the errors FastAPI used to turn into
    responses on our behalf: a body that is not the multipart it claims to be is
    a 400 about the request, not a traceback about the server.
    """
    try:
        async with request.form(max_files=1, max_fields=0) as form:
            file = form.get("file")
            if not isinstance(file, UploadFile):
                raise HTTPException(status_code=422,
                                    detail="Expected a file in the 'file' field")
            return await _stage_upload(file)
    except _MALFORMED_MULTIPART as exc:
        raise HTTPException(status_code=400, detail=f"Malformed upload: {exc}") from exc


async def _stage_upload(file: UploadFile) -> dict:
    """Write one uploaded file into the staging area, refusing it past the cap.

    Counted as the bytes are written rather than read off Content-Length, which
    is a number the client chose. app/limits.py has already bounded the request
    as a whole; this bounds the one file inside it, and is what
    `server.max_attachment_bytes` actually means.
    """
    staging_id = f"{uuid.uuid4().hex}__{_safe(file.filename or 'file')}"
    path = settings.outbox_dir / staging_id
    size = 0
    complete = False
    try:
        with path.open("wb") as staged:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_attachment_bytes:
                    raise HTTPException(status_code=413, detail="Attachment too large")
                staged.write(chunk)
        complete = True
    finally:
        if not complete:
            path.unlink(missing_ok=True)
    return {"id": staging_id, "filename": file.filename or "file",
            "content_type": file.content_type or "application/octet-stream", "size": size}


@router.delete("/attachments/{staging_id}", status_code=204)
def delete_attachment(staging_id: str):
    """Discard a staged attachment when the composer no longer references it."""
    _staged_path(staging_id).unlink(missing_ok=True)
    return Response(status_code=204)


def _stage_bytes(filename: str, payload: bytes) -> str:
    """Put bytes we already hold into the outbox staging area, as if uploaded."""
    staging_id = f"{uuid.uuid4().hex}__{_safe(filename or 'file')}"
    (settings.outbox_dir / staging_id).write_bytes(payload)
    return staging_id


# Files staged here and never sent are swept at startup, sparing the ones saved
# drafts still reference: see app/staging.py.


def _forward_attachments(db: DBSession, msg: Message) -> tuple[list[dict], int]:
    """Stage the original's attachments so a forward carries them too.

    Forwarding a message and losing the invoice that was the point of it is the
    kind of quiet failure the recipient discovers, not the sender. So the copies
    are staged exactly like uploads: they arrive as chips in the composer, they
    can be removed one by one, and /send bakes them in through the same path.
    They also get discarded through it — closing the composer deletes them.

    Inline parts are skipped, matching what the reader lists as attachments:
    they are the pictures of the body, and they travel with the body or not at
    all. An HTML forward carries the ones its markup refers to as related parts
    (see "Forwarding as HTML" below); a plain-text one has nowhere to put them,
    since a cid: reference in quoted text means nothing. Pruned messages keep
    their attachment rows with the payload emptied (see store.strip_content);
    those cannot be forwarded, and the second return value is how many were left
    behind so the composer can say so.
    """
    rows = db.execute(
        select(Attachment.filename, Attachment.content_type, Attachment.size_bytes,
               Attachment.content)
        .where(Attachment.message_pk == msg.id, Attachment.is_inline.is_(False))
        .order_by(Attachment.id)
    ).all()
    staged: list[dict] = []
    missing = 0
    for att in rows:
        if not att.content:
            missing += 1
            continue
        staged.append({
            "id": _stage_bytes(att.filename, att.content),
            "filename": att.filename or "file",
            "content_type": att.content_type or "application/octet-stream",
            "size": att.size_bytes or len(att.content),
        })
    return staged, missing


# --- Forwarding as HTML -------------------------------------------------------
#
# The composer is a plain-text editor, so a forward used to be the original's
# words quoted into it. Mail written in HTML lost everything that was not words
# on the way: its layout, and every picture it carried as a `cid:` part, because
# those are not attachments (see _forward_attachments above) and a cid:
# reference in quoted text points at nothing.
#
# So an HTML original is forwarded as HTML. The composer holds only the note the
# user writes above it and names the message it forwards (`forward_of`). /send
# reads that message's markup back out of the database and builds the body from
# three pieces: the note, the forward header, and the original, with each
# picture the original refers to carried as a multipart/related part under the
# Content-ID it already uses. Read at send time rather than handed to the
# browser and back, so a draft stays the size of what was typed.
#
# The original goes out as its sender wrote it, which is what every client does
# when it forwards HTML: the reader's sanitizer decides what this origin renders,
# and a recipient's client makes that decision for itself. Only the <head>
# styles and the <body> content are kept, since one document cannot sit inside
# another one's body, and the body's own style moves onto the <div> holding it.

_FORWARD_RULE = "---------- Forwarded message ----------"
# A cid: reference as it appears in markup, up to the quote, space or bracket
# that ends the attribute value or the url(...) it sits in.
_CID_REF = re.compile(r"""cid:([^"'\s<>)]+)""", re.I)


class _Related(NamedTuple):
    cid: str
    content_type: str
    filename: str
    payload: bytes


class _Forward(NamedTuple):
    html: str                   # the whole body: note, forward header, original
    text: str                   # the same as plain text, for the outbound row
    related: list[_Related]     # the pictures the original refers to by cid:


def _forward_headers(msg: Message) -> list[str]:
    return [f"From: {_format_sender(msg)}", f"Subject: {msg.subject or ''}"]


def _forward_text(msg: Message) -> str:
    """The original as quoted plain text under the forward rule: the whole of a
    forward before HTML ones, and still how a plain-text original goes."""
    body = msg.body_text or html_to_text(msg.body_html, quotes=True)
    return "\n".join([_FORWARD_RULE, *_forward_headers(msg)]) + "\n\n" + body


def _split_document(html: str) -> tuple[str, str, str]:
    """(head styles, body content, body style) of an HTML document or fragment.

    Parsed rather than cut at the tags: a stored body can be several documents
    end to end (see _body_with_forwards in core/mail/parse.py), and a fragment
    with no <body> at all is just as common.
    """
    tree = LexborHTMLParser(html or "")
    styles = "".join(node.html or "" for node in tree.css("head style"))
    body = tree.body
    if body is None:
        return styles, "", ""
    attrs = body.attributes
    style = (attrs.get("style") or "").strip()
    if attrs.get("bgcolor"):
        style = f"background-color:{attrs['bgcolor']};{style}"
    return styles, body.inner_html or "", style


def _embedded_parts(db: DBSession, msg: Message, html: str) -> tuple[list[_Related], int]:
    """The parts `html` refers to by cid:, and how many of those it refers to
    that there are no bytes for (pruned, or never captured).

    By reference rather than by the inline flag: a part the markup never names
    is not a picture in the message, and a part it does name is one whatever
    disposition its sender gave it.
    """
    wanted = {unquote(m.group(1)).strip("<>").lower() for m in _CID_REF.finditer(html or "")}
    if not wanted:
        return [], 0
    rows = db.execute(
        select(Attachment.content_id, Attachment.content_type, Attachment.filename,
               Attachment.content)
        .where(Attachment.message_pk == msg.id, Attachment.content_id.is_not(None))
        .order_by(Attachment.id)
    ).all()
    found: dict[str, _Related] = {}
    for row in rows:
        key = row.content_id.strip("<>").lower()
        if key in wanted and key not in found and row.content:
            found[key] = _Related(row.content_id.strip("<>"), row.content_type or "",
                                  row.filename or "", row.content)
    return list(found.values()), len(wanted - found.keys())


def _forward_body(db: DBSession, req: SendRequest) -> _Forward:
    """The body of a message that forwards `req.forward_of` as HTML.

    Gated like reading the original, because it is reading it. A draft can
    outlive its original (deleted, or its content pruned by the window), and
    that is a 409 naming the way out rather than a forward sent without the
    message it was for: the composer still holds the quoted text.
    """
    try:
        msg = _readable(db, req.forward_of)
    except HTTPException:
        msg = None
    if msg is None or not (msg.body_html or "").strip():
        raise HTTPException(status_code=409, detail=(
            "The message being forwarded is no longer stored, so it cannot go out as HTML. "
            "Use 'Forward as plain text' to send the copy this draft holds instead."))

    styles, content, body_style = _split_document(msg.body_html)
    related, _ = _embedded_parts(db, msg, content)
    if (req.body_html or "").strip():
        note_styles, note, _ = _split_document(req.body_html)
        styles = note_styles + styles
    elif req.body_text.strip():
        note = "<div>" + html_escape(req.body_text.strip()).replace("\n", "<br>\n") + "</div>"
    else:
        note = ""
    header = ('<div style="margin:1.5em 0 0.5em"><b>' + html_escape(_FORWARD_RULE) + "</b><br>"
              + "<br>".join(html_escape(h) for h in _forward_headers(msg)) + "</div>")
    original = (f'<div style="{html_escape(body_style)}">' if body_style else "<div>") + content + "</div>"
    html = ('<!DOCTYPE html>\n<html><head><meta charset="utf-8">' + styles + "</head><body>"
            + note + header + original + "</body></html>")
    text = "\n\n".join(p for p in (req.body_text.strip(), _forward_text(msg)) if p)
    return _Forward(html, text, related)


def _attach_staged(m: EmailMessage, staging_ids: list[str]) -> list[Path]:
    paths: list[Path] = []
    for sid in staging_ids:
        path = _staged_path(sid)
        if not path.exists():
            raise HTTPException(status_code=400, detail=f"Attachment {sid} is no longer staged")
        filename = sid.split("__", 1)[1] if "__" in sid else sid
        ctype, _ = mimetypes.guess_type(filename)
        maintype, subtype = (ctype.split("/", 1) if ctype else ("application", "octet-stream"))
        m.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=filename)
        paths.append(path)
    return paths


def _build_mime(req: SendRequest, from_addr: str, from_header: str | None = None,
                forward: _Forward | None = None) -> tuple[EmailMessage, list[str], list[Path]]:
    m = EmailMessage()
    # The header may carry a display name; `from_addr` never does — it is the
    # envelope sender and the Message-ID domain below.
    m["From"] = from_header or from_addr
    m["To"] = ", ".join(req.to)
    if req.cc:
        m["Cc"] = ", ".join(req.cc)
    m["Subject"] = req.subject
    m["Date"] = formatdate(localtime=True)
    m["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1])
    if req.in_reply_to:
        m["In-Reply-To"] = f"<{req.in_reply_to}>"
    refs = list(req.references)
    if req.in_reply_to and req.in_reply_to not in refs:
        refs.append(req.in_reply_to)
    if refs:
        m["References"] = " ".join(f"<{r}>" for r in refs)
    # Body verbatim: the composer prefills the account footer into the editor,
    # so whatever the user left in there is exactly what goes out.
    #
    # "Send as HTML email" makes the message an HTML one, rather than an HTML
    # alternative to a plain-text one. multipart/alternative is the textbook
    # shape and is what this sent at first — both renderings, plain text first,
    # the reader's client picks whichever it prefers. It does not survive the
    # trip. Proton keeps a single body per message, and handed the pair it
    # keeps the plain text and discards the HTML, so the mail arrives as raw
    # markdown. That was measured, not guessed: the alternative was sent with
    # RFC-correct CRLF endings, in the right order, with no stray headers on
    # either part, and it still landed as text/plain.
    #
    # Nothing about the message was wrong. There was simply a choice available
    # to get wrong, so do not offer one. The button says HTML and the mail is
    # HTML. The cost is the plain-text fallback — a client that cannot render
    # HTML now shows the markup — and that is the trade the button makes, once,
    # per message, when the user presses it. With it off nothing has changed:
    # the message is text/plain and nothing else, exactly as it always was.
    #
    # A forward of HTML mail is HTML whichever way the button is: it is the
    # original's markup that makes it so, not the note. Its pictures go beside
    # it as multipart/related, which is one body with parts it refers to rather
    # than a choice between bodies, so the reasoning above does not reach it.
    if forward is not None:
        m.set_content(forward.html, subtype="html")
        for part in forward.related:
            maintype, _, subtype = part.content_type.lower().partition("/")
            if not maintype or not subtype or maintype in ("multipart", "message"):
                maintype, subtype = "application", "octet-stream"
            # disposition spelled out: given a filename, Python would call the
            # part an attachment, and some clients would list it as one too.
            m.add_related(part.payload, maintype=maintype, subtype=subtype,
                          cid=f"<{part.cid}>", filename=part.filename or None,
                          disposition="inline")
    elif (req.body_html or "").strip():
        m.set_content(req.body_html, subtype="html")
    else:
        m.set_content(req.body_text or "")
    staged_paths = _attach_staged(m, req.attachments)
    # MIME-Version belongs to the message. RFC 2045 defines it at the top level
    # and leaves it undefined on a body part, and no ordinary mail client emits
    # one down there — but Python stamps one on the parts it builds, both for an
    # alternative and for an attachment. A part carrying it can read to a
    # gateway as an encapsulated message rather than as content to display,
    # which is a quiet way to have one dropped in transit.
    for part in m.walk():
        if part is not m:
            del part["MIME-Version"]
    rcpt = [str(a) for a in (req.to + req.cc + req.bcc)]
    return m, rcpt, staged_paths


@router.post("/send")
def send(req: SendRequest, db: DBSession = Depends(get_db)):
    account = db.get(Account, req.account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if not req.to:
        raise HTTPException(status_code=400, detail="At least one recipient is required")

    from_addr = _resolve_from(account, req.from_address)
    forward = _forward_body(db, req) if req.forward_of is not None else None
    m, rcpt, staged_paths = _build_mime(req, from_addr, _from_header(account, from_addr), forward)

    outbound = Outbound(
        account_id=account.id, state="queued",
        to_addrs=[str(a) for a in req.to], cc_addrs=[str(a) for a in req.cc],
        bcc_addrs=[str(a) for a in req.bcc], subject=req.subject,
        # A forward's row holds all of it, so the Outbox shows what is being
        # sent on and not only the note written above it.
        body_text=forward.text if forward else req.body_text or "",
        body_html=forward.html if forward else req.body_html or "",
        in_reply_to=req.in_reply_to, references=req.references,
        attachments=[p.name for p in staged_paths],
        raw_mime=m.as_string(),
    )
    db.add(outbound)
    db.flush()

    # The agent fetches the raw message by id (keeps big attachments out of the queue).
    # mail_from is the chosen sender so Proton relays it as that address.
    #
    # not_before is the configured delay, absent when there is none. It is the
    # difference between a message the agent has not got to yet and one it is
    # deliberately sitting on, and only the second can be called back — which is
    # the point of the setting.
    payload = {"outbound_id": outbound.id, "mail_from": from_addr, "rcpt_to": rcpt}
    hold = outbox_core.hold_until(outbox_core.send_delay(db), utcnow())
    if hold:
        payload["not_before"] = hold
    db.add(PendingAction(account_id=account.id, type="send", payload=payload))

    # The draft this was written in goes in the same commit as the mail it
    # became. Two commits would have a window, however short, in which a crash
    # leaves both: the message queued and its draft still listed, one "Send"
    # away from going out twice. Only a row that is still a draft is touched;
    # an id that has already gone (discarded in another tab, or sent from it)
    # or that names some other outbound row is not this request's to delete,
    # and the send itself is no less valid for it.
    #
    # Its staged files are not unlinked here beyond what the loop below does:
    # the ones this message carries were baked in above, and a chip the user
    # removed before sending was already discarded by the composer.
    consumed_draft = None
    if req.draft_id is not None:
        draft = _locked_draft(db, req.draft_id)
        if draft is not None:
            consumed_draft = draft.id
            db.delete(draft)
    db.commit()

    # Staged files are now baked into raw_mime; drop them.
    for p in staged_paths:
        try:
            p.unlink()
        except OSError:
            pass

    # The outbox count is on screen now, so it has to move when something lands
    # in it — in this window and in any other one that is open.
    events.publish({"type": "outbox", "queued": 1})
    if consumed_draft is not None:
        # Any other tab with this draft listed or open has to hear that it is
        # gone, rather than find out from a 404 on its next autosave.
        events.publish({"type": "drafts", "id": consumed_draft, "revision": None,
                        "change": "deleted"})
    # And ask the agent to drain now rather than at the end of its poll
    # interval: a message that sends in a second should not sit visibly in the
    # outbox for thirty. The pass this asks for sends the mail and then reads
    # the server's copy of it back, which is closer together than a server
    # necessarily likes — see _SEND_SETTLE_SECONDS in the agent's sync.
    #
    # Not for a held message: waking the agent to look at an action it must
    # refuse is a connection made for nothing. It goes out on the first pass
    # after the delay expires instead, so a delayed send lands within one
    # poll_interval of its deadline rather than on it.
    if not hold:
        mailops.wake_agent(db, account.id)

    return {"id": outbound.id, "state": outbound.state, "send_at": hold}


# --- Drafts -----------------------------------------------------------------
#
# The composer autosaves into the outbound table as state "draft", so a message
# that is half written survives a closed tab, a crashed browser and a server
# restart, and can be picked up again in another window. A draft is a snapshot
# of the composer and nothing more: the recipients are whatever tokens have been
# typed so far (a draft is allowed to hold "bob@"), the From is not checked
# against the account, and no MIME exists until /send builds it. All of that is
# checked when the message is sent, which is the moment it has to be right.
#
# Concurrency is optimistic. Every save bumps `revision` and states the revision
# it was based on, and a mismatch is a 409 carrying the current number: the tab
# that lost the race finds out it would be overwriting something it never saw,
# and can reload instead. The row lock is what makes compare-and-bump one step
# when two saves arrive together.
#
# Everything else that reads the table filters on state or goes through a send
# action's outbound_id, which a draft never has, so none of this reaches the
# Outbox, its counts or the agent.


class StagedChip(BaseModel):
    """One attachment chip as the composer shows it; `id` names a staged file."""

    id: str
    filename: str = ""
    size: int = 0
    content_type: str = ""


class DraftIn(BaseModel):
    account_id: int
    # Stored as given. The account's aliases can change while a draft sits, and
    # a draft that could no longer be saved because of that would be worse than
    # a From that /send refuses when the time comes.
    from_address: str | None = None
    # Raw tokens rather than EmailStr: autosave runs mid-keystroke.
    to: list[str] = []
    cc: list[str] = []
    bcc: list[str] = []
    subject: str = ""
    body_text: str = ""
    in_reply_to: str | None = None
    references: list[str] = []
    attachments: list[StagedChip] = []
    state: dict = {}                     # the composer's own UI state, opaque here
    # The revision this save started from. Required by PUT, ignored by POST.
    base_revision: int | None = None


def _check_chips(chips: list[StagedChip]) -> None:
    """400 for a chip whose id could not name a file in the staging area.

    Checked on the way in because a draft's ids are later turned back into
    paths by code that deletes files (DELETE /drafts) and by the sweep that
    decides which ones to spare, so an id that points anywhere else must never
    be stored. Whether the file still exists is a different question, answered
    on every read as `missing_attachments`: a draft is not refused for having
    lost one.
    """
    for chip in chips:
        _staged_path(chip.id)


def _apply_draft(row: Outbound, body: DraftIn) -> None:
    """Overwrite every stored field of a draft with what the composer sent.

    A save is the whole composer, not a patch of it, so nothing from the
    previous revision survives by accident: a field the composer cleared is
    cleared here too.
    """
    row.account_id = body.account_id
    row.to_addrs = list(body.to)
    row.cc_addrs = list(body.cc)
    row.bcc_addrs = list(body.bcc)
    row.subject = body.subject
    row.body_text = body.body_text
    row.body_html = ""
    row.in_reply_to = body.in_reply_to
    row.references = list(body.references)
    row.attachments = [chip.model_dump() for chip in body.attachments]
    row.draft_state = {"from_address": body.from_address, "state": body.state}


def _draft_out(row: Outbound) -> dict:
    """A draft as the composer gets it back: what it saved, plus which of its
    attachments are no longer on disk.

    Those are reported rather than dropped. A chip that silently vanished
    would be an attachment the user believes is still on the message, and the
    composer is the place that can say so before Send does.
    """
    stored = row.draft_state if isinstance(row.draft_state, dict) else {}
    ui_state = stored.get("state")
    chips = list(row.attachments or [])
    return {
        "id": row.id,
        "revision": row.revision,
        "account_id": row.account_id,
        "from_address": stored.get("from_address"),
        "to": list(row.to_addrs or []),
        "cc": list(row.cc_addrs or []),
        "bcc": list(row.bcc_addrs or []),
        "subject": row.subject or "",
        "body_text": row.body_text or "",
        "in_reply_to": row.in_reply_to,
        "references": list(row.references or []),
        "attachments": chips,
        "missing_attachments": [
            sid for sid in staging.chip_ids(chips)
            if (path := staging.staged_path(sid)) is None or not path.exists()
        ],
        "state": ui_state if isinstance(ui_state, dict) else {},
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _locked_draft(db: DBSession, draft_id: int) -> Outbound | None:
    """The draft with this id, locked for the rest of the transaction, or None
    if there is no such row or it is not a draft.

    Filtering on the state is what keeps every caller from ever touching a
    queued or sent message through a draft id. raw_mime is deferred because a
    draft has none, and nothing here should be the query that reads one.
    """
    return db.execute(
        select(Outbound).options(defer(Outbound.raw_mime))
        .where(Outbound.id == draft_id, Outbound.state == "draft")
        .with_for_update()
    ).scalars().first()


@router.get("/drafts")
def list_drafts(db: DBSession = Depends(get_db)) -> dict:
    """Every saved draft, oldest first."""
    rows = db.execute(
        select(Outbound).options(defer(Outbound.raw_mime))
        .where(Outbound.state == "draft")
        .order_by(Outbound.created_at, Outbound.id)
    ).scalars().all()
    return {"drafts": [_draft_out(row) for row in rows]}


@router.post("/drafts")
def create_draft(body: DraftIn, db: DBSession = Depends(get_db)) -> dict:
    """The first save of a composer: a new draft at revision 1."""
    _check_chips(body.attachments)
    if db.get(Account, body.account_id) is None:
        raise HTTPException(status_code=404, detail="Account not found")

    row = Outbound(state="draft", revision=1)
    _apply_draft(row, body)
    db.add(row)
    db.commit()

    out = _draft_out(row)
    events.publish({"type": "drafts", "id": out["id"], "revision": out["revision"],
                    "change": "saved"})
    return out


@router.put("/drafts/{draft_id}")
def save_draft(draft_id: int, body: DraftIn, db: DBSession = Depends(get_db)) -> dict:
    """Every later save: replace the draft, if it is still the revision the
    composer last saw.

    404 when the draft is gone, which means it was sent or discarded somewhere
    else. That is an answer, not an error to retry: recreating it here would
    resurrect a message the user already dealt with.
    """
    if body.base_revision is None:
        raise HTTPException(status_code=422,
                            detail="base_revision is required to save an existing draft")
    _check_chips(body.attachments)

    row = _locked_draft(db, draft_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    if row.revision != body.base_revision:
        raise HTTPException(status_code=409, detail={
            "message": "This draft was saved from somewhere else since it was opened",
            "revision": row.revision,
        })
    if db.get(Account, body.account_id) is None:
        raise HTTPException(status_code=404, detail="Account not found")

    _apply_draft(row, body)
    row.revision = row.revision + 1
    db.commit()

    out = _draft_out(row)
    events.publish({"type": "drafts", "id": out["id"], "revision": out["revision"],
                    "change": "saved"})
    return out


@router.delete("/drafts/{draft_id}", status_code=204)
def delete_draft(draft_id: int, db: DBSession = Depends(get_db)):
    """Discard a draft and the files it was holding on to.

    204 whether or not there was one to discard, since the outcome the caller
    wants (no such draft) is true either way, and two tabs discarding the same
    draft should not have one of them report a failure. A row that is not a
    draft is never deleted through here.

    The files are unlinked after the commit, not before: if the commit fails
    the draft is still there and still needs them. A crash between the two
    leaves files no draft references, and the startup sweep takes those once
    they are old enough.
    """
    row = _locked_draft(db, draft_id)
    if row is None:
        return Response(status_code=204)
    staged = staging.chip_ids(row.attachments)
    db.delete(row)
    db.commit()

    for sid in staged:
        path = staging.staged_path(sid)
        if path is None:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    events.publish({"type": "drafts", "id": draft_id, "revision": None, "change": "deleted"})
    return Response(status_code=204)


# --- Which address do I write to these people from? -------------------------
#
# Someone with a work account and a personal one writes to each set of people
# from a settled one of the two, and picking it by hand on every message is
# something the mailbox already knows the answer to. The composer asks this as
# recipients are typed and follows the answer.
#
# The evidence is your own sent mail: messages whose From is one of your
# accounts' sendable addresses and whose To/Cc/Bcc carries one of the addresses
# being typed. Candidates rank by
#
#   1. how many of the current recipients that address has written to — one
#      that covers the whole list beats one that only knows a single name;
#   2. how often within the last year — a habit since changed must not be
#      outvoted by however many years were spent on the old one;
#   3. how often ever, then how recently.
#
# No history means no answer (``null``), and the composer then leaves the From
# it opened with alone. Drafts are counted along with sent mail: they carry the
# same "I chose this address for this person" decision.
#
# This only answers the question; whether it is worth acting on is the
# composer's call — with one sendable address there is nothing to switch to,
# and it does not ask.

RECENT_DAYS = 365
MAX_LOOKUP_ADDRESSES = 20      # a composer with more recipients than this is not asking a question


def _identities(db: DBSession) -> list[tuple[int, str]]:
    """Every (account_id, address) the user can send as — the candidate set."""
    accounts = db.execute(select(Account).order_by(Account.created_at)).scalars().all()
    return [(a.id, addr.lower()) for a in accounts for addr in _sender_addresses(a)]


@router.get("/sender-for")
def sender_for(address: list[str] = Query(default=[]), db: DBSession = Depends(get_db)):
    """The From address usually used with these recipients, or null if unknown."""
    wanted: list[str] = []
    for raw in address:
        candidate = (raw or "").strip().lower()
        if candidate and "@" in candidate and candidate not in wanted:
            wanted.append(candidate)
        if len(wanted) >= MAX_LOOKUP_ADDRESSES:
            break

    identities = _identities(db)
    if not wanted or not identities:
        return None

    matched = func.count(distinct(Recipient.address)).label("matched")
    recent = func.count().filter(Message.date_sent >= utcnow() - timedelta(days=RECENT_DAYS)).label("recent")
    sent = func.count().label("sent")
    last_sent = func.max(Message.date_sent).label("last_sent")

    row = db.execute(
        select(Message.account_id, Message.from_addr, matched, recent, sent, last_sent)
        .join(Recipient, Recipient.message_pk == Message.id)
        .where(
            Recipient.kind.in_(("to", "cc", "bcc")),
            Recipient.address.in_(wanted),
            tuple_(Message.account_id, Message.from_addr).in_(identities),
        )
        .group_by(Message.account_id, Message.from_addr)
        .order_by(matched.desc(), recent.desc(), sent.desc(), last_sent.desc().nullslast())
        .limit(1)
    ).first()
    if row is None:
        return None
    return {
        "account_id": row.account_id, "address": row.from_addr,
        "matched": row.matched, "sent": row.sent, "last_sent": row.last_sent,
    }


@router.get("/reply-context/{message_id}")
def reply_context(message_id: int, mode: str = "reply", db: DBSession = Depends(get_db)):
    """Prefill for reply / replyall / forward.

    Reached through the same gate as reading the message, because that is what
    it does: a forward is the whole body quoted back out of the database, so mail
    the user has deleted must not be reachable this way either.
    """
    msg = _readable(db, message_id)
    account = db.get(Account, msg.account_id)
    self_addrs = {a.lower() for a in _sender_addresses(account)} if account else set()

    recips = db.execute(
        select(Recipient.kind, Recipient.name, Recipient.address).where(Recipient.message_pk == msg.id)
    ).all()
    orig_to = [a for k, _, a in recips if k == "to"]
    orig_cc = [a for k, _, a in recips if k == "cc"]

    # Default the reply's From to whichever of the account's own addresses the
    # original message was actually addressed to (its alias), else the primary.
    from_address = account.email if account else ""
    if account:
        dest = {a.lower() for a in orig_to + orig_cc}
        from_address = next((a for a in _sender_addresses(account) if a.lower() in dest), account.email)

    base_subj = msg.subject or ""
    quoted = _quote(msg)

    if mode == "forward":
        attachments, missing = _forward_attachments(db, msg)
        ctx = {
            "account_id": msg.account_id, "from_address": from_address, "to": [], "cc": [],
            # Prefixed once: see leading_prefix for why this is not a check on
            # the normalised subject.
            "subject": base_subj if leading_prefix(base_subj) == "forward" else f"Fwd: {base_subj}",
            "body_text": "\n\n" + _forward_text(msg),
            "in_reply_to": None, "references": [],
            "attachments": attachments, "attachments_missing": missing,
        }
        # HTML mail is forwarded as HTML (see "Forwarding as HTML"): the composer
        # opens on an empty note with the original shown under it, and keeps the
        # quoted text for a switch to a plain-text forward.
        if (msg.body_html or "").strip():
            images, images_missing = _embedded_parts(db, msg, msg.body_html)
            ctx["body_text"] = ""
            ctx["forward"] = {"message_id": msg.id, "text": _forward_text(msg),
                              "images": len(images), "images_missing": images_missing}
        return ctx

    to = [msg.from_addr]
    cc: list[str] = []
    if mode == "replyall":
        seen = {*self_addrs, msg.from_addr.lower()}
        for a in orig_to + orig_cc:
            if a.lower() not in seen:
                cc.append(a)
                seen.add(a.lower())
    subject = base_subj if leading_prefix(base_subj) == "reply" else f"Re: {base_subj}"
    references = list(msg.references or [])
    if msg.message_id and msg.message_id not in references:
        references.append(msg.message_id)
    return {
        "account_id": msg.account_id, "from_address": from_address, "to": to, "cc": cc,
        "subject": subject, "body_text": "\n\n" + quoted,
        "in_reply_to": msg.message_id, "references": references,
    }


def _format_sender(msg: Message) -> str:
    """"Name <addr>" when the sender has a display name, else the bare address."""
    if msg.from_name and msg.from_addr:
        return f"{msg.from_name} <{msg.from_addr}>"
    return msg.from_name or msg.from_addr


def _quote(msg: Message) -> str:
    when = msg.date_sent.strftime("%b %d, %Y at %H:%M") if msg.date_sent else ""
    who = _format_sender(msg)
    body = msg.body_text or html_to_text(msg.body_html, quotes=True)
    quoted = "\n".join(("> " + ln).rstrip() for ln in body.splitlines())
    return f"On {when}, {who} wrote:\n{quoted}"
