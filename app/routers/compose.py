"""Compose + send. The server builds the RFC822 message (including attachments
staged via /attachments); the agent fetches it and relays over SMTP. Reply/forward
prefill (recipients, quoting, threading headers) is computed here."""

from __future__ import annotations

import mimetypes
import os
import re
import uuid
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from ..config import get_settings
from ..database import get_db
from ..deps import require_ui_auth
from ..models import Account, Message, Outbound, PendingAction, Recipient
from ..mail.parse import html_to_text, normalize_subject

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
    to: list[EmailStr]
    cc: list[EmailStr] = []
    bcc: list[EmailStr] = []
    subject: str = ""
    body_text: str = ""
    in_reply_to: str | None = None
    references: list[str] = []
    attachments: list[str] = []          # staging ids from /attachments


@router.post("/attachments")
async def upload_attachment(file: UploadFile = File(...)):
    """Stage a file for an outgoing message; returns an id to include in /send."""
    data = await file.read()
    if len(data) > settings.max_attachment_bytes:
        raise HTTPException(status_code=413, detail="Attachment too large")
    staging_id = f"{uuid.uuid4().hex}__{_safe(file.filename or 'file')}"
    (settings.outbox_dir / staging_id).write_bytes(data)
    return {"id": staging_id, "filename": file.filename or "file",
            "content_type": file.content_type or "application/octet-stream", "size": len(data)}


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


def _build_mime(account: Account, req: SendRequest) -> tuple[EmailMessage, list[str], list[Path]]:
    m = EmailMessage()
    m["From"] = account.email
    m["To"] = ", ".join(req.to)
    if req.cc:
        m["Cc"] = ", ".join(req.cc)
    m["Subject"] = req.subject
    m["Date"] = formatdate(localtime=True)
    m["Message-ID"] = make_msgid(domain=account.email.split("@")[-1])
    if req.in_reply_to:
        m["In-Reply-To"] = f"<{req.in_reply_to}>"
    refs = list(req.references)
    if req.in_reply_to and req.in_reply_to not in refs:
        refs.append(req.in_reply_to)
    if refs:
        m["References"] = " ".join(f"<{r}>" for r in refs)
    m.set_content(req.body_text or "")
    staged_paths = _attach_staged(m, req.attachments)
    rcpt = [str(a) for a in (req.to + req.cc + req.bcc)]
    return m, rcpt, staged_paths


@router.post("/send")
def send(req: SendRequest, db: DBSession = Depends(get_db)):
    account = db.get(Account, req.account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if not req.to:
        raise HTTPException(status_code=400, detail="At least one recipient is required")

    m, rcpt, staged_paths = _build_mime(account, req)

    outbound = Outbound(
        account_id=account.id, state="queued",
        to_addrs=[str(a) for a in req.to], cc_addrs=[str(a) for a in req.cc],
        bcc_addrs=[str(a) for a in req.bcc], subject=req.subject,
        body_text=req.body_text, in_reply_to=req.in_reply_to, references=req.references,
        attachments=[p.name for p in staged_paths],
        raw_mime=m.as_string(),
    )
    db.add(outbound)
    db.flush()

    # The agent fetches the raw message by id (keeps big attachments out of the queue).
    db.add(PendingAction(
        account_id=account.id, type="send",
        payload={"outbound_id": outbound.id, "mail_from": account.email, "rcpt_to": rcpt},
    ))
    db.commit()

    # Staged files are now baked into raw_mime; drop them.
    for p in staged_paths:
        try:
            p.unlink()
        except OSError:
            pass

    return {"id": outbound.id, "state": outbound.state}


@router.get("/reply-context/{message_id}")
def reply_context(message_id: int, mode: str = "reply", db: DBSession = Depends(get_db)):
    """Prefill for reply / replyall / forward."""
    msg = db.get(Message, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    account = db.get(Account, msg.account_id)
    self_addr = account.email.lower() if account else ""

    recips = db.execute(
        select(Recipient.kind, Recipient.name, Recipient.address).where(Recipient.message_pk == msg.id)
    ).all()
    orig_to = [a for k, _, a in recips if k == "to"]
    orig_cc = [a for k, _, a in recips if k == "cc"]

    base_subj = msg.subject or ""
    quoted = _quote(msg)

    if mode == "forward":
        return {
            "account_id": msg.account_id, "to": [], "cc": [],
            "subject": ("" if normalize_subject(base_subj).startswith("fwd") else "Fwd: ") + base_subj,
            "body_text": f"\n\n---------- Forwarded message ----------\nFrom: {msg.from_name or msg.from_addr}"
                         f"\nSubject: {base_subj}\n\n{msg.body_text or html_to_text(msg.body_html)}",
            "in_reply_to": None, "references": [],
        }

    to = [msg.from_addr]
    cc: list[str] = []
    if mode == "replyall":
        seen = {self_addr, msg.from_addr.lower()}
        for a in orig_to + orig_cc:
            if a.lower() not in seen:
                cc.append(a)
                seen.add(a.lower())
    subject = base_subj if normalize_subject(base_subj).startswith("re") else f"Re: {base_subj}"
    references = list(msg.references or [])
    if msg.message_id and msg.message_id not in references:
        references.append(msg.message_id)
    return {
        "account_id": msg.account_id, "to": to, "cc": cc, "subject": subject,
        "body_text": "\n\n" + quoted, "in_reply_to": msg.message_id, "references": references,
    }


def _quote(msg: Message) -> str:
    when = msg.date_sent.strftime("%b %d, %Y at %H:%M") if msg.date_sent else ""
    who = msg.from_name or msg.from_addr
    body = msg.body_text or html_to_text(msg.body_html)
    quoted = "\n".join("> " + ln for ln in body.splitlines())
    return f"On {when}, {who} wrote:\n{quoted}"
