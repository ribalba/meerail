"""Compose + send. The server builds the RFC822 message (including attachments
staged via /attachments); the agent fetches it and relays over SMTP. Reply/forward
prefill (recipients, quoting, threading headers) is computed here."""

from __future__ import annotations

import mimetypes
import os
import re
import time
import uuid
from datetime import timedelta
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy import distinct, func, select, tuple_
from sqlalchemy.orm import Session as DBSession
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
from .. import events, mailops
from ..deps import require_ui_auth
from .messages import _readable
from core.models import Account, Attachment, Message, Outbound, PendingAction, Recipient, utcnow
from core.mail.parse import html_to_text, normalize_subject

router = APIRouter(prefix="/api/compose", tags=["compose"], dependencies=[Depends(require_ui_auth)])
settings = get_settings()
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(name: str) -> str:
    name = _UNSAFE.sub("_", (name or "file").strip()).strip("._") or "file"
    return name[:180]


def _staged_path(staging_id: str) -> Path:
    # staging_id is "<uuid>__<safe filename>"; reject anything that isn't a bare basename.
    if staging_id != os.path.basename(staging_id) or ".." in staging_id:
        raise HTTPException(status_code=400, detail="Invalid attachment id")
    path = (settings.outbox_dir / staging_id).resolve()
    if path.parent != settings.outbox_dir.resolve():
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


# How long a staged file may sit unclaimed before it is swept.
#
# Staging is meant to be brief: a file is written when it is attached and
# removed when the message is sent (/send, which unlinks every path it baked in)
# or when the composer drops it (DELETE /attachments/{id}). Neither happens if
# the composer never finishes — a closed tab, a crashed browser, a laptop lid —
# and nothing else ever looked at the directory, so those files stayed for good.
#
# Forwarding is what makes that matter rather than merely being untidy.
# ``reply_context(mode="forward")`` stages every attachment of the message being
# forwarded *before* the user has decided to send anything, so opening a forward
# of a mail carrying a video and then closing the composer leaves the video on
# disk. Doing that a few times a week is a data directory that grows forever
# with files nothing can reach.
#
# A day, because the only thing the age has to clear is a composer someone left
# open — including one left open overnight. A staged file is written once and
# then only read at /send, so mtime is a fair reading of "nothing has claimed
# this".
STAGING_TTL_SECONDS = 24 * 3600


def sweep_outbox_staging(ttl_seconds: int = STAGING_TTL_SECONDS) -> int:
    """Delete staged attachments nothing came back for. Returns how many.

    Called at startup rather than on a timer. The files are only orphaned by a
    composer that never finished, so a sweep per process start clears them at
    the one moment there is demonstrably no composer open against this server —
    and it costs a directory listing on a directory that is normally empty.

    Best-effort throughout: this runs before the app serves anything, and a
    permission error or a file that vanishes underneath us is not a reason to
    refuse to start. Anything it cannot deal with is simply left, and the next
    start tries again.
    """
    cutoff = time.time() - ttl_seconds
    swept = 0
    try:
        entries = list(settings.outbox_dir.iterdir())
    except OSError:
        return 0
    for path in entries:
        try:
            if not path.is_file() or path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
            swept += 1
        except OSError:
            continue
    return swept


def _forward_attachments(db: DBSession, msg: Message) -> tuple[list[dict], int]:
    """Stage the original's attachments so a forward carries them too.

    Forwarding a message and losing the invoice that was the point of it is the
    kind of quiet failure the recipient discovers, not the sender. So the copies
    are staged exactly like uploads: they arrive as chips in the composer, they
    can be removed one by one, and /send bakes them in through the same path.
    They also get discarded through it — closing the composer deletes them.

    Inline parts are skipped, matching what the reader lists as attachments:
    they are the signature logos and tracking pixels of the body, and the body
    goes into a forward as quoted plain text where a cid: reference means
    nothing. Pruned messages keep their attachment rows with the payload
    emptied (see store.strip_content); those cannot be forwarded, and the second
    return value is how many were left behind so the composer can say so.
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


def _build_mime(req: SendRequest, from_addr: str,
                from_header: str | None = None) -> tuple[EmailMessage, list[str], list[Path]]:
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
    if (req.body_html or "").strip():
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
    m, rcpt, staged_paths = _build_mime(req, from_addr, _from_header(account, from_addr))

    outbound = Outbound(
        account_id=account.id, state="queued",
        to_addrs=[str(a) for a in req.to], cc_addrs=[str(a) for a in req.cc],
        bcc_addrs=[str(a) for a in req.bcc], subject=req.subject,
        body_text=req.body_text or "",
        body_html=req.body_html or "",
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
        return {
            "account_id": msg.account_id, "from_address": from_address, "to": [], "cc": [],
            "subject": ("" if normalize_subject(base_subj).startswith("fwd") else "Fwd: ") + base_subj,
            "body_text": f"\n\n---------- Forwarded message ----------\nFrom: {_format_sender(msg)}"
                         f"\nSubject: {base_subj}\n\n{msg.body_text or html_to_text(msg.body_html)}",
            "in_reply_to": None, "references": [],
            "attachments": attachments, "attachments_missing": missing,
        }

    to = [msg.from_addr]
    cc: list[str] = []
    if mode == "replyall":
        seen = {*self_addrs, msg.from_addr.lower()}
        for a in orig_to + orig_cc:
            if a.lower() not in seen:
                cc.append(a)
                seen.add(a.lower())
    subject = base_subj if normalize_subject(base_subj).startswith("re") else f"Re: {base_subj}"
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
    body = msg.body_text or html_to_text(msg.body_html)
    quoted = "\n".join(("> " + ln).rstrip() for ln in body.splitlines())
    return f"On {when}, {who} wrote:\n{quoted}"
