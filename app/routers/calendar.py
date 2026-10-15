"""Put a calendar event from a mail into meercal.

meercal's address and password live in `settings`, and every call goes out
through this proxy rather than from the browser. meercal ships no CORS
middleware, so the reader could not make the request itself, and proxying keeps
meercal's password out of the page. meercal's own shapes live in `app.meercal`.

What gets imported is an attachment row. The parser stores every calendar part
of a message as one (core/mail/parse.py::is_calendar_part), including the
`text/calendar` an Outlook invitation hides inside multipart/alternative, so
the reader can offer the button off the attachment list it already has, and
this router never has to read a message's raw bytes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from core.database import get_db
from core.mail.parse import is_calendar_part
from core.models import Attachment, Setting
from .. import meercal
from ..deps import require_ui_auth
from ..security import decrypt_secret, encrypt_secret
from .messages import _readable

router = APIRouter(prefix="/api/calendar", tags=["calendar"],
                   dependencies=[Depends(require_ui_auth)])

# Two rows rather than one URL with the secret folded in (which is what Meerato
# hands out). The address is not a secret and is shown back in full, so the
# field can be corrected in place. The password is, and is encrypted.
URL_KEY = "meercal_url"
PASSWORD_KEY = "meercal_password"


def _get(db: DBSession, key: str) -> str:
    row = db.get(Setting, key)
    return row.value if row else ""


def _put(db: DBSession, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if not value:
        if row:
            db.delete(row)
        return
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value


def _stored_password(db: DBSession) -> str:
    """The saved meercal password, decrypted.

    Encrypted at rest with the install's secret_key (app/security.py) for the
    same reason the Meerato token is. It opens somebody's calendar, it sits in
    the same database as the mail, and a backup or a stray
    `select * from settings` should not be a way in. A value that will not
    decrypt was written under a secret_key this server no longer has, and reads
    as no password at all.
    """
    stored = _get(db, PASSWORD_KEY)
    return (decrypt_secret(stored) or "") if stored else ""


def _endpoint(db: DBSession) -> tuple[str, str]:
    """The configured (base, password), or 409. The reader only draws the
    button once meercal is configured, so reaching here unconfigured means the
    setting was cleared in another tab."""
    base = _get(db, URL_KEY)
    if not base:
        raise HTTPException(status_code=409,
                            detail="meercal is not set up. Add its address in Settings.")
    return base, _stored_password(db)


def _upstream(exc: meercal.MeercalError) -> HTTPException:
    """A meercal failure as the status code that says whose problem it is.

    400 when meercal looked at the request and refused it (not a calendar
    file, a read-only calendar), 504 when it never answered, and 502 for the
    rest, which is meercal being unreachable or refusing the password.
    """
    if isinstance(exc, meercal.Refused):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, meercal.NoAnswer):
        return HTTPException(status_code=504, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))


# --- Address and password --------------------------------------------------


class ConfigIn(BaseModel):
    url: str = Field(default="", max_length=2048)
    password: str = Field(default="", max_length=1024)


def _config_reply(db: DBSession) -> dict:
    return {
        "configured": bool(_get(db, URL_KEY)),
        "url": _get(db, URL_KEY),
        # The mask, never the password. This module proxies every meercal call
        # so the password stays on the server, and drawing the settings modal
        # is not a reason to hand it to the page after all.
        "password": meercal.PASSWORD_MASK if _get(db, PASSWORD_KEY) else "",
    }


@router.get("/config")
def get_config(db: DBSession = Depends(get_db)) -> dict:
    return _config_reply(db)


@router.put("/config")
def put_config(payload: ConfigIn, db: DBSession = Depends(get_db)) -> dict:
    """Save (or clear, with an empty address) meercal's address and password.

    The pair is tried against meercal before it is stored. A typo saved
    silently would only surface later, as a button on some invitation that
    fails when pressed, with nothing pointing back at this field.
    """
    raw = (payload.url or "").strip()
    if not raw:
        _put(db, URL_KEY, "")
        _put(db, PASSWORD_KEY, "")
        db.commit()
        return {**_config_reply(db), "calendars": 0, "warning": ""}

    try:
        base = meercal.parse_base(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    password = (payload.password or "").strip()
    if password == meercal.PASSWORD_MASK:
        # The field came back as GET /config drew it. The address may have
        # been edited, the password was never shown and so cannot have been.
        # It is still tried below against the new address, so this is not a
        # way to save a pairing that does not work.
        password = _stored_password(db)
        if not password:
            raise HTTPException(
                status_code=400,
                detail="There is no saved password to keep. Type meercal's password, "
                       "or clear the field if it has none.")

    try:
        calendars = meercal.fetch_calendars(base, password)
    except meercal.BlockedHost as exc:
        # A mistake in the field, not a failure at the far end. This address
        # will never be fetched, so it is refused where it was typed.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except meercal.MeercalError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    _put(db, URL_KEY, base)
    _put(db, PASSWORD_KEY, encrypt_secret(password) if password else "")
    db.commit()
    warning = "" if calendars else (
        "Saved, but meercal has no calendar you can add events to yet. "
        "Read-only calendars and subscriptions are left out.")
    return {**_config_reply(db), "calendars": len(calendars), "warning": warning}


# --- The file, the choice, the import --------------------------------------


def _calendar_file(db: DBSession, attachment_id: int) -> tuple[str, bytes]:
    """The attachment as (filename, bytes), if it is a calendar file the user
    can still see.

    The type is checked from the row's metadata before the content is read, so
    an id that names a 20 MB scan costs a few columns, not the scan.
    """
    meta = db.execute(
        select(Attachment.message_pk, Attachment.filename, Attachment.content_type,
               Attachment.size_bytes, Attachment.content.is_not(None).label("stored"))
        .where(Attachment.id == attachment_id)
    ).first()
    if meta is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    # Through the same gate as reading it. This sends the file to another
    # service, which is one more thing deleted mail must not still be able to do.
    _readable(db, meta.message_pk)
    if not is_calendar_part(meta.content_type, meta.filename):
        raise HTTPException(status_code=400, detail="That attachment is not a calendar file")
    if not meta.stored:
        raise HTTPException(status_code=404,
                            detail="This attachment is not stored any more, because the "
                                   "message is outside the sync window")
    if (meta.size_bytes or 0) > meercal.MAX_ICS_BYTES:
        raise HTTPException(status_code=400,
                            detail=f"That file is larger than "
                                   f"{meercal.MAX_ICS_BYTES // (1024 * 1024)} MB, "
                                   f"which meercal will not import")
    content = db.execute(
        select(Attachment.content).where(Attachment.id == attachment_id)
    ).scalar_one()
    # meercal only uses the name to suggest a calendar name, but an Outlook
    # part that arrived without one should not reach it as "attachment".
    name = meta.filename if (meta.filename or "").lower().endswith(".ics") else "invite.ics"
    return name, content


@router.get("/attachments/{attachment_id}")
def import_options(attachment_id: int, db: DBSession = Depends(get_db)) -> dict:
    """What is in the file and where it could go, for the dialog.

    Both come from meercal and both are fetched fresh each time. A calendar
    added in meercal a minute ago is offered without a reload here, and the
    preview is meercal's reading of the file, which is the reading the import
    will use.
    """
    base, password = _endpoint(db)
    filename, content = _calendar_file(db, attachment_id)
    try:
        # The preview first. A file meercal cannot read is the more useful
        # thing to say, and there is no point listing calendars for it.
        summary = meercal.preview(base, password, filename, content)
        calendars = meercal.fetch_calendars(base, password)
    except meercal.MeercalError as exc:
        raise _upstream(exc) from exc
    return {"filename": filename, "preview": summary, "calendars": calendars}


class ImportIn(BaseModel):
    attachment_id: int
    calendar_id: int = Field(ge=1)


@router.post("/import")
def import_event(payload: ImportIn, db: DBSession = Depends(get_db)) -> dict:
    base, password = _endpoint(db)
    filename, content = _calendar_file(db, payload.attachment_id)
    try:
        result = meercal.import_ics(base, password, payload.calendar_id, filename, content)
    except meercal.MeercalError as exc:
        raise _upstream(exc) from exc
    cal = result.get("calendar") if isinstance(result.get("calendar"), dict) else {}

    def count(key: str) -> int:
        try:
            return int(result.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "calendar": {"id": cal.get("id", payload.calendar_id), "name": str(cal.get("name") or "")},
        "created": count("created"),
        "updated": count("updated"),
        # Non-zero when the calendar has a CalDAV server behind it. meercal has
        # stored the event and its agent pushes it on at the next sync, so the
        # dialog says that rather than letting the count read as finished.
        "queued": count("queued"),
    }
