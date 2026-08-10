"""The four places meerail asks a language model something, and the settings
behind them.

  * ``POST /search`` turns "the invoice Ada sent me in a PDF" into the query to
    type into the search box. The query comes back to the box rather than being
    run behind the scenes, so the person sees the syntax they were handed and
    picks it up — the search is powerful and the writing of it is the hard part.
  * ``POST /thread`` flattens a whole conversation to text and asks whatever was
    typed into the dialog about it. The answer comes back as text; putting it in
    an email is a second, deliberate step in the browser.
  * ``POST /remind-suggest`` reads the conversation and proposes when it should
    come back. A proposal only: it appears as one more row in the reminder menu,
    and the mail is filed by the same press it always was.
  * ``POST /attachment`` explains one file — as the text Tika already pulled out
    of it, or, for a picture, by handing the image itself to the model.

A shape runs through all four. Each is one call, started by one press, answering
with text the person then decides what to do with; none of them acts on the
mailbox. That is what keeps a wrong answer cheap.

Every call goes out from here rather than from the page, for the same two reasons
the Meerato integration does (app/routers/tasks.py): the key stays on the server,
where an extension reading the DOM or a screenshot cannot reach it, and the
browser is not the thing holding a minute-long request open to a third party.

The provider shapes live in app/llm.py, the words in app/aiprompts.py, and
turning a conversation into text in app/threadtext.py; this module is storage,
auth, and deciding what may be sent.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from core.config import get_settings
from core.database import get_db
from core.models import Attachment, Setting
from .. import aiprompts, llm, searchquery, threadtext
from ..deps import require_ui_auth
from ..security import decrypt_secret, encrypt_secret
from .messages import _readable

router = APIRouter(prefix="/api/ai", tags=["ai"], dependencies=[Depends(require_ui_auth)])

PROVIDER_KEY = "ai_provider"
MODEL_KEY = "ai_model"
BASE_KEY = "ai_base_url"
# One key per provider rather than one key: switching from Claude to GPT to try
# it out and back again should not cost you the key you pasted first.
KEY_PREFIX = "ai_key_"

# How much of an answer to allow. Only Anthropic is told (OpenAI's shape omits it
# — see llm._openai_complete); a query is a line and a thread answer is a few
# paragraphs, so neither number is a constraint in practice.
SEARCH_MAX_TOKENS = 1024
THREAD_MAX_TOKENS = 8192
REMIND_MAX_TOKENS = 1024
ATTACHMENT_MAX_TOKENS = 4096

# How far ahead a suggested reminder may land. A model that answers with 2031 has
# misread the thread — and a conversation parked for five years is one nobody
# will see again, so the suggestion is refused rather than offered.
MAX_REMIND_AHEAD = timedelta(days=366)

# How much of a conversation to send when the question is only "when should this
# come back?". Far less than a summary needs: the answer turns on the dates and
# the last exchange, and this runs from a menu, where a slow answer reads as a
# broken button.
REMIND_THREAD_CHARS = 24_000


# --- What is configured -----------------------------------------------------


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


def _stored_key(db: DBSession, provider: str) -> str:
    """The saved API key for a provider, decrypted.

    Encrypted at rest with the install's secret_key (app/security.py) for the
    same reason the Meerato token is: it is a credential for another service, it
    sits in the same database as the mail, and a backup or a stray
    `select * from settings` should not be a way to spend somebody's API budget.
    A value that will not decrypt was encrypted under a secret_key this server no
    longer has — nothing can recover it, so it reads as "no key".
    """
    stored = _get(db, KEY_PREFIX + provider)
    if not stored:
        return ""
    return decrypt_secret(stored) or ""


def config_for(db: DBSession) -> llm.Config:
    """The configuration as saved, whether or not it is usable."""
    provider = _get(db, PROVIDER_KEY) or "anthropic"
    if provider not in llm.PROVIDERS:
        provider = "anthropic"
    return llm.Config(provider=provider, model=_get(db, MODEL_KEY),
                      api_key=_stored_key(db, provider), base_url=_get(db, BASE_KEY))


def _ready(db: DBSession) -> llm.Config:
    """The configuration, or 409 — the UI hides the robot buttons when nothing is
    set up, so reaching here means it was cleared in another tab."""
    cfg = config_for(db)
    if not cfg.ready:
        raise HTTPException(status_code=409,
                            detail="No AI model is configured — add one in Settings")
    return cfg


def _fail(exc: llm.LLMError) -> HTTPException:
    """The provider's failure as a status code.

    A rejected key is the person's to fix and says so with a 400; a refusal is
    the model declining rather than failing, which is a 409 — nothing to retry.
    Everything else is the far end being unavailable, which is what 502 means.
    """
    if isinstance(exc, llm.AuthFailed):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, llm.Refused):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))


@router.get("/config")
def get_config(db: DBSession = Depends(get_db)) -> dict:
    """What Settings draws, and what the robot buttons switch on.

    No key ever comes back — not masked, not truncated. This module proxies every
    call precisely so the key never reaches the page, and handing it over on the
    way to drawing a settings modal would give that up. `keys` says only which
    providers have one saved, which is all the form needs to show "saved" beside
    a field and to leave it alone when it is submitted untouched.
    """
    cfg = config_for(db)
    return {
        "enabled": cfg.ready,
        "provider": cfg.provider,
        "model": cfg.model,
        "base_url": cfg.base_url,
        "keys": {p: bool(_stored_key(db, p)) for p in llm.PROVIDERS},
        "providers": [
            {"id": p, "label": llm.PROVIDER_LABELS[p], "default_base": llm.DEFAULT_BASE[p],
             "needs_key": p != "compatible", "needs_base": p == "compatible"}
            for p in llm.PROVIDERS
        ],
        "examples": [{"label": name, "url": url} for name, url in llm.COMPATIBLE_EXAMPLES],
        "presets": aiprompts.THREAD_PRESETS,
    }


class ConfigIn(BaseModel):
    provider: str = "anthropic"
    model: str = Field(default="", max_length=200)
    base_url: str = Field(default="", max_length=500)
    # Empty means "keep the one already saved", which is what lets the model be
    # changed without the key being retyped. Clearing is DELETE, below.
    api_key: str = Field(default="", max_length=500)


def _effective(db: DBSession, payload: ConfigIn) -> llm.Config:
    provider = payload.provider
    if provider not in llm.PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider {provider!r}")
    key = payload.api_key.strip() or _stored_key(db, provider)
    base = payload.base_url.strip()
    if provider == "compatible" and not base:
        raise HTTPException(status_code=400,
                            detail="An OpenAI-compatible provider needs a base URL")
    if provider != "compatible":
        base = ""      # a base saved while `compatible` was selected is not this one's
    return llm.Config(provider=provider, model=payload.model.strip(),
                      api_key=key, base_url=base)


@router.put("/config")
def put_config(payload: ConfigIn, db: DBSession = Depends(get_db)) -> dict:
    """Save the provider, key and model.

    Probed before it is stored, the same way the Meerato URL is: a key with a
    typo saved silently comes back later as a robot button that only ever fails,
    with nothing pointing at the field that caused it.
    """
    cfg = _effective(db, payload)
    if cfg.provider != "compatible" and not cfg.api_key:
        raise HTTPException(status_code=400, detail="Paste an API key for this provider")
    if not cfg.model:
        raise HTTPException(status_code=400, detail="Pick a model")

    warning = ""
    try:
        models = llm.list_models(cfg)
    except llm.ModelsUnsupported:
        # Worth saving anyway: a self-hosted server that serves one model often
        # has no listing at all, and completions still work. Say so here rather
        # than letting it surface as an empty dropdown later.
        warning = ("Saved, but this endpoint cannot list its models, so the name above "
                   "was not checked.")
    except llm.LLMError as exc:
        raise _fail(exc) from exc
    else:
        if models and not any(m["id"] == cfg.model for m in models):
            # A warning rather than a refusal: some endpoints (OpenRouter,
            # gateways) list a subset of what they will actually serve, and
            # refusing on an incomplete list would block a working setup.
            warning = (f"Saved, but {cfg.model!r} was not in this provider's model list. "
                       "If it does not answer, pick one from the list.")

    _put(db, PROVIDER_KEY, cfg.provider)
    _put(db, MODEL_KEY, cfg.model)
    _put(db, BASE_KEY, cfg.base_url)
    if payload.api_key.strip():
        _put(db, KEY_PREFIX + cfg.provider, encrypt_secret(payload.api_key.strip()))
    db.commit()
    return {"enabled": True, "provider": cfg.provider, "model": cfg.model,
            "base_url": cfg.base_url, "warning": warning}


@router.delete("/config")
def delete_config(db: DBSession = Depends(get_db)) -> dict:
    """Turn it off and forget every key. The buttons go away with it."""
    for key in (PROVIDER_KEY, MODEL_KEY, BASE_KEY,
                *(KEY_PREFIX + p for p in llm.PROVIDERS)):
        _put(db, key, "")
    db.commit()
    return {"enabled": False}


class ProbeIn(BaseModel):
    provider: str = "anthropic"
    base_url: str = Field(default="", max_length=500)
    api_key: str = Field(default="", max_length=500)


@router.post("/models")
def post_models(payload: ProbeIn, db: DBSession = Depends(get_db)) -> dict:
    """The models this key can use, for the dropdown to be filled from.

    A POST rather than a GET because the key is in the body: the field is filled
    in and the list appears before anything is saved, and a key in a query string
    is a key in an access log. An empty key means "use the saved one", which is
    how the list is refreshed later without retyping it.

    Nothing is written — this is a probe, and a wrong key here should leave the
    working configuration alone.
    """
    if payload.provider not in llm.PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider {payload.provider!r}")
    key = payload.api_key.strip() or _stored_key(db, payload.provider)
    base = payload.base_url.strip() if payload.provider == "compatible" else ""
    cfg = llm.Config(provider=payload.provider, model="probe", api_key=key, base_url=base)
    if not cfg.base:
        raise HTTPException(status_code=400, detail="Enter the endpoint's base URL first")
    try:
        return {"models": llm.list_models(cfg), "listed": True}
    except llm.ModelsUnsupported as exc:
        # Not an error the form should refuse on: type the model name instead.
        return {"models": [], "listed": False, "detail": str(exc)}
    except llm.LLMError as exc:
        raise _fail(exc) from exc


# --- Feature 1: write me a search -------------------------------------------


class SearchIn(BaseModel):
    # Long enough to describe a mail in a paragraph, short enough that the box is
    # not a way to post a novel to a metered API.
    description: str = Field(min_length=1, max_length=2000)


@router.post("/search")
def write_search(payload: SearchIn, db: DBSession = Depends(get_db)) -> dict:
    cfg = _ready(db)
    try:
        reply = llm.complete(cfg, system=aiprompts.SEARCH_SYSTEM,
                             user=aiprompts.search_user(payload.description),
                             max_tokens=SEARCH_MAX_TOKENS)
    except llm.LLMError as exc:
        raise _fail(exc) from exc

    out = aiprompts.parse_search_reply(reply.text)
    if not out["query"]:
        raise HTTPException(
            status_code=502,
            detail="The model did not answer with a query. Try describing the mail "
                   "differently, or a different model.")

    # Checked here rather than left for the search to reject: the query is about
    # to be put in the box and run, and "Search failed — the engine rejected that
    # pattern" a moment later reads as meerail's fault rather than the writer's.
    warning = ""
    parsed = searchquery.parse(out["query"])
    if out["mode"] == "regex" and parsed.text:
        try:
            re.compile(parsed.text)
        except re.error as exc:
            warning = f"The pattern it wrote will not compile ({exc}) — edit it before searching."
    for label, pat in ((":from", parsed.from_pat), (":to", parsed.to_pat)):
        if not pat:
            continue
        try:
            re.compile(pat)
        except re.error as exc:
            warning = f"The pattern in {label} will not compile ({exc}) — edit it before searching."
    if reply.truncated:
        warning = warning or "The model ran out of room mid-answer; the query may be cut short."

    return {"query": out["query"], "mode": out["mode"], "note": out["note"],
            "warning": warning, "model": reply.model}


# --- Feature 2: ask something about a thread --------------------------------


class ThreadIn(BaseModel):
    message_id: int
    # One of aiprompts.THREAD_PRESETS, or empty when `instruction` stands alone.
    preset: str = Field(default="", max_length=32)
    instruction: str = Field(default="", max_length=4000)


@router.post("/thread")
def ask_thread(payload: ThreadIn, db: DBSession = Depends(get_db)) -> dict:
    cfg = _ready(db)
    # Through the same gate as reading it: this sends the conversation's text to
    # a third party, which is the one thing deleted mail must not still do.
    msg = _readable(db, payload.message_id)
    msgs = threadtext.thread_messages(db, msg)

    text, info = threadtext.render(db, msgs, get_settings().llm_max_thread_chars)

    # Preset and free text compose: the buttons are a starting instruction, and
    # anything typed under one is an addition to it rather than a replacement.
    parts = [aiprompts.THREAD_PRESETS[payload.preset]] if payload.preset in aiprompts.THREAD_PRESETS else []
    if payload.instruction.strip():
        parts.append(payload.instruction.strip())
    if not parts:
        parts.append(aiprompts.THREAD_PRESETS["summary"])

    try:
        reply = llm.complete(cfg, system=aiprompts.THREAD_SYSTEM,
                             user=aiprompts.thread_user(text, "\n\n".join(parts)),
                             max_tokens=THREAD_MAX_TOKENS)
    except llm.LLMError as exc:
        raise _fail(exc) from exc

    return {"text": reply.text, "model": reply.model, "truncated": reply.truncated,
            "subject": msg.subject or "(no subject)", "thread": info}


# --- Feature 3: when should this come back? ---------------------------------


class RemindSuggestIn(BaseModel):
    message_id: int
    # The reader's own clock, as local wall-clock ("2026-08-09T14:32"). Sent
    # rather than read here because a reminder is set on *their* calendar: this
    # process may be in another timezone, or in a container running UTC, and
    # "Thursday morning" resolved against the wrong one is a promise kept on the
    # wrong day. Everything about reminders already works this way — see
    # app/static/js/app.reminders.js, which computes every preset locally and
    # sends an absolute instant.
    now: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
    timezone: str = Field(default="", max_length=64)


@router.post("/remind-suggest")
def suggest_reminder(payload: RemindSuggestIn, db: DBSession = Depends(get_db)) -> dict:
    """Read the conversation and propose when it should come back.

    A proposal, not an action: nothing is parked here. The menu shows the moment
    and the sentence explaining it, and setting the reminder is the same press it
    always was — through POST /api/messages/{id}/remind, which is where the mail
    actually moves. The one thing worse than a bad suggestion would be a bad
    suggestion that had already filed the mail away.
    """
    cfg = _ready(db)
    msg = _readable(db, payload.message_id)
    msgs = threadtext.thread_messages(db, msg)
    text, _ = threadtext.render(db, msgs, REMIND_THREAD_CHARS)

    try:
        reply = llm.complete(cfg, system=aiprompts.REMIND_SYSTEM,
                             user=aiprompts.remind_user(text, payload.now, payload.timezone),
                             max_tokens=REMIND_MAX_TOKENS)
    except llm.LLMError as exc:
        raise _fail(exc) from exc

    out = aiprompts.parse_remind_reply(reply.text)
    try:
        when = datetime.strptime(out["when"], "%Y-%m-%dT%H:%M")
        now = datetime.strptime(payload.now, "%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="The model did not answer with a date. Try again, or pick a time "
                   "from the list.") from exc

    # Compared against the clock the caller sent, not this server's: both are
    # local wall-clock in the reader's zone, and the whole point of sending it
    # was that those are the only two that can be compared.
    if when <= now:
        raise HTTPException(status_code=502,
                            detail=f"It suggested {out['when']}, which is already past.")
    if when - now > MAX_REMIND_AHEAD:
        raise HTTPException(status_code=502,
                            detail=f"It suggested {out['when']}, which is further off than "
                                   "a reminder is for.")

    return {"when": out["when"], "reason": out["reason"], "model": reply.model}


# --- Feature 4: what is this attachment? ------------------------------------


class AttachmentIn(BaseModel):
    attachment_id: int
    instruction: str = Field(default="", max_length=2000)


def _attachment_source(att: Attachment) -> tuple[str, list[llm.Image]]:
    """What of this file can be sent, as (text, images).

    Three kinds, in the order they are worth having:

      * Tika's extracted text, which is what a PDF, a Word file or a spreadsheet
        turns into. Already stored — the search index is built from it — so
        asking about a document costs no extraction.
      * The bytes of a plain-text file, decoded here. Nothing extracts those,
        because there is nothing to extract.
      * The image itself, for a screenshot or a photo, handed to the model to
        look at.

    Anything else — a zip, an unreadable binary — raises, because a model asked
    to explain a file it was not given will describe the filename instead, at
    length and convincingly.
    """
    kind = (att.content_type or "").split(";")[0].strip().lower()
    limit = get_settings().llm_max_attachment_chars

    if att.extracted_text and att.extracted_text.strip():
        text = att.extracted_text.strip()
        if len(text) > limit:
            text = text[:limit] + "\n\n[… the rest of this file did not fit and was cut here]"
        return text, []

    if att.content is None:
        raise HTTPException(
            status_code=409,
            detail="This attachment is not stored on the server — only its name and size "
                   "are, because it is outside the content window.")

    if kind in llm.IMAGE_TYPES:
        return "", [(kind, att.content)]

    if kind.startswith("text/") or kind in ("application/json", "application/xml"):
        # errors="replace" rather than a guess at the charset: a mojibake
        # paragraph is still readable enough to summarise, and refusing a file
        # over one bad byte is worse than showing it as it decoded.
        text = att.content.decode("utf-8", errors="replace").strip()
        if not text:
            raise HTTPException(status_code=409, detail="That file is empty.")
        if len(text) > limit:
            text = text[:limit] + "\n\n[… the rest of this file did not fit and was cut here]"
        return text, []

    raise HTTPException(
        status_code=409,
        detail=f"There is nothing readable in a {kind or 'file of this type'} — no text was "
               "extracted from it, and it is not an image a model can look at.")


@router.post("/attachment")
def explain_attachment(payload: AttachmentIn, db: DBSession = Depends(get_db)) -> dict:
    cfg = _ready(db)
    att = db.get(Attachment, payload.attachment_id)
    if att is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    # Through the message it hangs off, and through the same gate as reading
    # that: this sends the file's contents to a third party, which is the one
    # thing an attachment on deleted mail must not still be able to do.
    msg = _readable(db, att.message_pk)

    text, images = _attachment_source(att)
    # Which mail it came with. A model told only "invoice.pdf" describes an
    # invoice; told that Ada sent it under "Re: March hosting", it can say
    # whether this is the one being argued about.
    context = "\n".join([
        f"From: {msg.from_name} <{msg.from_addr}>".strip(),
        f"Subject: {msg.subject or '(no subject)'}",
        f"Date: {msg.date_sent.strftime('%Y-%m-%d')}" if msg.date_sent else "",
    ]).strip()

    try:
        reply = llm.complete(
            cfg, system=aiprompts.ATTACHMENT_SYSTEM,
            user=aiprompts.attachment_user(att.filename or "(unnamed)", att.content_type or "",
                                           att.size_bytes or 0, context, text,
                                           payload.instruction),
            max_tokens=ATTACHMENT_MAX_TOKENS, images=images)
    except llm.LLMError as exc:
        raise _fail(exc) from exc

    return {
        "text": reply.text, "model": reply.model, "truncated": reply.truncated,
        "filename": att.filename or "(unnamed)",
        # What was actually sent, so the dialog can say so rather than leaving
        # "it read the file" to be assumed.
        "source": "image" if images else "text",
        "cut": text.endswith("was cut here]"),
    }
