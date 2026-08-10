"""One client for whichever language model the install is pointed at.

meerail asks a model two questions — "turn this description into a search query"
and "here is a thread, do this with it" — and neither cares who answers. So this
module is the only place that knows a provider's shapes, and everything above it
works in terms of `Config` and `complete()`.

Three providers cover the ground:

  * ``anthropic``  — Claude, at api.anthropic.com (``/v1/messages``).
  * ``openai``     — GPT, at api.openai.com (``/v1/chat/completions``).
  * ``compatible`` — any base URL speaking OpenAI's chat-completions shape, which
    is what Ollama, LM Studio, vLLM, OpenRouter, Groq, Mistral, DeepSeek, Together
    and Gemini's compatibility endpoint all offer. This is the "other AI systems"
    door, and it is a URL rather than a menu of vendors on purpose: a menu would
    have to grow every time one appears, and the thing that actually varies is
    the address.

Raw HTTP rather than each vendor's SDK. Two adapters over one httpx call is
smaller than two SDK dependencies that then have to be kept in step, it keeps the
`compatible` provider a first-class citizen rather than a fork of the OpenAI
client, and it matches how the rest of this app talks to services it does not own
(app/meerato.py, core/mail/tika.py). What it costs is that the request shapes
below are ours to keep current — see the notes on each.

Nothing here reads the database or imports FastAPI: the router above it owns
storage, auth and turning `LLMError` into a response.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from dataclasses import dataclass, field

import httpx

from core.config import get_settings
from . import nethost

# Anthropic pins its wire format with a date rather than a path version. This is
# the current one; it is sent on every call, including the model listing.
ANTHROPIC_VERSION = "2023-06-01"

PROVIDERS = ("anthropic", "openai", "compatible")

PROVIDER_LABELS = {
    "anthropic": "Anthropic (Claude)",
    "openai": "OpenAI",
    "compatible": "Other (OpenAI-compatible)",
}

DEFAULT_BASE = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
    "compatible": "",
}

# Where an OpenAI-compatible endpoint usually lives, offered as examples in the
# settings field. Not a whitelist — anything with a /chat/completions under it
# works, and that is the point of the provider.
COMPATIBLE_EXAMPLES = (
    ("Ollama", "http://localhost:11434/v1"),
    ("LM Studio", "http://localhost:1234/v1"),
    ("OpenRouter", "https://openrouter.ai/api/v1"),
    ("Groq", "https://api.groq.com/openai/v1"),
    ("Mistral", "https://api.mistral.ai/v1"),
    ("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai"),
)

_HINT = "server.llm_allow_private_hosts"

# The image types both wire formats accept. Deliberately short: everything on it
# is a format every vision model has been trained to read, and an attachment in
# something else (a TIFF, an SVG, a HEIC off a phone) is refused by name here
# rather than turned into an opaque 400 from the provider.
IMAGE_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")


class LLMError(Exception):
    """The model could not be asked, or refused to answer."""


class AuthFailed(LLMError):
    """The provider rejected the key."""


class ModelsUnsupported(LLMError):
    """This endpoint has no model listing.

    Not a configuration error: a self-hosted server that serves one model often
    has no ``/models`` route at all, and completions still work. So a config that
    fails only here is worth saving — the model has to be typed in rather than
    picked from a list.
    """


class Refused(LLMError):
    """The provider's own safety layer declined, rather than failing.

    Distinct from an error because nothing is wrong with the request as a
    request: retrying it unchanged produces the same answer, so the person needs
    to be told rather than have it quietly retried.
    """


@dataclass(frozen=True)
class Config:
    """Which model to ask, and how to reach it."""

    provider: str
    model: str
    api_key: str = ""
    base_url: str = ""       # only meaningful for `compatible`

    def __post_init__(self) -> None:
        if self.provider not in PROVIDERS:
            raise LLMError(f"Unknown provider {self.provider!r}")

    @property
    def base(self) -> str:
        """The API root, without a trailing slash."""
        return (self.base_url or DEFAULT_BASE[self.provider]).rstrip("/")

    @property
    def ready(self) -> bool:
        """Enough to make a call with. A `compatible` endpoint may legitimately
        want no key — a local Ollama has nothing to authenticate — so only the
        two hosted providers insist on one."""
        if not self.model or not self.base:
            return False
        return bool(self.api_key) or self.provider == "compatible"


@dataclass
class Reply:
    """What came back, and whether it is all of it."""

    text: str
    model: str = ""
    # The model hit its output ceiling mid-answer. The text is real but stops
    # in the middle, and a caller that parses it (the search writer does) needs
    # to know that before it blames the model for malformed output.
    truncated: bool = False
    usage: dict = field(default_factory=dict)


# --- Talking to it ----------------------------------------------------------


def _timeout() -> httpx.Timeout:
    # A model thinking about a long thread is slow in a way a web service is not:
    # tens of seconds is normal and the read timeout has to allow for it. The
    # connect timeout stays short, because failing to *reach* a provider should
    # be quick to find out about.
    return httpx.Timeout(float(get_settings().llm_timeout_seconds), connect=10.0)


def _target(cfg: Config) -> tuple[str, dict]:
    """The base URL to request and the arguments that keep it there.

    Only `compatible` is checked: the other two are constants in this file and
    cannot be aimed anywhere. A custom endpoint is a URL somebody typed into
    Settings and is then fetched by the *server*, which is the whole of
    app/nethost.py's reason to exist — and a local Ollama is exactly the private
    address it refuses, so the refusal names the setting that allows it.
    """
    if cfg.provider != "compatible":
        return cfg.base, {}
    try:
        return nethost.pinned(cfg.base,
                              allow_private=get_settings().llm_allow_private_hosts,
                              hint=_HINT)
    except nethost.BlockedHost as exc:
        raise LLMError(str(exc)) from exc


def _headers(cfg: Config) -> dict[str, str]:
    if cfg.provider == "anthropic":
        return {"x-api-key": cfg.api_key, "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json"}
    headers = {"content-type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    return headers


def _detail(res: httpx.Response) -> str:
    """The provider's own words for what went wrong.

    Both shapes put the sentence somewhere under `error`; anything else falls
    back to the status line, which at least says whose fault it was.
    """
    try:
        body = res.json()
    except Exception:
        return res.reason_phrase or f"HTTP {res.status_code}"
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err.get("type") or res.reason_phrase)
    if isinstance(err, str):
        return err
    return res.reason_phrase or f"HTTP {res.status_code}"


def _raise_for(res: httpx.Response, cfg: Config) -> None:
    if res.is_success:
        return
    detail = _detail(res)
    if res.status_code in (401, 403):
        raise AuthFailed(f"{PROVIDER_LABELS[cfg.provider]} rejected the API key — {detail}")
    if res.status_code == 404:
        raise LLMError(f"No such model or endpoint: {detail}")
    if res.status_code == 429:
        raise LLMError(f"{PROVIDER_LABELS[cfg.provider]} is rate-limiting this key — {detail}")
    if res.status_code >= 500:
        raise LLMError(f"{PROVIDER_LABELS[cfg.provider]} had a server error — {detail}")
    raise LLMError(detail)


def _request(cfg: Config, method: str, path: str, json: dict | None = None) -> httpx.Response:
    target, pin = _target(cfg)
    try:
        # No redirects: a provider that answers a POST with a 3xx is not one of
        # ours, and following it would carry the API key to wherever it points.
        with httpx.Client(timeout=_timeout(), follow_redirects=False) as client:
            res = client.request(method, f"{target}{path}", json=json,
                                 headers=_headers(cfg), **pin)
    except httpx.TimeoutException as exc:
        raise LLMError(
            f"{PROVIDER_LABELS[cfg.provider]} did not answer within "
            f"{get_settings().llm_timeout_seconds}s") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"Could not reach {PROVIDER_LABELS[cfg.provider]}: {exc}") from exc
    _raise_for(res, cfg)
    return res


# --- Listing the models -----------------------------------------------------


def list_models(cfg: Config) -> list[dict[str, str]]:
    """What this key can actually use, as `[{id, label}]`.

    Asked of the provider rather than kept in a table here, because a table of
    model names is wrong within weeks: models are released and retired on a
    schedule nothing in this repository knows about, and a settings page offering
    a model the account cannot reach is worse than one offering none.
    """
    path = "/v1/models" if cfg.provider == "anthropic" else "/models"
    try:
        res = _request(cfg, "GET", path)
    except LLMError as exc:
        if isinstance(exc, AuthFailed):
            raise
        # A 404 here means this endpoint has no listing, not that the config is
        # wrong — see ModelsUnsupported.
        raise ModelsUnsupported(str(exc)) from exc
    try:
        rows = res.json().get("data") or []
    except Exception as exc:
        raise ModelsUnsupported("That endpoint answered /models with something "
                                "other than a model list") from exc
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("id") or "").strip()
        if not mid:
            continue
        out.append({"id": mid, "label": str(row.get("display_name") or mid)})
    # Newest-looking first is not knowable from an id, so leave the provider's
    # order alone for Anthropic (which returns newest first) and sort the rest,
    # where the order is arbitrary and a sorted list is at least searchable.
    if cfg.provider != "anthropic":
        out.sort(key=lambda m: m["id"])
    return out


# --- Asking it something ----------------------------------------------------


Image = tuple[str, bytes]      # (media_type, bytes)


def check_images(images: Sequence[Image]) -> None:
    """Refuse here what the provider would refuse opaquely.

    A 400 from Anthropic saying "image exceeds maximum size" reaches the dialog
    as a sentence about somebody else's API; the same refusal made here can name
    the file's own limit and the setting that raises it.
    """
    cap = get_settings().llm_max_image_bytes
    for media_type, blob in images:
        if media_type not in IMAGE_TYPES:
            raise LLMError(
                f"{media_type} is not an image type a model can read — "
                f"{', '.join(t.split('/')[1].upper() for t in IMAGE_TYPES)} only.")
        if len(blob) > cap:
            raise LLMError(
                f"That image is {len(blob) / 1e6:.1f} MB, over the "
                f"{cap / 1e6:.1f} MB limit for what may be sent to a model "
                f"(server.llm_max_image_bytes).")


def _b64(blob: bytes) -> str:
    return base64.standard_b64encode(blob).decode("ascii")


def _anthropic_complete(cfg: Config, system: str, user: str, max_tokens: int,
                        images: Sequence[Image]) -> Reply:
    """POST /v1/messages.

    No `temperature` and no `top_p`: the current Claude models reject both with a
    400, and neither has anything to add to "produce this query" or "summarise
    this thread". `max_tokens` is required here (it is not, for OpenAI), and it
    caps thinking and answer together.

    Images lead the turn, which is what Anthropic's own guidance asks for: the
    question reads as being about the picture above it rather than the other way
    round.
    """
    content: list[dict] | str = user
    if images:
        content = [{"type": "image",
                    "source": {"type": "base64", "media_type": mt, "data": _b64(blob)}}
                   for mt, blob in images] + [{"type": "text", "text": user}]
    body = {
        "model": cfg.model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content}],
    }
    data = _request(cfg, "POST", "/v1/messages", body).json()
    # Checked before the content is read: a refusal comes back as a perfectly
    # ordinary 200 with an empty (or half-written) content list, so code that
    # reads content[0] first sees a malformed answer instead of a decision.
    stop = data.get("stop_reason")
    if stop == "refusal":
        raise Refused("Claude declined to answer that one.")
    text = "".join(
        block.get("text", "")
        for block in data.get("content") or []
        if isinstance(block, dict) and block.get("type") == "text"
    )
    return Reply(text=text.strip(), model=data.get("model") or cfg.model,
                 truncated=stop == "max_tokens", usage=data.get("usage") or {})


def _openai_complete(cfg: Config, system: str, user: str, max_tokens: int,
                     images: Sequence[Image]) -> Reply:
    """POST /chat/completions, for OpenAI and everything that imitates it.

    Deliberately the smallest body that can work: model and messages, nothing
    else. Every optional field is one more thing some implementation in the
    `compatible` set rejects — OpenAI's own reasoning models 400 on `max_tokens`
    (they want `max_completion_tokens`) and on a `temperature` that is not the
    default, and several third-party servers do the reverse. `max_tokens` is
    accepted here as an argument and ignored on purpose, so the two providers
    keep one signature.

    An image rides as a `data:` URL in an `image_url` part — the shape OpenAI
    defined and the compatible servers copied. Text first here, unlike
    Anthropic's ordering: this is the arrangement each vendor documents.
    """
    content: list[dict] | str = user
    if images:
        content = [{"type": "text", "text": user}] + [
            {"type": "image_url", "image_url": {"url": f"data:{mt};base64,{_b64(blob)}"}}
            for mt, blob in images]
    body = {
        "model": cfg.model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": content}],
    }
    data = _request(cfg, "POST", "/chat/completions", body).json()
    choices = data.get("choices") or []
    if not choices:
        raise LLMError("The model returned no answer at all.")
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") or {}
    if message.get("refusal"):
        raise Refused(str(message["refusal"]))
    content = message.get("content")
    # Some servers stream-shape a non-stream response, and some reasoning models
    # answer with a list of parts rather than a string.
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return Reply(text=(content or "").strip(), model=data.get("model") or cfg.model,
                 truncated=choice.get("finish_reason") == "length",
                 usage=data.get("usage") or {})


def complete(cfg: Config, *, system: str, user: str, max_tokens: int = 4096,
             images: Sequence[Image] = ()) -> Reply:
    """Ask the configured model one question and hand back what it said.

    `images` is for "explain this attachment" and nothing else so far. Whether
    the configured model can actually see is not knowable from here — there is no
    capability list to consult, and the `compatible` provider could be anything —
    so a text-only model answers a picture with its own error, which reaches the
    dialog as the provider's own words.
    """
    if not cfg.ready:
        raise LLMError("No model is configured — set one up in Settings.")
    check_images(images)
    reply = (_anthropic_complete if cfg.provider == "anthropic" else _openai_complete)(
        cfg, system, user, max_tokens, images)
    if not reply.text:
        raise LLMError("The model answered with nothing.")
    return reply
