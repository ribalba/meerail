"""Turn a mail into a Meerato task.

The Meerato private URL lives in `settings`, and every call goes out through
this proxy rather than from the browser: Meerato ships no CORS middleware, so a
cross-origin POST from the reader would never leave the page — and proxying
keeps the token out of the page, where an extension or a stray screenshot could
read it. Meerato's own shapes live in `app.meerato`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DBSession

from core.database import get_db
from core.mail.parse import html_to_text
from core.models import Attachment, Message, Setting
from .. import meerato
from ..deps import require_ui_auth

router = APIRouter(prefix="/api/tasks", tags=["tasks"], dependencies=[Depends(require_ui_auth)])

SETTING_KEY = "meerato_url"


def _stored_url(db: DBSession) -> str:
    row = db.get(Setting, SETTING_KEY)
    return row.value if row else ""


def _endpoint(db: DBSession) -> tuple[str, str]:
    """The configured (base, token), or 409 — the UI hides the buttons when
    nothing is configured, so reaching here means the setting was cleared in
    another tab."""
    raw = _stored_url(db)
    if not raw:
        raise HTTPException(status_code=409, detail="No Meerato URL configured — add one in Settings")
    try:
        return meerato.parse_endpoint(raw)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# --- The private URL -------------------------------------------------------


class ConfigIn(BaseModel):
    url: str = ""


@router.get("/config")
def get_config(db: DBSession = Depends(get_db)) -> dict:
    """The saved URL, token and all.

    It is returned in full so the field can be edited in place instead of
    retyped whole to change the host. Nothing is gained by masking it: this
    endpoint sits behind the same gate as the page that would show the mask,
    and the user pasted the string in the first place.
    """
    url = _stored_url(db)
    return {"configured": bool(url), "url": url}


@router.put("/config")
def put_config(payload: ConfigIn, db: DBSession = Depends(get_db)) -> dict:
    """Save (or clear, with an empty string) the private URL.

    The URL is probed before it is stored. A typo saved silently would surface
    later as an "Add Task" button that fails on click, with nothing pointing
    back at the field that caused it.
    """
    raw = (payload.url or "").strip()
    row = db.get(Setting, SETTING_KEY)

    if not raw:
        if row:
            db.delete(row)
            db.commit()
        return {"configured": False, "url": ""}

    try:
        base, token = meerato.parse_endpoint(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    warning = ""
    try:
        meerato.fetch_options(base, token)
    except meerato.OptionsUnsupported:
        # Worth saving anyway — POST /api/create is all a task actually needs.
        # The cost is that the dialog has no buckets or statuses to offer, so
        # say so here rather than letting it surface as an empty dropdown later.
        warning = ("Saved, but this Meerato has no bucket/status list — tasks will use its "
                   "defaults. Update Meerato to choose them.")
    except meerato.MeeratoError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if row is None:
        db.add(Setting(key=SETTING_KEY, value=raw))
    else:
        row.value = raw
    db.commit()
    return {"configured": True, "url": raw, "warning": warning}


# --- Buckets, statuses, and creating the task ------------------------------


@router.get("/options")
def get_options(db: DBSession = Depends(get_db)) -> dict:
    """Meerato's buckets + statuses, for the dialog's two selects."""
    base, token = _endpoint(db)
    try:
        return meerato.fetch_options(base, token)
    except meerato.MeeratoError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class TaskIn(BaseModel):
    # Absent for a task filed from the composer: the mail it is about is on its
    # way out over SMTP and has no row here to point at, so the caller supplies
    # the text itself. Everything else is the same task.
    message_id: int | None = None
    title: str = Field(default="", max_length=500)
    text: str = ""
    status: str | None = None
    bucket_id: str | None = None
    # ISO YYYY-MM-DD. Meerato parks the task and moves it to "Now" that day.
    schedule_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    # Attachment ids to carry over. Empty means none — the dialog ticks them all
    # by default, but sending a 20 MB scan to a todo is a choice, not a given.
    attachment_ids: list[int] = Field(default_factory=list)


def _task_body(msg: Message) -> str:
    """The mail as task text. Plain text if the sender provided it, else the
    HTML flattened — the same fallback the search corpus uses, so a task made
    from an HTML-only mail reads like the snippet in the list rather than markup.
    """
    if msg.body_text and msg.body_text.strip():
        return msg.body_text
    return html_to_text(msg.body_html)


def _files_for(db: DBSession, message_pk: int, attachment_ids: list[int]
               ) -> tuple[list[tuple[str, bytes, str]], list[str]]:
    """The chosen attachments as upload triples, plus the ones that had no bytes.

    Constrained to the message being filed: the ids come from the client, and
    "which files belong to this mail" is the server's call, not the caller's.
    """
    if not attachment_ids:
        return [], []
    rows = (
        db.query(Attachment)
        .filter(Attachment.id.in_(attachment_ids), Attachment.message_pk == message_pk)
        .all()
    )
    files, missing = [], []
    for att in rows:
        name = att.filename or "attachment"
        if not att.content:
            missing.append(f"{name} (not stored)")
            continue
        files.append((name, att.content, att.content_type or "application/octet-stream"))
    return files, missing


@router.post("")
def create_task(payload: TaskIn, db: DBSession = Depends(get_db)) -> dict:
    base, token = _endpoint(db)

    msg = None
    if payload.message_id is not None:
        msg = db.get(Message, payload.message_id)
        if msg is None:
            raise HTTPException(status_code=404, detail="Message not found")

    # A stored mail is the authority on its own subject, text and files; without
    # one, all three come from the caller — and there are no files to carry over,
    # since the composer's attachments are staged blobs, not rows we can read back.
    title = (payload.title or (msg.subject if msg else "") or "").strip() or "(no subject)"
    text = _task_body(msg) if msg else payload.text
    files, missing = _files_for(db, msg.id, payload.attachment_ids) if msg else ([], [])

    try:
        task = meerato.create_task(base, token, title, text,
                                   bucket_id=payload.bucket_id, status=payload.status,
                                   schedule_date=payload.schedule_date)
    except meerato.MeeratoError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    uploaded, failed = meerato.upload_attachments(base, task, files)
    return {
        "id": task.get("id"),
        "title": task.get("title", title),
        "url": f"{base}/t/{task['public_token']}" if task.get("public_token") else base,
        "uploaded": uploaded,
        "failed": missing + failed,
    }
