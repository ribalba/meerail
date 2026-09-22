"""Grammar and spelling checks for the composer, done by a checker on this machine.

The composer sends the paragraphs of a draft here on every pause in typing. This
module hands them to a LanguageTool server (or anything else speaking its v2
HTTP API, such as LingoTweaker) and turns the answer into the underlines the
browser draws. Three things shape it more than the feature itself does:

  * **The draft never leaves the machine unless the operator says it may.** A
    draft is the most private thing a mail client holds: it has not been sent,
    and it is often the version nobody was meant to read. The checker is a
    container on the compose network, and before any request goes out the
    configured host is resolved and refused if any address it has is public
    (see ``_guard``). ``grammar.allow_public_hosts`` is the only way past that.
    Nothing here logs the text or puts it into an error message, and nothing is
    sent at all on an install that has not set ``grammar.url``.
  * **One request per check, whatever the draft looks like.** The browser sends
    the prose as a list of segments, with quotes, code and the prefilled footer
    already left out (checking somebody else's spelling is only noise). They are
    joined into one text, so LanguageTool sees one document and answers once,
    and the matches are then split back out per segment.
  * **Offsets are UTF-16 code units.** LanguageTool is Java, and a Java string
    is UTF-16; so is a JavaScript one. Python counts code points instead, so an
    emoji ahead of a typo would shift every underline after it by one if this
    module counted the Python way. All the offset arithmetic below is done on
    the UTF-16 encoding for that reason, and what goes back to the browser is in
    the units the browser itself uses.

Kept free of FastAPI (like app/meerato.py), so the whole of it can be driven by
the unit tests through an httpx.MockTransport (see ``use_transport``). The
router, app/routers/grammar.py, does the database and turns the exceptions
below into status codes.
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
from bisect import bisect_right
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from core.config import get_settings
from . import nethost

log = logging.getLogger(__name__)

# The `settings` row this feature owns. Its value is the JSON object that
# parse_stored() reads and validate() checks.
SETTING_KEY = "grammar"

# What goes between two segments in the text sent to the checker. A blank line,
# so each segment reads as a paragraph of its own: joined with a space, the end
# of one paragraph and the start of the next would be one sentence to
# LanguageTool, and it would report grammar across a boundary the author never
# wrote. A match that starts inside a separator, or runs through one, belongs to
# no segment and is dropped (see _locate).
SEPARATOR = "\n\n"
_SEPARATOR_UNITS = 2

# Request limits, enforced by the router before anything is sent. A long mail is
# a few thousand characters; these are far above that and far below what makes
# one keystroke's check slow for everyone else using the same checker.
MAX_SEGMENTS = 500
MAX_CHARS = 60_000

# Limits on the stored settings. The dictionary is the one that grows: a word is
# added every time somebody presses "Add to dictionary", so it needs a ceiling,
# and ten thousand is more names and jargon than anybody collects by hand.
MAX_LANGUAGE_CHARS = 40
MAX_VARIANTS = 12
MAX_WORDS = 10_000
MAX_WORD_CHARS = 100
MAX_RULES = 1000
MAX_RULE_CHARS = 120

# LanguageTool offers up to dozens of replacements for a misspelling, ranked.
# Past the first few they are guesses nobody picks, and a menu of thirty is
# worse than one of six.
MAX_REPLACEMENTS = 6

# How much of the checker's own error message is passed on. Java LanguageTool
# answers an unknown language code with the list of every code it knows, which
# is several hundred characters of detail after the one sentence that matters.
MAX_DETAIL_CHARS = 300

# Failing to reach the checker should be quick to find out about: it is on the
# same host or the same compose network, so three seconds is already generous.
# The read side is grammar.timeout_seconds, which has to allow for LanguageTool
# loading a language the first time it is asked for one.
CONNECT_TIMEOUT = 3.0

# How long a privacy verdict on the configured URL is trusted. Checks run on
# every pause in typing, and a DNS lookup per keystroke would be a cost with no
# benefit: the URL is operator configuration and does not change under a
# running server. Five minutes still notices a name that is re-pointed.
GUARD_TTL = 300.0

# How long "that name does not resolve" is remembered. Much shorter, because it
# is not a verdict but a state: the checker's container is stopped, restarting
# or not started yet, and it should be found soon after it is back. It is
# remembered at all because the lookup that fails is slow (about four seconds
# for a missing service name on a compose network), and without this every
# pause in typing would spend those four seconds finding out the same thing.
UNRESOLVED_TTL = 10.0

# The checker's language list changes when the checker is upgraded, which is
# rare, so it is asked once an hour at most. The Settings page reads it every
# time it is opened.
LANGUAGES_TTL = 3600.0

NOT_CONFIGURED = ("Grammar checking is not set up on this server. "
                  "Set [grammar] url in meerail.toml.")
BAD_URL = ("grammar.url in meerail.toml is not an http:// or https:// address with a "
           "host name in it, so there is nothing to send a check to.")
UNREACHABLE = "The grammar checker is not answering. Is the languagetool container running?"
BAD_ANSWER = "The grammar checker sent an answer meerail could not read."
PUBLIC_REFUSAL = ("grammar.url points at a public address, so your drafts would leave this "
                  "machine. Point it at a checker on your own network, or set "
                  "grammar.allow_public_hosts = true if that is intended.")


# --- Errors -------------------------------------------------------------------


class GrammarError(Exception):
    """The check could not be done. Each subclass carries a sentence meant for
    the person typing, and none of them ever contains any of the draft."""


class NotConfigured(GrammarError):
    """grammar.url is empty, or is not a URL a request could be sent to."""


class PublicHost(GrammarError):
    """The privacy guard: grammar.url resolves to a public address and the
    install has not said that is allowed. Nothing was sent."""


class Unreachable(GrammarError):
    """The checker could not be reached, or did not answer in time. Also a host
    that does not resolve: in a compose stack that is what a service which is
    not running looks like, and it is not a verdict about privacy."""


class Refused(GrammarError):
    """The checker answered 4xx (or 501, which LingoTweaker uses for a language
    its build lacks). The message is the checker's own, trimmed."""


class BadAnswer(GrammarError):
    """The checker answered with a server error, or with something that is not
    the LanguageTool v2 shape."""


class InvalidSetting(ValueError):
    """A settings value that cannot be stored, with a sentence saying why."""


# --- The settings row -----------------------------------------------------------

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "language": "auto",
    "variants": [],
    "picky": False,
    "words": [],
    "disabled_rules": [],
}

# BCP 47 as far as LanguageTool uses it: a two- or three-letter language, then
# up to four subtags. That covers de-DE, ca-ES-valencia and the private-use
# de-DE-x-simple-language. Matched with fullmatch, not ^...$, because `$` also
# matches before a trailing newline.
_LANGUAGE = re.compile(r"[a-z]{2,3}(-[A-Za-z0-9]{1,8}){0,4}")

# LanguageTool rule ids are upper-case words joined by underscores
# (MORFOLOGIK_RULE_EN_US); other checkers add dots, colons and dashes. Nothing
# else is needed, and keeping it to these makes the comma-joined disabledRules
# parameter safe to build.
_RULE = re.compile(r"[A-Za-z0-9_.:\-]{1,%d}" % MAX_RULE_CHARS)

# Unicode categories a dictionary word may not contain: control characters
# (newlines and tabs among them), lone surrogates (which Postgres cannot store),
# and the line and paragraph separators.
_FORBIDDEN_IN_WORDS = frozenset({"Cc", "Cs", "Zl", "Zp"})


def defaults() -> dict[str, Any]:
    """A fresh copy of the defaults, safe to mutate."""
    return {key: list(value) if isinstance(value, list) else value
            for key, value in DEFAULTS.items()}


def _shown(value: Any) -> str:
    """A value quoted for an error message, cut short: the message is shown in a
    settings field, and echoing a megabyte back would help nobody."""
    text = repr(value)
    return text if len(text) <= 50 else text[:47] + "..."


def _flag(name: str) -> Callable[[Any], bool]:
    def check(value: Any) -> bool:
        # isinstance(True, int) holds, but not the other way round: 1 is not
        # accepted as a boolean, so a client that means something else by it
        # is told rather than guessed at.
        if not isinstance(value, bool):
            raise InvalidSetting(f"{name} must be true or false.")
        return value
    return check


def validate_language(value: Any, *, allow_auto: bool = True) -> str:
    """A language code LanguageTool could be asked for, or "auto"."""
    if not isinstance(value, str):
        raise InvalidSetting("The language must be a code such as en-US or de-DE, or auto.")
    code = value.strip()
    if allow_auto and code == "auto":
        return code
    if len(code) > MAX_LANGUAGE_CHARS or not _LANGUAGE.fullmatch(code):
        tail = ", or auto." if allow_auto else "."
        raise InvalidSetting(f"{_shown(value)} is not a language code. "
                             f"Use a code such as en-US or de-DE{tail}")
    return code


def validate_word(value: Any) -> str:
    """One dictionary entry, stripped."""
    if not isinstance(value, str):
        raise InvalidSetting("A dictionary word must be text.")
    word = value.strip()
    if not word or len(word) > MAX_WORD_CHARS:
        raise InvalidSetting(f"A dictionary word must be between 1 and {MAX_WORD_CHARS} "
                             f"characters long.")
    if any(unicodedata.category(ch) in _FORBIDDEN_IN_WORDS for ch in word):
        raise InvalidSetting("A dictionary word cannot contain line breaks or control "
                             "characters.")
    return word


def validate_rule(value: Any) -> str:
    """One rule id to switch off."""
    if not isinstance(value, str) or not _RULE.fullmatch(value.strip()):
        raise InvalidSetting(f"{_shown(value)} is not a rule id. Rule ids are letters, "
                             f"digits and _ . : - only, at most {MAX_RULE_CHARS} characters.")
    return value.strip()


def _each(value: Any, check: Callable[[Any], str], what: str, lenient: bool) -> list[str]:
    """Every entry of a list through `check`. Lenient (reading a stored row)
    skips an entry that no longer passes instead of refusing the list: rules
    can tighten between versions, and one entry that stopped validating must
    not cost somebody the other nine thousand words in their dictionary."""
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


def validate_variants(value: Any, *, lenient: bool = False) -> list[str]:
    """Preferred variants for auto-detection (en-GB rather than en-US, de-AT
    rather than de-DE), deduplicated in the order given."""
    codes = _each(value, lambda v: validate_language(v, allow_auto=False),
                  "variants", lenient)
    out = list(dict.fromkeys(codes))
    if len(out) > MAX_VARIANTS:
        if lenient:
            return out[:MAX_VARIANTS]
        raise InvalidSetting(f"At most {MAX_VARIANTS} preferred variants can be set.")
    return out


def validate_words(value: Any, *, lenient: bool = False) -> list[str]:
    """The personal dictionary: deduplicated ignoring case, keeping the first
    spelling seen, and stored sorted so the Settings list reads alphabetically.

    Case-insensitive because that is how it is applied (see check): a word added
    as "Kubernetes" also silences "kubernetes" typed in lower case, and two
    entries differing only in case would be one entry that the list shows twice.
    """
    seen: dict[str, str] = {}
    for word in _each(value, validate_word, "words", lenient):
        seen.setdefault(word.casefold(), word)
    if len(seen) > MAX_WORDS and not lenient:
        raise InvalidSetting(f"The dictionary holds at most {MAX_WORDS} words.")
    out = sorted(seen.values(), key=lambda w: (w.casefold(), w))
    return out[:MAX_WORDS]


def validate_rules(value: Any, *, lenient: bool = False) -> list[str]:
    """Rule ids switched off with "Turn off this rule", deduplicated in
    the order they were added."""
    out = list(dict.fromkeys(_each(value, validate_rule, "disabled_rules", lenient)))
    if len(out) > MAX_RULES:
        if lenient:
            return out[:MAX_RULES]
        raise InvalidSetting(f"At most {MAX_RULES} rules can be switched off.")
    return out


_VALIDATORS: dict[str, Callable[..., Any]] = {
    "enabled": _flag("enabled"),
    "language": validate_language,
    "variants": validate_variants,
    "picky": _flag("picky"),
    "words": validate_words,
    "disabled_rules": validate_rules,
}
_LISTS = ("variants", "words", "disabled_rules")


def validate(config: dict[str, Any]) -> dict[str, Any]:
    """A complete settings value, checked and normalised; missing keys take the
    default, unknown keys are dropped. Raises InvalidSetting."""
    base = defaults()
    return {key: check(config.get(key, base[key])) for key, check in _VALIDATORS.items()}


def merge(current: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """`current` with the keys in `changes` replaced, validated as a whole."""
    return validate({**current, **{k: v for k, v in changes.items() if k in DEFAULTS}})


def parse_stored(raw: str | None) -> dict[str, Any]:
    """The stored row as a settings value, never raising.

    A row that is absent, is not JSON or is not an object reads as the
    defaults. Inside a readable row each field falls back on its own, and the
    lists keep whichever entries still pass: the row is only ever written by
    this module, so anything odd in it is a row from another version, and the
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


# --- Segments and UTF-16 offsets ----------------------------------------------


def _units(text: str) -> int:
    """Length in UTF-16 code units, which is what Java and JavaScript count."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def clean_segment(text: str) -> str:
    """The segment with any unpaired surrogate replaced by U+FFFD.

    JSON can carry a lone surrogate ("\\ud800") and Python will hold one in a
    str, but it cannot be encoded to UTF-8, so it would fail the request to the
    checker as a 500. U+FFFD is one UTF-16 unit, exactly as the lone surrogate
    is in the browser's copy of the text, so every offset after it still lines
    up with what the browser has.
    """
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def join_segments(segments: list[str]) -> tuple[str, list[tuple[int, int]]]:
    """The one text sent to the checker, and each segment's [start, end) in it,
    in UTF-16 units."""
    spans: list[tuple[int, int]] = []
    pos = 0
    for i, segment in enumerate(segments):
        if i:
            pos += _SEPARATOR_UNITS
        end = pos + _units(segment)
        spans.append((pos, end))
        pos = end
    return SEPARATOR.join(segments), spans


def _locate(spans: list[tuple[int, int]], starts: list[int],
            offset: int, length: int) -> tuple[int, int] | None:
    """(segment index, offset within that segment) for a match reported against
    the joined text, or None for one that is not wholly inside a single segment:
    one that starts in a separator, or runs on past the end of its segment.
    Neither can be drawn as an underline in the text the author wrote."""
    i = bisect_right(starts, offset) - 1
    if i < 0:
        return None
    start, end = spans[i]
    if offset + length > end:
        return None
    return i, offset - start


def _slice(segment: str, offset: int, length: int) -> str:
    """The text a match covers, cut by UTF-16 units."""
    data = segment.encode("utf-16-le", "surrogatepass")
    return data[offset * 2:(offset + length) * 2].decode("utf-16-le", "replace")


# --- Matches --------------------------------------------------------------------

_STYLE_ISSUES = frozenset({"style", "locale-violation", "register"})


def issue_type(issue: str, category: str) -> str:
    """spelling, grammar or style: which colour of underline.

    LanguageTool's issueType alone does not say it. "misspelling" is also what
    the a/an rule (EN_A_VS_AN, category MISC) reports, which is grammar to
    anybody reading the underline; only the dictionary rules sit in category
    TYPOS as well. Everything that is neither a dictionary miss nor a matter of
    taste is grammar.
    """
    if issue == "misspelling" and category == "TYPOS":
        return "spelling"
    if issue in _STYLE_ISSUES:
        return "style"
    return "grammar"


# Java LanguageTool renders <suggestion>x</suggestion> in a message as “x”
# before sending it; LingoTweaker (as of its current alpha) leaves the markup
# raw. The browser escapes whatever it is given, so left alone the tags would
# be shown to the user literally.
_SUGGESTION = re.compile(r"<suggestion>(.*?)</suggestion>", re.S)


def render_message(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    return _SUGGESTION.sub(lambda m: "\u201c" + m.group(1) + "\u201d", text)


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _normalize(raw: Any) -> dict[str, Any] | None:
    """One backend match in the shape the browser reads, or None for one that
    cannot be placed. Offset and length are still against the joined text."""
    if not isinstance(raw, dict):
        return None
    offset, length = raw.get("offset"), raw.get("length")
    if not (_is_count(offset) and _is_count(length)):
        return None
    rule = raw.get("rule") if isinstance(raw.get("rule"), dict) else {}
    category = rule.get("category") if isinstance(rule.get("category"), dict) else {}
    replacements = []
    for item in raw.get("replacements") or []:
        # An empty value is a real suggestion: it means "delete this" (a
        # doubled word, a stray space), so it is kept.
        if isinstance(item, dict) and isinstance(item.get("value"), str):
            replacements.append(item["value"])
            if len(replacements) == MAX_REPLACEMENTS:
                break
    category_id = str(category.get("id") or "")
    return {
        "offset": offset,
        "length": length,
        "message": render_message(raw.get("message")),
        "short": render_message(raw.get("shortMessage")),
        "replacements": replacements,
        "rule": str(rule.get("id") or ""),
        "rule_description": str(rule.get("description") or ""),
        "category": category_id,
        "type": issue_type(str(rule.get("issueType") or ""), category_id),
    }


# --- Talking to the checker -----------------------------------------------------

# One client for the life of the process. httpx.Client is thread-safe, and the
# routes calling this run in FastAPI's threadpool; keeping it means the
# connection to the checker stays open between checks, which matters for a
# request made on every pause in typing. Per-request timeouts are passed on each
# call, so the client itself holds no setting.
_client: httpx.Client | None = None
_transport: httpx.BaseTransport | None = None
_client_lock = threading.Lock()

# url -> (expires at, "public" | "private" | "unresolved"). See _guard.
_guard_cache: dict[str, tuple[float, str]] = {}
# url -> (expires at, languages). See languages().
_languages_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}


def use_transport(transport: httpx.BaseTransport | None) -> None:
    """Send every request through `transport` from now on (None: the network),
    and forget everything cached. The seam the unit tests use to stand an
    httpx.MockTransport in for the checker."""
    global _client, _transport
    with _client_lock:
        old, _client, _transport = _client, None, transport
    if old is not None:
        old.close()
    _guard_cache.clear()
    _languages_cache.clear()


def _http() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None:
            # No redirects: a checker that answers with a 3xx is not a checker,
            # and following it would carry the draft to wherever it points,
            # past the guard that only looked at the configured host.
            #
            # And no proxy from the environment (trust_env=False). httpx would
            # otherwise honour HTTP_PROXY and HTTPS_PROXY, and on a machine that
            # sets them for its outbound traffic every draft would travel through
            # that proxy, which is exactly the leaving-the-machine the guard is
            # there to prevent, and one it cannot see.
            _client = httpx.Client(transport=_transport, follow_redirects=False,
                                   trust_env=False)
        return _client


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(float(get_settings().grammar_timeout_seconds), connect=CONNECT_TIMEOUT)


def available() -> bool:
    """Whether this server has a checker configured at all."""
    return bool(get_settings().grammar_url.strip())


def base_url() -> str:
    """The configured checker's base URL, without a trailing slash or /v2."""
    raw = get_settings().grammar_url.strip()
    if not raw:
        raise NotConfigured(NOT_CONFIGURED)
    parts = urlsplit(raw)
    try:
        # Read only for its ValueError: urlsplit accepts "host:abc" and leaves
        # the complaint to whoever asks for the port.
        parts.port
    except ValueError as exc:
        raise NotConfigured(BAD_URL) from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise NotConfigured(BAD_URL)
    base = raw.rstrip("/")
    # Most LanguageTool clients are configured with the API root, .../v2, and a
    # URL copied from one of them should work here as well.
    if base.endswith("/v2"):
        base = base[:-3]
    return base


def _resolves_publicly(base: str) -> bool:
    host = urlsplit(base).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError) as exc:
        # Not a refusal. In a compose stack a name that does not resolve is a
        # service that is not running (the `grammar` profile left off, or the
        # container restarting), and the sentence that helps is the one about
        # the container. Remembered only briefly; see UNRESOLVED_TTL.
        log.info("grammar: %s does not resolve (%s)", host, type(exc).__name__)
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
    """Refuse to send a draft to a checker on a public address.

    The inverse of app/nethost.py. That guard is for URLs typed into Settings
    and refuses *private* destinations, so that a text field cannot aim this
    server at its own network. This URL comes from the operator's file, and a
    private destination is exactly what it is meant to be; what must not happen
    silently is the opposite, drafts going out to a hosted service because a
    URL was copied from a tutorial. So every address the host resolves to is
    checked, and one public address among them is enough to refuse.

    The verdict is cached per URL for GUARD_TTL; a name that does not resolve is
    reported as unreachable and remembered for UNRESOLVED_TTL. There is no
    pinning of the checked address as nethost.pinned does: the threat here is a
    mistake in a file the operator controls, not somebody racing DNS answers,
    and the operator can point the URL anywhere they like in any case.
    """
    if get_settings().grammar_allow_public_hosts:
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
    """The checker's own words for a refusal.

    Java LanguageTool answers errors as plain text ("Error: 'xx' is not a
    language code known to LanguageTool. Supported language codes are: ...");
    LingoTweaker answers JSON, {"error": {"message": "..."}}. Either is reduced
    to one line and cut to MAX_DETAIL_CHARS.
    """
    text = ""
    try:
        body = res.json()
    except ValueError:
        body = None
        text = res.text
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            err = err.get("message")
        if not isinstance(err, str):
            err = body.get("message")
        text = err if isinstance(err, str) else ""
    elif isinstance(body, str):
        text = body
    text = " ".join(text.split())
    if text.startswith("Error: "):
        text = text[len("Error: "):]
    if not text:
        return f"The grammar checker refused the check (HTTP {res.status_code})."
    if len(text) > MAX_DETAIL_CHARS:
        text = text[:MAX_DETAIL_CHARS - 3].rstrip() + "..."
    return text


def _request(method: str, url: str, **kwargs: Any) -> Any:
    """One call to the checker, answered with its decoded JSON body.

    Logs say what failed and never with what: no text, no response body. The
    request carries a draft and a refusal could quote parts of it back.
    """
    try:
        res = _http().request(method, url, timeout=_timeout(), **kwargs)
    except (httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        log.info("grammar: could not connect to the checker (%s)", type(exc).__name__)
        raise Unreachable(UNREACHABLE) from exc
    except httpx.TimeoutException as exc:
        # Connected, then nothing for the whole read timeout. Usually a
        # LanguageTool still loading a language, which clears by itself.
        log.info("grammar: the checker did not answer in time (%s)", type(exc).__name__)
        raise Unreachable(
            f"The grammar checker did not answer within "
            f"{get_settings().grammar_timeout_seconds} seconds. It may still be starting "
            f"up; try again in a moment.") from exc
    except httpx.DecodingError as exc:
        log.warning("grammar: the checker's answer could not be decoded")
        raise BadAnswer(BAD_ANSWER) from exc
    except httpx.InvalidURL as exc:
        raise NotConfigured(BAD_URL) from exc
    except httpx.HTTPError as exc:
        log.info("grammar: could not reach the checker (%s)", type(exc).__name__)
        raise Unreachable(UNREACHABLE) from exc

    status = res.status_code
    if 400 <= status < 500 or status == 501:
        log.info("grammar: the checker refused a request with HTTP %d", status)
        raise Refused(_backend_detail(res))
    if not 200 <= status < 300:
        log.warning("grammar: the checker answered HTTP %d", status)
        raise BadAnswer(BAD_ANSWER)
    try:
        return res.json()
    except ValueError as exc:
        log.warning("grammar: the checker answered with something that is not JSON")
        raise BadAnswer(BAD_ANSWER) from exc


def _empty(segments: list[str], requested: str) -> dict[str, Any]:
    return {"language": {"code": requested, "name": "", "auto": requested == "auto"},
            "matches": [[] for _ in segments]}


def check(segments: list[str], config: dict[str, Any], language: str | None = None
          ) -> dict[str, Any]:
    """Check the draft's segments. `config` is a settings value (parse_stored
    or validate); `language` overrides its language for this one check.

    Returns {"language": {code, name, auto}, "matches": [[match, ...], ...]},
    with one inner list per segment, in order, and every offset in UTF-16 units
    relative to its own segment.
    """
    base = base_url()
    requested = validate_language(config.get("language", "auto") if language is None
                                  else language)
    segments = [clean_segment(s) for s in segments]
    # Nothing to check is answered here, without a request: a composer that has
    # just been opened sends its empty body, and it would be odd for that to
    # cost a round trip, or to fail because the checker is still starting.
    if not any(s.strip() for s in segments):
        return _empty(segments, requested)
    _guard(base)

    text, spans = join_segments(segments)
    form = {"text": text, "language": requested}
    variants = config.get("variants") or []
    # Only with auto. Both LanguageTool and LingoTweaker answer 400 to
    # preferredVariants next to a fixed language, and with a fixed language
    # the variant is already chosen.
    if requested == "auto" and variants:
        form["preferredVariants"] = ",".join(variants)
    rules = config.get("disabled_rules") or []
    if rules:
        form["disabledRules"] = ",".join(rules)
    if config.get("picky"):
        form["level"] = "picky"

    body = _request("POST", f"{base}/v2/check", data=form)
    if not isinstance(body, dict) or not isinstance(body.get("matches"), list):
        log.warning("grammar: the checker's answer has no match list")
        raise BadAnswer(BAD_ANSWER)

    answered = body.get("language") if isinstance(body.get("language"), dict) else {}
    code = answered.get("code")
    name = answered.get("name")
    result_language = {
        # For auto, LanguageTool reports the language it detected here.
        "code": code if isinstance(code, str) and code else requested,
        "name": name if isinstance(name, str) else "",
        "auto": requested == "auto",
    }

    # Also sent as disabledRules, but filtered here as well: not every checker
    # speaking this API honours the parameter, and a rule the user switched off
    # coming back anyway is the kind of thing that gets the feature turned off.
    disabled = set(rules)
    words = {w.casefold() for w in config.get("words") or []}
    starts = [start for start, _ in spans]
    matches: list[list[dict[str, Any]]] = [[] for _ in segments]
    for raw in body["matches"]:
        match = _normalize(raw)
        if match is None:
            continue
        where = _locate(spans, starts, match["offset"], match["length"])
        if where is None:
            continue
        index, offset = where
        if match["rule"] in disabled:
            continue
        # The personal dictionary. LanguageTool's public API has nowhere to send
        # one, so it is applied to the answer: a spelling match on a word the
        # user has added is dropped, ignoring case. Only spelling, because a
        # grammar match that happens to cover that word is about something else.
        if (match["type"] == "spelling" and words
                and _slice(segments[index], offset, match["length"]).casefold() in words):
            continue
        match["offset"] = offset
        matches[index].append(match)
    return {"language": result_language, "matches": matches}


def languages() -> list[dict[str, str]]:
    """What the checker can check, as [{code, name}] sorted by name.

    Only codes the settings validator accepts are listed, so the Settings page
    never offers a language that saving would then refuse. Java LanguageTool
    lists Simple German twice, once as de-DE-x-simple-language-DE, which has one
    subtag more than a language code here may have; the other entry stays.
    """
    base = base_url()
    _guard(base)
    now = time.monotonic()
    hit = _languages_cache.get(base)
    if hit is not None and hit[0] > now:
        return [dict(row) for row in hit[1]]

    body = _request("GET", f"{base}/v2/languages")
    if not isinstance(body, list):
        log.warning("grammar: the checker's language list is not a list")
        raise BadAnswer(BAD_ANSWER)
    seen: dict[str, dict[str, str]] = {}
    for row in body:
        if not isinstance(row, dict):
            continue
        code = row.get("longCode") or row.get("code")
        name = row.get("name")
        if not isinstance(code, str) or not isinstance(name, str):
            continue
        try:
            code = validate_language(code, allow_auto=False)
        except InvalidSetting:
            continue
        seen.setdefault(code, {"code": code, "name": name.strip() or code})
    out = sorted(seen.values(), key=lambda row: (row["name"].casefold(), row["code"]))
    _languages_cache[base] = (now + LANGUAGES_TTL, out)
    return [dict(row) for row in out]
