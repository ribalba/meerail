"""Dictation in the composer, which is speech to text and its settings.

The recognising is done by a Whisper server this install runs itself (the
`whisper` service under the compose profile `dictation`), and every recording
goes to it from here, never from the browser. Partly because the service is on
the compose network, where the browser cannot reach it. Mostly because the
server is where the privacy guard can be enforced (app/dictation.py), so a
recording goes to the address in meerail.toml or nowhere.

The settings are one `settings` row, "dictation", holding a JSON object with
these fields:

  * enabled, off until the user turns it on
  * language, a Whisper language code or auto
  * words, the vocabulary of names and terms the recogniser should spell right

This router owns the row. app/dictation.py owns its shape and validation.
"""

from __future__ import annotations

from typing import Annotated, Any, Callable

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from core.database import get_db
from core.models import Setting
from .. import dictation
from ..deps import require_ui_auth

router = APIRouter(prefix="/api/dictation", tags=["dictation"],
                   dependencies=[Depends(require_ui_auth)])

TURNED_OFF = "Dictation is turned off in Settings."

LanguageCode = Annotated[str, StringConstraints(max_length=dictation.MAX_LANGUAGE_CHARS)]
Word = Annotated[str, StringConstraints(max_length=dictation.MAX_WORD_CHARS)]


def _refuse_null(value: Any, field: str) -> Any:
    # The same contract as AccountUpdate in app/schemas.py and the grammar
    # router. Optional means "omit it to leave it alone", which is not the same
    # as accepting null.
    if value is None:
        raise ValueError(f"{field} cannot be null. Omit it to leave it unchanged, or send a value.")
    return value


def _http_error(exc: dictation.DictationError) -> HTTPException:
    """The status code for each way a transcription can fail. The detail is
    always the module's own sentence, which never contains any of the recording,
    the draft or the transcript."""
    if isinstance(exc, dictation.NotConfigured):
        code = 409
    elif isinstance(exc, (dictation.PublicHost, dictation.Refused)):
        code = 400
    elif isinstance(exc, dictation.Unreachable):
        code = 503
    else:
        code = 502
    return HTTPException(status_code=code, detail=str(exc))


def _load(db: DBSession) -> dict:
    row = db.get(Setting, dictation.SETTING_KEY)
    return dictation.parse_stored(row.value if row else None)


def _shape(config: dict) -> dict:
    return {"available": dictation.available(), **config,
            "languages": list(dictation.WHISPER_LANGUAGES)}


def _save(db: DBSession, change: Callable[[dict], dict]) -> dict:
    """Read the row, apply `change`, store the result, as one read-modify-write.

    The row is read FOR UPDATE, because two of these can overlap. Settings open
    in two tabs, or on two machines, can save at the same moment, and without
    the lock whichever commits second writes back the other's fields as they
    were before.

    The lock cannot cover a row that does not exist yet. Two first-ever writes
    both see nothing and both insert, and the second fails on the primary key.
    It is retried once, and on the retry the row is there to lock.
    """
    try:
        return _write(db, change)
    except IntegrityError:
        db.rollback()
        return _write(db, change)


def _write(db: DBSession, change: Callable[[dict], dict]) -> dict:
    # with_for_update makes get() query even when the row is already in the
    # session, so the lock is always taken. populate_existing makes what it
    # reads overwrite whatever that copy held, so the value is the locked one.
    row = db.get(Setting, dictation.SETTING_KEY, with_for_update=True, populate_existing=True)
    updated = change(dictation.parse_stored(row.value if row else None))
    if row is None:
        db.add(Setting(key=dictation.SETTING_KEY, value=dictation.dump(updated)))
    else:
        row.value = dictation.dump(updated)
    db.commit()
    return updated


# --- Settings -------------------------------------------------------------------


@router.get("/config")
def get_config(db: DBSession = Depends(get_db)) -> dict:
    """The settings, whether this server has a recogniser at all, and the
    languages it can be asked for. `available` false means dictation.url is not
    set, and the composer draws no microphone."""
    return _shape(_load(db))


class ConfigIn(BaseModel):
    """Any subset of the settings. What is left out keeps its stored value."""

    # Strict, so `"enabled": "yes"` or `1` is a 422 rather than a guess.
    model_config = ConfigDict(strict=True)

    enabled: bool | None = None
    language: LanguageCode | None = None
    words: Annotated[list[Word], Field(max_length=dictation.MAX_WORDS)] | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _not_null(cls, value, info):
        # Defaults are not validated in pydantic v2, so this only ever sees a
        # value the caller actually sent.
        return _refuse_null(value, info.field_name)


@router.put("/config")
def put_config(payload: ConfigIn, db: DBSession = Depends(get_db)) -> dict:
    changes = payload.model_dump(exclude_unset=True)

    def change(current: dict) -> dict:
        try:
            return dictation.merge(current, changes)
        except dictation.InvalidSetting as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return _shape(_save(db, change))


# --- Transcribing ---------------------------------------------------------------


@router.post("/transcribe")
def transcribe(
    # The piece of the recording, a WAV file of at most MAX_AUDIO_BYTES.
    audio: UploadFile = File(...),
    # Overrides the stored language for this one piece. Absent means the stored
    # one. Sent empty is not absent (FastAPI passes "" through for a form
    # field), and is refused like any other value that is not a language.
    language: str | None = Form(None),
    # The draft's text just before where the words will go, for the prompt.
    context: str | None = Form(None),
    db: DBSession = Depends(get_db),
) -> dict:
    """Turn one piece of a recording into text.

    Declared with File and Form, unlike /api/compose/attachments, which parses
    its form inside the handler so that nothing is spooled before
    require_ui_auth has run. FastAPI parses a declared form before it resolves
    dependencies, so here an anonymous request is read before it is turned away.
    What bounds that is app/limits.py. This path has no override there, so the
    general server.max_request_bytes applies, the same ceiling every JSON route
    already accepts from anybody, and Starlette's temporary file for the upload
    is closed and removed when the request ends. The attachments route had to
    go further because its own ceiling is server.max_attachment_bytes, which is
    meant to be large.

    A plain def, so it runs in the threadpool. Reading the upload and waiting on
    the service both block, and neither may hold up the event loop.
    """
    # One byte more than the limit is all that has to be read to know a
    # recording is over it.
    data = audio.file.read(dictation.MAX_AUDIO_BYTES + 1)
    if not data:
        raise HTTPException(status_code=422, detail="The recording is empty.")
    if len(data) > dictation.MAX_AUDIO_BYTES:
        # Starlette counts the part as it writes it, so the real size is known
        # even though only the first few megabytes were read here.
        size = audio.size if isinstance(audio.size, int) and audio.size > len(data) else len(data)
        raise HTTPException(
            status_code=422,
            detail=f"That recording is {size} bytes. The limit is {dictation.MAX_AUDIO_BYTES}.")
    # RIFF, a length, then WAVE. Checked here so that a client that sends some
    # other container is told what is expected, rather than the service
    # transcribing whatever ffmpeg made of it.
    if data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise HTTPException(status_code=422, detail="The recording must be a WAV file.")
    try:
        override = dictation.validate_language(language) if language is not None else None
    except dictation.InvalidSetting as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if context is not None and len(context) > dictation.MAX_CONTEXT_IN:
        raise HTTPException(
            status_code=422,
            detail=f"The draft text sent with the recording is {len(context)} characters. "
                   f"The limit is {dictation.MAX_CONTEXT_IN}.")

    if not dictation.available():
        raise HTTPException(status_code=409, detail=dictation.NOT_CONFIGURED)
    config = _load(db)
    if not config["enabled"]:
        raise HTTPException(status_code=409, detail=TURNED_OFF)
    # Hand the connection back before waiting on the service. A piece can take
    # seconds to transcribe, longer when it waits behind another one, a request
    # is made on every pause in speech, and the server's pool is sized on the
    # assumption that a request returns its connection straight away
    # (core/database.py).
    db.rollback()
    try:
        return dictation.transcribe(data, config, override, context)
    except dictation.InvalidSetting as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except dictation.DictationError as exc:
        raise _http_error(exc) from exc
