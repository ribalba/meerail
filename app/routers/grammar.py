"""Grammar and spelling while writing: the composer's checker, and its settings.

The checking is done by a LanguageTool server this install runs itself (the
`languagetool` service under the compose profile `grammar`), and every request
to it goes out from here, never from the browser. Partly because the checker is
on the compose network, where the browser cannot reach it; mostly because the
server is where the privacy guard can be enforced (app/grammar.py): the draft
goes to the address in meerail.toml or nowhere.

The settings are one `settings` row, "grammar", holding a JSON object: on or
off, the language (or auto), preferred variants for auto-detection, the picky
level, the personal dictionary, and the rules switched off. This router owns
the row. app/grammar.py owns its shape and validation.
"""

from __future__ import annotations

from typing import Annotated, Any, Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from core.database import get_db
from core.models import Setting
from .. import grammar
from ..deps import require_ui_auth

router = APIRouter(prefix="/api/grammar", tags=["grammar"], dependencies=[Depends(require_ui_auth)])

TURNED_OFF = "Grammar checking is turned off in Settings."

LanguageCode = Annotated[str, StringConstraints(max_length=grammar.MAX_LANGUAGE_CHARS)]
Word = Annotated[str, StringConstraints(max_length=grammar.MAX_WORD_CHARS)]
RuleId = Annotated[str, StringConstraints(max_length=grammar.MAX_RULE_CHARS)]


def _refuse_null(value: Any, field: str) -> Any:
    # The same contract as AccountUpdate in app/schemas.py: optional means
    # "omit it to leave it alone", which is not the same as accepting null.
    if value is None:
        raise ValueError(f"{field} cannot be null; omit it to leave it unchanged, or send a value")
    return value


def _http_error(exc: grammar.GrammarError) -> HTTPException:
    """The status code for each way a check can fail. The detail is always the
    module's own sentence, which never contains any of the draft."""
    if isinstance(exc, grammar.NotConfigured):
        code = 409
    elif isinstance(exc, (grammar.PublicHost, grammar.Refused)):
        code = 400
    elif isinstance(exc, grammar.Unreachable):
        code = 503
    else:
        code = 502
    return HTTPException(status_code=code, detail=str(exc))


def _load(db: DBSession) -> dict:
    row = db.get(Setting, grammar.SETTING_KEY)
    return grammar.parse_stored(row.value if row else None)


def _shape(config: dict) -> dict:
    return {"available": grammar.available(), **config}


def _save(db: DBSession, change: Callable[[dict], dict]) -> dict:
    """Read the row, apply `change`, store the result: one read-modify-write.

    The row is read FOR UPDATE, because two of these can overlap. The composer's
    "Add to dictionary" and "Turn off this rule" are one click each,
    and an open Settings page may save at the same time; without the lock, two
    clicks that land together each add their word to the list as they read it,
    and whichever commits second writes the first one's word back out.

    The lock cannot cover a row that does not exist yet. Two first-ever writes
    both see nothing and both insert, and the second fails on the primary key;
    it is retried once, and on the retry the row is there to lock.
    """
    try:
        return _write(db, change)
    except IntegrityError:
        db.rollback()
        return _write(db, change)


def _write(db: DBSession, change: Callable[[dict], dict]) -> dict:
    # with_for_update makes get() query even when the row is already in the
    # session, so the lock is always taken; populate_existing makes what it
    # reads overwrite whatever that copy held, so the value is the locked one.
    row = db.get(Setting, grammar.SETTING_KEY, with_for_update=True, populate_existing=True)
    updated = change(grammar.parse_stored(row.value if row else None))
    if row is None:
        db.add(Setting(key=grammar.SETTING_KEY, value=grammar.dump(updated)))
    else:
        row.value = grammar.dump(updated)
    db.commit()
    return updated


# --- Settings -------------------------------------------------------------------


@router.get("/config")
def get_config(db: DBSession = Depends(get_db)) -> dict:
    """The settings, plus whether this server has a checker at all. `available`
    false means grammar.url is not set, and the composer draws no checker."""
    return _shape(_load(db))


class ConfigIn(BaseModel):
    """Any subset of the settings. What is left out keeps its stored value."""

    # Strict: `"enabled": "yes"` or `1` is a 422 rather than a guess.
    model_config = ConfigDict(strict=True)

    enabled: bool | None = None
    language: LanguageCode | None = None
    variants: Annotated[list[LanguageCode], Field(max_length=grammar.MAX_VARIANTS)] | None = None
    picky: bool | None = None
    words: Annotated[list[Word], Field(max_length=grammar.MAX_WORDS)] | None = None
    disabled_rules: Annotated[list[RuleId], Field(max_length=grammar.MAX_RULES)] | None = None

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
            return grammar.merge(current, changes)
        except grammar.InvalidSetting as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return _shape(_save(db, change))


class IgnoreIn(BaseModel):
    """A word for the dictionary (the composer's "Add to dictionary"), a rule id
    to switch off ("Turn off this rule"), or both at once."""

    model_config = ConfigDict(strict=True)

    word: Word | None = None
    rule: RuleId | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _not_null(cls, value, info):
        return _refuse_null(value, info.field_name)

    @model_validator(mode="after")
    def _something(self):
        if self.word is None and self.rule is None:
            raise ValueError("Send a word to add to the dictionary, a rule to switch off, or both")
        return self


@router.post("/ignore")
def ignore(payload: IgnoreIn, db: DBSession = Depends(get_db)) -> dict:
    """Add one word or one rule to what is ignored. Idempotent: a word already
    in the dictionary under any capitalisation, or a rule already off, changes
    nothing and is not an error."""
    try:
        word = grammar.validate_word(payload.word) if payload.word is not None else None
        rule = grammar.validate_rule(payload.rule) if payload.rule is not None else None
    except grammar.InvalidSetting as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    def change(current: dict) -> dict:
        changes: dict = {}
        if word is not None:
            changes["words"] = [*current["words"], word]
        if rule is not None:
            changes["disabled_rules"] = [*current["disabled_rules"], rule]
        try:
            return grammar.merge(current, changes)
        except grammar.InvalidSetting as exc:
            # The entry itself was checked above, so what is left is a list
            # that is full: a refusal of the operation rather than bad input.
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _shape(_save(db, change))


# --- The checker ----------------------------------------------------------------


@router.get("/languages")
def get_languages() -> dict:
    """What the configured checker can check, for the language picker."""
    try:
        return {"languages": grammar.languages()}
    except grammar.GrammarError as exc:
        raise _http_error(exc) from exc


class CheckIn(BaseModel):
    model_config = ConfigDict(strict=True)

    # The draft's prose, one entry per paragraph, with quotes, code and the
    # footer already left out by the browser.
    segments: Annotated[list[str], Field(max_length=grammar.MAX_SEGMENTS)]
    # Overrides the stored language for this one check. Absent or null means
    # the stored one.
    language: LanguageCode | None = None


@router.post("/check")
def check(payload: CheckIn, db: DBSession = Depends(get_db)) -> dict:
    total = sum(len(s) for s in payload.segments)
    if total > grammar.MAX_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"That is {total} characters to check; the limit is {grammar.MAX_CHARS}.")
    if not grammar.available():
        raise HTTPException(status_code=409, detail=grammar.NOT_CONFIGURED)
    config = _load(db)
    if not config["enabled"]:
        raise HTTPException(status_code=409, detail=TURNED_OFF)
    # Hand the connection back before waiting on the checker. A check can take
    # seconds (the first one in a language, twenty at worst), it runs on every
    # pause in typing, and the server's pool is sized on the assumption that a
    # request returns its connection straight away (core/database.py).
    db.rollback()
    try:
        return grammar.check(payload.segments, config, payload.language)
    except grammar.InvalidSetting as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except grammar.GrammarError as exc:
        raise _http_error(exc) from exc
