"""Apache Tika client for attachment text extraction.

Runs as a separate container; we PUT bytes and get back plain text. We only call
Tika for content types likely to hold extractable text, to avoid wasting work on
video/archives. Scanned images are OCR'd, which needs the Tika `-full` image
(it bundles Tesseract) — on the plain image they simply come back empty.

Failure has two flavours and callers must tell them apart: Tika being
unreachable is temporary and the attachment should stay queued, while Tika
rejecting the bytes is permanent and requeueing it just wedges the queue behind
a file that can never succeed. See ``extract_text``.
"""

from __future__ import annotations

import time

import httpx

from ..config import get_settings
from .parse import strip_nuls

settings = get_settings()

# Prefixes / exact types worth sending to Tika.
_EXTRACTABLE_PREFIXES = ("text/",)
_EXTRACTABLE_TYPES = {
    "application/pdf",
    "application/rtf",
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.presentation",
    "application/epub+zip",
    "application/xml",
    "application/json",
    "message/rfc822",
}

# Raster formats Tesseract actually handles. Deliberately not all of `image/`:
# OCR is expensive, and icons/GIFs/SVGs in signatures are pure waste.
_OCR_TYPES = {
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/bmp",
    "image/webp",
}


class _Unprocessable:
    """Singleton marker: Tika read the file and cannot parse it. Never retry."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "UNPROCESSABLE"


UNPROCESSABLE = _Unprocessable()


class _Crashed:
    """Singleton marker: a JVM went away with this payload inside it.

    Two shapes reach here. Tika answers ``503 {"status": "UNSPECIFIED_CRASH"}``
    when the forked parser holding this document died without saying why; and
    the connection simply closes when it is the server process itself that went,
    which nothing but that can cause once the request is in flight.

    Distinct from ``None`` in both cases: something did happen to this payload,
    and it may or may not have been this payload's doing. See core/ingest.py,
    which waits for Tika to come back and hands it the same file exactly once
    more — twice is what separates a poison pill from a bystander.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "CRASHED"


CRASHED = _Crashed()


class _Timeout:
    """Singleton marker: Tika accepted the bytes and never answered in time.

    Kept apart from ``None`` because the two need opposite handling. ``None``
    means the service is unreachable, so every queued attachment should wait.
    A timeout is a property of this payload — Tika is up, it is this file it
    cannot finish — so blocking the queue on it stalls everything behind it.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "TIMEOUT"


TIMEOUT = _Timeout()

# Leading bytes that identify the raster formats we send for OCR. Mail clients
# mislabel these constantly — Outlook in particular emits JPEG bodies under
# `image/png` filenames. Tika 3 trusted the Content-Type header we supplied and
# handed a mislabelled JPEG to the PNG reader, which threw; Tika 4 treats it as
# a hint and keeps it only where it agrees with what the bytes say, so a wrong
# label now costs nothing. Still sniffed, because a *right* label is what
# refines detection within a format family, and being honest about the type is
# cheap.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
)

# 4xx means Tika understood the request, so retrying changes nothing — except
# for these two, which are back-pressure and genuinely worth another pass. 429
# is what a busy server says now that parsing runs in a fixed pool of forked
# JVMs: nothing failed, the pool was full, and the same bytes succeed later.
_TRANSIENT_STATUSES = {408, 429}

# What a failed parse looks like from this side: a JSON body naming the outcome,
# where 3.x sent the plain text "Parse failed: TIMEOUT". The status is what
# separates a file that cannot be read from a service having a bad minute, so it
# is read rather than guessed at from the HTTP code.
#
# OOM and TIMEOUT are properties of the payload. Heap exhaustion is entirely
# one: rendering a PDF page for OCR allocates from the *decoded* size of the
# images inside it, so a 2MB PDF holding one 9000x12000 scan takes ~300MB every
# time it is parsed, and every later pass exhausts the same heap. A document
# that ran out of its budget runs out of it again. Both are burned like any
# other file Tika cannot read.
#
# UNSPECIFIED_CRASH is not: the fork died without attributing it, which happens
# to whichever document was inside it at the time. That one gets the single
# retry CRASHED buys — see core/ingest.py.
_FATAL_PIPES_STATUSES = {"OOM", "TIMEOUT"}
_CRASH_PIPES_STATUSES = {"UNSPECIFIED_CRASH"}


def _pipes_status(resp) -> str:
    """The ``status`` tika-server gives for a failed parse, or "" if it gave none."""
    try:
        body = resp.json()
    except Exception:
        return ""
    return body.get("status", "") if isinstance(body, dict) else ""


def should_extract(content_type: str, filename: str = "") -> bool:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _EXTRACTABLE_TYPES or ct in _OCR_TYPES:
        return True
    return any(ct.startswith(p) for p in _EXTRACTABLE_PREFIXES)


def ocr_types() -> frozenset[str]:
    """The image types extraction sends through OCR.

    Exposed so callers that have to express the set as a query rather than a
    per-row predicate — core/database.py's one-time retire of inline images
    already queued — cannot drift from the predicate below.
    """
    return frozenset(_OCR_TYPES)


def is_ocr_type(content_type: str) -> bool:
    """Whether extracting this type means OCR rather than reading text out.

    Callers use it to weigh the cost: OCR is seconds of CPU in the Tika
    container per file, where a PDF or a .docx is a parse. See
    core/mail/store.py, which declines to queue inline images on the strength
    of it.
    """
    return (content_type or "").split(";")[0].strip().lower() in _OCR_TYPES


def _sniff(payload: bytes) -> str | None:
    """Identify raster bytes by their signature, or None if unrecognised."""
    for prefix, ctype in _MAGIC:
        if payload.startswith(prefix):
            return ctype
    # RIFF....WEBP — the four size bytes in between are not fixed.
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    return None


def _effective_type(payload: bytes, content_type: str) -> str | None:
    """The Content-Type to send, or None to let Tika detect it itself.

    Only images are second-guessed. Their signatures are unambiguous and their
    declared types are unreliable, whereas text/* carries no magic worth
    trusting and the office formats are all ZIP containers that sniff alike.
    """
    ct = (content_type or "").split(";")[0].strip().lower()
    if not ct.startswith("image/"):
        return content_type or None
    # Unrecognised bytes under an image label: hand Tika no type at all rather
    # than a label we already know is suspect, and let AutoDetectParser decide.
    return _sniff(payload)


# Long enough for OCR of a scan at the sizes mail actually carries. A phone
# photo of an invoice is routinely 40+ megapixels, and Tesseract's cost is in
# pixels, not bytes: measured on this image, 21MP of dense text takes ~33s, so
# the 60s this used to be gave up on anything much past A4-at-camera-resolution
# and burned the attachment for it. Draining is slower for it — a file that
# cannot be read now holds its batch for five minutes rather than one — and that
# is the trade being made deliberately: the queue is a background thread on its
# own (see agent/main.py, where the indexer is not what stamps the agent's
# heartbeat), while an attachment burned for want of time is not read again.
#
# It is also the outer bound on a budget the server keeps for itself. Tika 4
# takes no per-request timeout — the header 3.x read for that is silently
# ignored — so the number that actually stops a runaway parse is
# totalTaskTimeoutMillis in the image's tika-config.json, set to 240s so that
# Tika gives up, and answers, inside the window this client is still listening
# in. The 60s of daylight between them is the wind-down that turns an exhausted
# budget into a truncated success rather than a hang.
_EXTRACT_TIMEOUT = 300.0


def extract_text(payload: bytes, content_type: str, timeout: float = _EXTRACT_TIMEOUT):
    """Return extracted text, or a marker describing how the attempt failed.

    Four outcomes, which callers must keep apart:

    * ``str`` — success. An empty string means a document with no text in it.
      A parse that ran out of its budget or threw part-way lands here too, with
      whatever had been extracted before it stopped.
    * ``None`` — Tika is unreachable, or busy, or broken in a way that is not
      about this file. Temporary; the attachment should stay queued and be
      retried on a later pass.
    * ``UNPROCESSABLE`` — Tika read the bytes and got nowhere: a rejected
      request, a parse that yielded nothing, a heap or a budget this document
      exhausts every time. Permanent; requeueing only blocks the queue behind a
      file that will never extract.
    * ``CRASHED`` — the parse took a JVM with it. Worth exactly one more go;
      see core/ingest.py.
    * ``TIMEOUT`` — Tika took the bytes and did not answer within ``timeout``.
      Attributable to this payload rather than the service, so the caller must
      not treat it as "Tika is down" and stop draining.

    ``timeout`` is this client's patience only. The budget Tika parses under is
    its own, set in tika-config.json and deliberately shorter — see
    _EXTRACT_TIMEOUT — so a parse that cannot finish comes back as an answer
    rather than as silence we give up on.
    """
    if not payload:
        return ""
    # /tika/text, not /tika: 4.x routes the output format by path and the bare
    # endpoint now returns Markdown, headings and bullets and all, whatever the
    # Accept header says. Indexing that would put `#` and `-` into the search
    # text and the document's title at the top of every body.
    url = settings.tika_url.rstrip("/") + "/tika/text"
    headers = {}
    effective = _effective_type(payload, content_type)
    if effective:
        headers["Content-Type"] = effective
    try:
        resp = httpx.put(url, content=payload, headers=headers, timeout=timeout)
    except httpx.TimeoutException:
        return TIMEOUT
    except httpx.ConnectError:
        # Refused or unreachable: this payload never got in, so it cannot be
        # what is wrong. Temporary, like the None below.
        return None
    except httpx.HTTPError:
        # Connected, sent, and then the far end vanished. A forked parser dying
        # no longer looks like this — the server outlives it and says so in a
        # 503 — so what is left is the server process itself going, with this
        # request inside it.
        return CRASHED
    except Exception:
        return None
    if resp.status_code == 422:
        # A container-level exception, with whatever was extracted before it as
        # the body: a PDF that turned out to be truncated after page three, an
        # image whose bytes stop half way. Keeping the partial text is the whole
        # point of Tika answering this way rather than with an error, and an
        # empty one says it got nothing at all, which is a file to burn.
        text = strip_nuls(resp.text).strip()
        return text if text else UNPROCESSABLE
    if resp.status_code >= 500:
        status = _pipes_status(resp)
        if status in _FATAL_PIPES_STATUSES:
            return UNPROCESSABLE
        if status in _CRASH_PIPES_STATUSES:
            return CRASHED
        # Anything else 5xx is the service, not the file — a fork that could not
        # start, a misconfiguration, a restart mid-request.
        return None
    if resp.status_code in _TRANSIENT_STATUSES:
        return None
    if resp.status_code >= 400:
        return UNPROCESSABLE
    return strip_nuls(resp.text).strip()


def health() -> bool:
    try:
        resp = httpx.get(settings.tika_url.rstrip("/") + "/version", timeout=5.0)
        return resp.status_code == 200
    except Exception:
        return False


def wait_for_health(timeout: float = 60.0, interval: float = 2.0) -> bool:
    """Whether Tika answers within ``timeout``, polling until it does.

    Only used after a crash, to tell "the file killed it and it came back" from
    "the service is gone". A container restarting is seconds; waiting a minute
    for it costs one pause on a path that should almost never be taken, and
    getting the answer wrong costs either a permanently wedged queue or a file
    burned for something that was not its fault.
    """
    deadline = time.monotonic() + timeout
    while True:
        if health():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)
