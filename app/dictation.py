"""Dictation for the composer, done by a speech recogniser on this machine.

The composer records while its microphone button is on, cuts the recording at
natural pauses, and posts each piece here as a short WAV file. This module hands
the piece to a Whisper server (onerahmet/openai-whisper-asr-webservice, run with
the faster-whisper engine) and turns the answer into the text the browser
inserts. Three things shape it more than the feature itself does:

  * **A recording never leaves the machine unless the operator says it may.** It
    is somebody's voice, and with it goes the end of the draft they are writing
    (see build_prompt), which has not been sent to anybody yet. The recogniser is
    a container on the compose network, and before any request goes out the
    configured host is resolved and refused if any address it has is public (see
    ``_guard``). ``dictation.allow_public_hosts`` is the only way past that.
    Nothing here logs the audio, the draft, the prompt or the transcript, or puts
    any of them into an error message, and nothing is sent at all on an install
    that has not set ``dictation.url``.
  * **Each piece is transcribed on its own, so it is told what came before.**
    Whisper reads its prompt as the text that preceded the audio. The tail of the
    draft goes there, and so does the user's vocabulary of names and terms, which
    is what keeps casing, punctuation and spelling the same from one piece of a
    sentence to the next.
  * **Whisper invents text on silence.** A piece cut at a pause has silence in
    it, and Whisper fills silence with things it saw often in training, subtitle
    credits above all. The service's voice activity filter removes most silence
    before the model runs, and keep_segment drops what still gets through.

Kept free of FastAPI (like app/grammar.py), so the whole of it can be driven by
the unit tests through an httpx.MockTransport (see ``use_transport``). The
router, app/routers/dictation.py, reads the upload, does the database, and turns
the exceptions below into status codes.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import threading
import time
import unicodedata
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from core.config import get_settings
from . import nethost

log = logging.getLogger(__name__)

# The `settings` row this feature owns. Its value is the JSON object that
# parse_stored() reads and validate() checks.
SETTING_KEY = "dictation"

# Whisper's own language codes, in the order openai/whisper lists them in
# whisper/tokenizer.py (LANGUAGES), which is roughly how much of each language
# the model was trained on. The service checks `language` against the same
# table and answers 422 to anything else, so this is both what the Settings
# picker offers and the only codes a request may carry. Compared with upstream
# on 2026-10-29. That is 100 codes, the last of them (yue) added with large-v3.
WHISPER_LANGUAGES: tuple[str, ...] = (
    "en", "zh", "de", "es", "ru", "ko", "fr", "ja", "pt", "tr",
    "pl", "ca", "nl", "ar", "sv", "it", "id", "hi", "fi", "vi",
    "he", "uk", "el", "ms", "cs", "ro", "da", "hu", "ta", "no",
    "th", "ur", "hr", "bg", "lt", "la", "mi", "ml", "cy", "sk",
    "te", "fa", "lv", "bn", "sr", "az", "sl", "kn", "et", "mk",
    "br", "eu", "is", "hy", "ne", "mn", "bs", "kk", "sq", "sw",
    "gl", "mr", "pa", "si", "km", "sn", "yo", "so", "af", "oc",
    "ka", "be", "tg", "sd", "gu", "am", "yi", "lo", "uz", "fo",
    "ht", "ps", "tk", "nn", "mt", "sa", "lb", "my", "bo", "tl",
    "mg", "as", "tt", "haw", "ln", "ha", "ba", "jw", "su", "yue",
)
_KNOWN_LANGUAGES = frozenset(WHISPER_LANGUAGES)

# What one recording may be. The browser sends 16 kHz mono 16-bit WAV in pieces
# of at most 30 seconds, which is 960 kB. Four MiB leaves room for a client that
# records at 44.1 or 48 kHz and does not resample, and it stays well inside
# server.max_request_bytes (8 MiB by default), which the whole request, form
# fields included, has to fit into before this limit is even looked at.
MAX_AUDIO_BYTES = 4 * 1024 * 1024

# How much draft text the browser may send along with a recording. Only the last
# CONTEXT_CHARS of it are used. This bound is there so that a client cannot send
# a whole long draft with every pause in speech.
MAX_CONTEXT_IN = 4000

# How much of the draft goes into the prompt. A few sentences are enough for
# Whisper to pick up the language, the casing and the punctuation style, and the
# prompt has a hard ceiling (see build_prompt) that the vocabulary shares.
CONTEXT_CHARS = 300

# Limits on the vocabulary. Unlike grammar's dictionary it is not applied to the
# answer but sent with every recording, inside the prompt, and the prompt has
# room for only so much (see build_prompt). A hundred names and terms, and four
# hundred characters in all, is what fits next to the draft's tail.
MAX_WORDS = 100
MAX_WORD_CHARS = 60
MAX_VOCABULARY_CHARS = 400

# The longest language value the router lets through to validate_language. Every
# real one is two or three letters. This only keeps a client from sending a
# megabyte of something that is then refused anyway.
MAX_LANGUAGE_CHARS = 16

# How much of the service's own error message is passed on. FastAPI answers an
# unknown language with the full list of codes it would have taken, which is
# several hundred characters after the part that matters.
MAX_DETAIL_CHARS = 300

# Failing to reach the service should be quick to find out about. It is on the
# same host or the same compose network, so three seconds is already generous.
# The read side is dictation.timeout_seconds, which has to allow for a piece
# waiting behind another one (the service transcribes one at a time) and for a
# model the service unloaded while idle being loaded again. A first start is a
# different matter. The service downloads and loads its model before it listens
# at all, so until then it refuses connections, and that is reported as
# UNREACHABLE rather than waited out.
CONNECT_TIMEOUT = 3.0

# How long a privacy verdict on the configured URL is trusted. A recording is
# posted on every pause in speech, and a DNS lookup for each would be a cost with
# no benefit, because the URL is operator configuration and does not change
# under a running server. Five minutes still notices a name that is re-pointed.
GUARD_TTL = 300.0

# How long "that name does not resolve" is remembered. Much shorter, because it
# is not a verdict but a state. The whisper container is stopped, restarting or
# not started yet, and it should be found soon after it is back. It is
# remembered at all because the lookup that fails is slow (about four seconds
# for a missing service name on a compose network), and without this every
# pause in speech would spend those four seconds finding out the same thing.
UNRESOLVED_TTL = 10.0

# Whisper's own rule for a window that is silence rather than speech, as
# openai/whisper's transcribe() applies it with its default thresholds. The
# model's "no speech" token is likely, and the text it produced anyway is
# unlikely. Either half alone is not enough. A quiet speaker can score a high
# no-speech probability on real words, which then come with a decent log
# probability, and an unusual name scores a low log probability while plainly
# being speech. faster-whisper applies the same rule while it decodes, so for
# this service it is a second check. It is kept because the service can be
# switched to another engine, and a segment that slips through is a sentence of
# nonsense in somebody's mail.
NO_SPEECH_PROB = 0.6
LOGPROB_FLOOR = -1.0

# Also Whisper's own threshold. Text that gzip compresses better than 2.4 to 1
# is the same few words over and over, which is what the decoder produces when
# it falls into a loop, and nobody dictates that.
COMPRESSION_CEILING = 2.4

NOT_CONFIGURED = ("Dictation is not set up on this server. "
                  "Set [dictation] url in meerail.toml.")
BAD_URL = ("dictation.url in meerail.toml is not an http:// or https:// address with a "
           "host name in it, so there is nothing to send a recording to.")
UNREACHABLE = ("The dictation service is not answering. Is the whisper container running? "
               "On its first start it downloads the speech model, which can take several "
               "minutes.")
BAD_ANSWER = "The dictation service sent an answer meerail could not read."
PUBLIC_REFUSAL = ("dictation.url points at a public address, so recordings of your voice would "
                  "leave this machine. Point it at a service on your own network, or set "
                  "dictation.allow_public_hosts = true if that is intended.")


# --- Errors -------------------------------------------------------------------


class DictationError(Exception):
    """The recording could not be transcribed. Each subclass carries a sentence
    meant for the person speaking, and none of them ever contains any of the
    recording, the draft or the transcript."""


class NotConfigured(DictationError):
    """dictation.url is empty, or is not a URL a request could be sent to."""


class PublicHost(DictationError):
    """The privacy guard. dictation.url resolves to a public address and the
    install has not said that is allowed. Nothing was sent."""


class Unreachable(DictationError):
    """The service could not be reached, or did not answer in time. Also a host
    that does not resolve. In a compose stack that is what a service which is
    not running looks like, and it is not a verdict about privacy."""


class Refused(DictationError):
    """The service answered 4xx. The message is the service's own, trimmed."""


class BadAnswer(DictationError):
    """The service answered with a server error, or with something that is not
    the JSON shape it documents."""


class InvalidSetting(ValueError):
    """A settings value that cannot be stored, with a sentence saying why."""


# --- The settings row -----------------------------------------------------------

# Off until the user turns it on. Unlike grammar checking, which only reads what
# was typed, this records a microphone, and that is a decision for the person
# whose microphone it is, not a default.
DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "language": "auto",
    "words": [],
}

# Unicode categories a vocabulary word may not contain. They are control
# characters (newlines and tabs among them), lone surrogates (which Postgres
# cannot store), and the line and paragraph separators. The same set grammar's
# dictionary refuses.
_FORBIDDEN_IN_WORDS = frozenset({"Cc", "Cs", "Zl", "Zp"})


def defaults() -> dict[str, Any]:
    """A fresh copy of the defaults, safe to mutate."""
    return {key: list(value) if isinstance(value, list) else value
            for key, value in DEFAULTS.items()}


def _shown(value: Any) -> str:
    """A value quoted for an error message, cut short. The message is shown in a
    settings field, and echoing a megabyte back would help nobody."""
    text = repr(value)
    return text if len(text) <= 50 else text[:47] + "..."


def _flag(name: str) -> Callable[[Any], bool]:
    def check(value: Any) -> bool:
        # isinstance(True, int) holds, but not the other way round. So 1 is not
        # accepted as a boolean, and a client that means something else by it
        # is told rather than guessed at.
        if not isinstance(value, bool):
            raise InvalidSetting(f"{name} must be true or false.")
        return value
    return check


def validate_language(value: Any) -> str:
    """One of Whisper's language codes, or "auto".

    Matched exactly after stripping, lower case and all. Whisper's table has no
    regional variants and no upper-case spellings, and the service refuses
    anything that is not literally in it, so there is nothing to normalise
    towards that would not be a guess.
    """
    if not isinstance(value, str):
        raise InvalidSetting("The language must be a code such as en or de, or auto.")
    code = value.strip()
    if code == "auto" or code in _KNOWN_LANGUAGES:
        return code
    raise InvalidSetting(f"{_shown(value)} is not a language Whisper knows. "
                         f"Use a code such as en or de, or auto.")


def validate_word(value: Any) -> str:
    """One vocabulary entry, stripped."""
    if not isinstance(value, str):
        raise InvalidSetting("A vocabulary word must be text.")
    word = value.strip()
    if not word or len(word) > MAX_WORD_CHARS:
        raise InvalidSetting(f"A vocabulary word must be between 1 and {MAX_WORD_CHARS} "
                             f"characters long.")
    if any(unicodedata.category(ch) in _FORBIDDEN_IN_WORDS for ch in word):
        raise InvalidSetting("A vocabulary word cannot contain line breaks or control "
                             "characters.")
    return word


def _each(value: Any, check: Callable[[Any], str], what: str, lenient: bool) -> list[str]:
    """Every entry of a list through `check`. Lenient (reading a stored row)
    skips an entry that no longer passes instead of refusing the list. Rules can
    tighten between versions, and one entry that stopped validating must not
    cost somebody the rest of their vocabulary."""
    if not isinstance(value, list):
        if lenient:
            return []
        raise InvalidSetting(f"{what} must be a list.")
    out = []
    for item in value:
        try:
            out.append(check(item))
        except InvalidSetting:
            if not lenient:
                raise
    return out


def validate_words(value: Any, *, lenient: bool = False) -> list[str]:
    """The vocabulary, deduplicated ignoring case and keeping the first spelling
    seen, in the order given.

    Not sorted, unlike grammar's dictionary. The order is the order the words go
    into the prompt, and the prompt is cut from the front when it is too long
    (see build_prompt), so where a word stands is the user's to decide.
    Deduplicated ignoring case because two spellings of one name would tell the
    recogniser two different things, and the first one entered is the one meant.

    Lenient keeps the longest run from the start that fits both limits, so a
    stored row from a version with looser limits loses its last words rather
    than all of them.
    """
    seen: dict[str, str] = {}
    for word in _each(value, validate_word, "words", lenient):
        seen.setdefault(word.casefold(), word)
    out = list(seen.values())
    if len(out) > MAX_WORDS:
        if not lenient:
            raise InvalidSetting(f"The vocabulary holds at most {MAX_WORDS} words.")
        out = out[:MAX_WORDS]
    if len(", ".join(out)) > MAX_VOCABULARY_CHARS:
        if not lenient:
            raise InvalidSetting(
                f"The vocabulary can be at most {MAX_VOCABULARY_CHARS} characters long in "
                f"all, counting a comma and a space between words. Remove some words to "
                f"make room.")
        while out and len(", ".join(out)) > MAX_VOCABULARY_CHARS:
            out.pop()
    return out


_VALIDATORS: dict[str, Callable[..., Any]] = {
    "enabled": _flag("enabled"),
    "language": validate_language,
    "words": validate_words,
}
_LISTS = ("words",)


def validate(config: dict[str, Any]) -> dict[str, Any]:
    """A complete settings value, checked and normalised. Missing keys take the
    default and unknown keys are dropped. Raises InvalidSetting."""
    base = defaults()
    return {key: check(config.get(key, base[key])) for key, check in _VALIDATORS.items()}


def merge(current: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """`current` with the keys in `changes` replaced, validated as a whole."""
    return validate({**current, **{k: v for k, v in changes.items() if k in DEFAULTS}})


def parse_stored(raw: str | None) -> dict[str, Any]:
    """The stored row as a settings value, never raising.

    A row that is absent, is not JSON or is not an object reads as the
    defaults. Inside a readable row each field falls back on its own, and the
    vocabulary keeps whichever entries still pass. The row is only ever written
    by this module, so anything odd in it is a row from another version, and the
    right response to that is to keep as much of it as can be kept.
    """
    out = defaults()
    if not raw:
        return out
    try:
        data = json.loads(raw)
    except ValueError:
        return out
    if not isinstance(data, dict):
        return out
    for key, check in _VALIDATORS.items():
        if key not in data:
            continue
        try:
            out[key] = check(data[key], lenient=True) if key in _LISTS else check(data[key])
        except InvalidSetting:
            pass
    return out


def dump(config: dict[str, Any]) -> str:
    """The row's text. Only ever called with a value validate() returned."""
    return json.dumps(config, ensure_ascii=False, separators=(",", ":"))


# --- The prompt -----------------------------------------------------------------


def build_prompt(words: list[str], context: str | None) -> str:
    """The `initial_prompt` for one piece of a recording, or "" for none.

    Whisper treats the prompt as the text that came right before the audio, and
    continues from it. Given the tail of the draft, a piece cut in the middle of
    a sentence starts in lower case and without a capital after a comma, a name
    spelled one way in the last piece is spelled the same way in this one, and a
    German draft stays German. That is the point of sending the draft at all.

    The vocabulary goes first and the draft's tail last, so the prompt ends where
    the dictation continues. faster-whisper keeps only the last 223 prompt tokens
    (half its 448-token context, less one), and what overflows is cut from the
    front. The caps on both (MAX_VOCABULARY_CHARS and CONTEXT_CHARS, about 700
    characters together) are sized to keep both inside that window for ordinary
    prose. Should a vocabulary of long rare names push past it, what is lost is
    the start of the vocabulary and never the sentence being continued.
    """
    parts = []
    if words:
        parts.append(", ".join(words) + ".")
    tail = " ".join((context or "").split())
    if len(tail) > CONTEXT_CHARS:
        # Cut at a word boundary. A word sliced in half at the front ("ing the
        # contract") would read to Whisper as a word that does not exist. Only
        # the first word is in question, and only when the cut fell inside it.
        whole = tail[-CONTEXT_CHARS - 1] == " "
        tail = tail[-CONTEXT_CHARS:]
        if not whole:
            space = tail.find(" ")
            if space >= 0:
                tail = tail[space + 1:]
    if tail:
        parts.append(tail)
    return " ".join(parts)


# --- Segments -------------------------------------------------------------------

# Letters and digits, in any script. The normal form below keeps runs of these
# and nothing else, so punctuation, case and spacing do not decide whether a
# segment is a known invention.
_WORD_RUN = re.compile(r"[^\W_]+")

# Whisper was trained on a great deal of subtitled video from the web, and the
# subtitle files ended with the credits of whoever made them, the broadcaster
# who paid for them, or the presenter's sign-off. So on silence, or on noise
# that is not speech, the model sometimes produces one of those lines with full
# confidence. The voice activity filter normally removes the silence before the
# model ever sees it, and Whisper's no-speech rule catches most of what is left,
# but not always, and a line like this pasted into somebody's mail is worse than
# a missed word.
#
# Only credits are listed, lines nobody writes into an email. Phrases a person
# could really dictate in a mail ("Vielen Dank", "Bis zum nächsten Mal",
# "Thank you") are deliberately left out even though Whisper invents them too.
# Dropping a sentence the user said is worse than inserting one they can see
# and delete, and the user would have no way to tell why their words vanished.
_HALLUCINATIONS = frozenset({
    "thank you for watching",
    "thanks for watching",
    "please subscribe",
    "vielen dank fürs zuschauen",
    "danke fürs zuschauen",
    "untertitel im auftrag des zdf",
    "untertitel im auftrag des zdf für funk",
    "untertitel der amara org community",
    "untertitel von stephanie geiges",
    "subtitles by the amara org community",
})

# The same credits with a year, a channel or a different broadcaster in them
# ("Untertitelung des ZDF, 2020", "Copyright WDR 2021"), matched against the
# normal form, so with no punctuation and in lower case.
_CREDIT_PATTERNS = (
    re.compile(r"^(untertitel|untertitelung|subtitles?)\b.*\b(zdf|ard|wdr|swr|ndr|amara|funk)\b"),
    re.compile(r"^copyright (wdr|zdf|ard|swr|ndr)\b"),
)


def _normal(text: str) -> str:
    return " ".join(_WORD_RUN.findall(text.casefold()))


def is_hallucination(text: str) -> bool:
    """Whether a segment is one of the credits Whisper is known to invent."""
    normal = _normal(text)
    return normal in _HALLUCINATIONS or any(p.search(normal) for p in _CREDIT_PATTERNS)


def _number(value: Any) -> float | None:
    # A bool is an int to isinstance, and true is not a probability.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def keep_segment(segment: Any) -> bool:
    """Whether a segment of the service's answer is speech worth inserting.

    A score that is missing, or is not a number, never drops a segment. The
    scores are there to catch the model inventing text, and a service build
    that leaves one out must cost the filter its precision, not the user their
    words.
    """
    if not isinstance(segment, dict):
        return False
    text = segment.get("text")
    if not isinstance(text, str) or not text.strip():
        return False
    no_speech = _number(segment.get("no_speech_prob"))
    logprob = _number(segment.get("avg_logprob"))
    if (no_speech is not None and logprob is not None
            and no_speech > NO_SPEECH_PROB and logprob < LOGPROB_FLOOR):
        return False
    ratio = _number(segment.get("compression_ratio"))
    if ratio is not None and ratio > COMPRESSION_CEILING:
        return False
    return not is_hallucination(text)


# --- Talking to the service -----------------------------------------------------

# One client for the life of the process. httpx.Client is thread-safe, and the
# route calling this runs in FastAPI's threadpool. Keeping it means the
# connection to the service stays open between pieces, which matters for a
# request made on every pause in speech. Per-request timeouts are passed on each
# call, so the client itself holds no setting.
_client: httpx.Client | None = None
_transport: httpx.BaseTransport | None = None
_client_lock = threading.Lock()

# url -> (expires at, "public" | "private" | "unresolved"). See _guard.
_guard_cache: dict[str, tuple[float, str]] = {}

# Set for the length of one request from _request, in the thread making it. See
# _HideQuery.
_in_request = threading.local()


class _HideQuery(logging.Filter):
    """Cut the query string off httpx's own log line for this module's requests.

    httpx logs every request at INFO with its whole URL, in the form
    'HTTP Request: POST http://whisper:9000/asr?...&initial_prompt=... "HTTP/1.1
    200 OK"'. The prompt has to travel in the query string, because that is the
    only place the service reads it from (see transcribe), so on any install
    that turns INFO on for httpx that line would put the end of the user's draft
    into the server's log. The line is kept, without the query. Only this
    module's requests are touched. _request marks its own thread while it runs,
    and httpx writes the line from the thread that made the request.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(_in_request, "active", False) and isinstance(record.args, tuple):
            record.args = tuple(arg.copy_with(query=None) if isinstance(arg, httpx.URL) else arg
                                for arg in record.args)
        return True


logging.getLogger("httpx").addFilter(_HideQuery())


def use_transport(transport: httpx.BaseTransport | None) -> None:
    """Send every request through `transport` from now on (None means the
    network), and forget everything cached. The seam the unit tests use to stand
    an httpx.MockTransport in for the service."""
    global _client, _transport
    with _client_lock:
        old, _client, _transport = _client, None, transport
    if old is not None:
        old.close()
    _guard_cache.clear()


def _http() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None:
            # No redirects. A recogniser that answers with a 3xx is not a
            # recogniser, and following it would carry the recording to wherever
            # it points, past the guard that only looked at the configured host.
            #
            # And no proxy from the environment (trust_env=False). httpx would
            # otherwise honour HTTP_PROXY and HTTPS_PROXY, and on a machine that
            # sets them for its outbound traffic every recording would travel
            # through that proxy. That is exactly the leaving-the-machine the
            # guard is there to prevent, and one it cannot see.
            _client = httpx.Client(transport=_transport, follow_redirects=False,
                                   trust_env=False)
        return _client


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(float(get_settings().dictation_timeout_seconds),
                         connect=CONNECT_TIMEOUT)


def available() -> bool:
    """Whether this server has a recogniser configured at all."""
    return bool(get_settings().dictation_url.strip())


def base_url() -> str:
    """The configured service's base URL, without a trailing slash."""
    raw = get_settings().dictation_url.strip()
    if not raw:
        raise NotConfigured(NOT_CONFIGURED)
    parts = urlsplit(raw)
    try:
        # Read only for its ValueError. urlsplit accepts "host:abc" and leaves
        # the complaint to whoever asks for the port.
        parts.port
    except ValueError as exc:
        raise NotConfigured(BAD_URL) from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise NotConfigured(BAD_URL)
    return raw.rstrip("/")


def _resolves_publicly(base: str) -> bool:
    host = urlsplit(base).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError) as exc:
        # Not a refusal. In a compose stack a name that does not resolve is a
        # service that is not running (the `dictation` profile left off, or the
        # container restarting), and the sentence that helps is the one about
        # the container. Remembered only briefly, see UNRESOLVED_TTL.
        log.info("dictation: %s does not resolve (%s)", host, type(exc).__name__)
        raise Unreachable(UNREACHABLE) from exc
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if nethost.reachable(ip):
            return True
    return False


def _guard(base: str) -> None:
    """Refuse to send a recording to a service on a public address.

    The inverse of app/nethost.py, exactly as in app/grammar.py. That guard is
    for URLs typed into Settings and refuses *private* destinations, so that a
    text field cannot aim this server at its own network. This URL comes from
    the operator's file, and a private destination is exactly what it is meant
    to be. What must not happen silently is the opposite, somebody's voice going
    out to a hosted service because a URL was copied from a tutorial. So every
    address the host resolves to is checked, and one public address among them
    is enough to refuse.

    The verdict is cached per URL for GUARD_TTL. A name that does not resolve is
    reported as unreachable and remembered for UNRESOLVED_TTL. There is no
    pinning of the checked address as nethost.pinned does. The threat here is a
    mistake in a file the operator controls, not somebody racing DNS answers,
    and the operator can point the URL anywhere they like in any case.
    """
    if get_settings().dictation_allow_public_hosts:
        return
    now = time.monotonic()
    hit = _guard_cache.get(base)
    if hit is None or hit[0] <= now:
        try:
            verdict = "public" if _resolves_publicly(base) else "private"
        except Unreachable:
            _guard_cache[base] = (now + UNRESOLVED_TTL, "unresolved")
            raise
        hit = (now + GUARD_TTL, verdict)
        _guard_cache[base] = hit
    if hit[1] == "unresolved":
        raise Unreachable(UNREACHABLE)
    if hit[1] == "public":
        raise PublicHost(PUBLIC_REFUSAL)


def _backend_detail(res: httpx.Response) -> str:
    """The service's own words for a refusal.

    The service is a FastAPI app, so a refusal is {"detail": "..."} or, for a
    parameter that failed validation, {"detail": [{"msg": "..."}, ...]}. Anything
    in front of it (a proxy, a crash page) may answer plain text. Either is
    reduced to one line and cut to MAX_DETAIL_CHARS.

    Only "msg" is read from a validation error, never "input". FastAPI echoes
    the offending value there, and the request this answers carries the end of
    the user's draft in its query string.
    """
    text = ""
    try:
        body = res.json()
    except ValueError:
        body = None
        text = res.text
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, list):
            messages = [item.get("msg") for item in detail if isinstance(item, dict)]
            text = " ".join(m.strip().rstrip(".") + "." for m in messages
                            if isinstance(m, str) and m.strip())
        elif isinstance(detail, str):
            text = detail
    elif isinstance(body, str):
        text = body
    text = " ".join(text.split())
    if not text:
        return f"The dictation service refused the recording (HTTP {res.status_code})."
    if len(text) > MAX_DETAIL_CHARS:
        text = text[:MAX_DETAIL_CHARS - 3].rstrip() + "..."
    return text


def _request(method: str, url: str, **kwargs: Any) -> Any:
    """One call to the service, answered with its decoded JSON body.

    Logs say what failed and never with what. No audio, no prompt, no response
    body, because the request carries the user's voice and draft and the answer
    is what they said.
    """
    _in_request.active = True
    try:
        res = _http().request(method, url, timeout=_timeout(), **kwargs)
    except (httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        log.info("dictation: could not connect to the service (%s)", type(exc).__name__)
        raise Unreachable(UNREACHABLE) from exc
    except httpx.TimeoutException as exc:
        # Connected, then nothing for the whole read timeout. Usually a piece
        # queued behind a long one, or the model still loading, both of which
        # clear by themselves.
        log.info("dictation: the service did not answer in time (%s)", type(exc).__name__)
        raise Unreachable(
            f"The dictation service did not answer within "
            f"{get_settings().dictation_timeout_seconds} seconds. It may still be starting "
            f"up or busy with an earlier recording. Try again in a moment.") from exc
    except httpx.DecodingError as exc:
        log.warning("dictation: the service's answer could not be decoded")
        raise BadAnswer(BAD_ANSWER) from exc
    except httpx.InvalidURL as exc:
        raise NotConfigured(BAD_URL) from exc
    except httpx.HTTPError as exc:
        log.info("dictation: could not reach the service (%s)", type(exc).__name__)
        raise Unreachable(UNREACHABLE) from exc
    finally:
        _in_request.active = False

    status = res.status_code
    if 400 <= status < 500:
        log.info("dictation: the service refused a request with HTTP %d", status)
        raise Refused(_backend_detail(res))
    if not 200 <= status < 300:
        log.warning("dictation: the service answered HTTP %d", status)
        raise BadAnswer(BAD_ANSWER)
    try:
        # The service declares its JSON answer as text/plain (it streams the
        # same writer it uses for its txt, srt and vtt outputs), so the content
        # type says nothing and the body is parsed as JSON regardless.
        return res.json()
    except ValueError as exc:
        log.warning("dictation: the service answered with something that is not JSON")
        raise BadAnswer(BAD_ANSWER) from exc


def transcribe(audio: bytes, config: dict[str, Any], language: str | None = None,
               context: str | None = None) -> dict[str, str]:
    """Transcribe one piece of a recording. `config` is a settings value
    (parse_stored or validate), `language` overrides its language for this one
    piece, and `context` is the draft's text just before where the words go.

    Returns {"text": ..., "language": ...}. The text is "" when nothing in the
    piece was speech, and the language is the one Whisper detected for "auto",
    or the one asked for.
    """
    base = base_url()
    requested = validate_language(config.get("language", "auto") if language is None
                                  else language)
    _guard(base)

    params = {
        # Not "translate", which is Whisper turning any language into English.
        "task": "transcribe",
        # The only output that carries the per-segment scores keep_segment
        # reads. The others are plain text or subtitles.
        "output": "json",
        # Run the upload through ffmpeg first. The browser already sends the 16
        # kHz mono PCM Whisper wants, but without this the service reads the
        # bytes as raw samples, header and all, and a client that sent anything
        # else would be transcribed as noise.
        "encode": "true",
        # faster-whisper's voice activity filter, which cuts the silence out
        # before the model runs. The first defence against the inventions
        # keep_segment filters, and a piece with a long pause in it is faster.
        "vad_filter": "true",
    }
    # Left out for auto. Whisper then detects the language from the audio,
    # which for a piece of at most 30 seconds means from all of it.
    if requested != "auto":
        params["language"] = requested
    # In the query string, because the service declares initial_prompt as a
    # query parameter and reads it from nowhere else. That is why the compose
    # files start the whisper container with uvicorn's access log turned off
    # (--no-access-log), so the draft's tail does not land in `docker logs`. On
    # this side, see _HideQuery.
    prompt = build_prompt(config.get("words") or [], context)
    if prompt:
        params["initial_prompt"] = prompt

    body = _request("POST", f"{base}/asr", params=params,
                    files={"audio_file": ("dictation.wav", audio, "audio/wav")})
    if not isinstance(body, dict) or not isinstance(body.get("segments"), list):
        log.warning("dictation: the service's answer has no segment list")
        raise BadAnswer(BAD_ANSWER)

    kept = [segment["text"].strip() for segment in body["segments"] if keep_segment(segment)]
    # Whisper starts most segments with a space and sometimes doubles one, so
    # the joined text has its whitespace collapsed. The browser decides how it
    # meets the text already in the draft.
    text = " ".join(" ".join(kept).split())
    answered = body.get("language")
    # For auto, the service reports the language it detected here. Only a code
    # from Whisper's own table is passed on, because the browser may send it
    # back as the language of the next piece.
    detected = answered if isinstance(answered, str) and answered in _KNOWN_LANGUAGES else requested
    return {"text": text, "language": detected}
