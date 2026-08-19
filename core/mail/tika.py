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
    """Singleton marker: the JVM went away with this payload inside it.

    Distinct from ``None``: nothing answered, but the connection was accepted
    and the request was in flight, and Tika does not drop requests it is merely
    busy with. What does drop them is the process dying — which for a payload
    this client chose is the payload's doing, not the service's. See
    core/ingest.py, which waits for Tika to come back and then burns the file
    rather than handing it the same one forever.
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
# `image/png` filenames — and Tika trusts the Content-Type header we supply, so
# a wrong label makes it hand a JPEG to the PNG reader and throw.
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
# for these two, which are back-pressure and genuinely worth another pass.
_TRANSIENT_STATUSES = {408, 429}

# What a heap exhaustion looks like from this side: a 500 whose body carries the
# JVM's own words for it. Worth matching on, because it is the one 5xx that is a
# property of the file rather than of the service — see extract_text.
_OOM_MARKERS = ("OutOfMemoryError", "Java heap space")

# How long Tika may spend inside one OCR call, sent per request so it is derived
# from the same number as the HTTP timeout below rather than configured next to
# it and left to drift.
#
# Without it the two budgets are independent, and whichever is larger is time
# spent on an answer the other side will not wait for. Tesseract's default is
# 120s per image; when we listened for 60 the container spent a further minute
# OCRing an attachment whose row had already been burned, which on a backlog of
# oversized scans is a core pinned permanently on work nobody will read. Tying
# Tika's budget to ours ends that: the parse stops while we are still listening,
# so the file is judged once, on the budget it was given. How that stop reaches
# us is its own question — see _TASK_TIMEOUT_KILL_WINDOW.
#
# The bounds are the server's, not ours: tika-server refuses anything under its
# minimumTimeoutMillis with a 400 and anything over its taskTimeoutMillis with a
# 500, and this client reads those as "burn the file" and "Tika is broken"
# respectively. Both would be wrong, and both would be caused by us, so the
# value is clamped rather than trusted.
_TASK_TIMEOUT_HEADER = "X-Tika-Timeout-Millis"
_MIN_TASK_TIMEOUT_MS = 30_000
_MAX_TASK_TIMEOUT_MS = 300_000
# Enough of a gap that Tika's answer arrives while we are still listening.
_TASK_TIMEOUT_MARGIN_MS = 5_000

# How the budget above is actually enforced, which is not by an HTTP status.
# tika-server hands the parse to a forked JVM and watches the clock; when the
# budget runs out its watchdog *kills that process* ("Shutting down forked
# process with status: TIMEOUT") and restarts it a second later. Sometimes the
# 422 makes it onto the wire first and sometimes the socket simply closes —
# measured on 3.3.1, both happen for the same file.
#
# The closed socket is indistinguishable from the JVM dying under a poison pill,
# and telling them apart matters: CRASHED hands the same payload back for a
# second go, which here means a second full budget burned and a second forked
# process killed to reach the verdict the first one already gave. What separates
# them is the clock. The watchdog fires at the budget and the kill takes
# milliseconds, so a disconnect in the last tenth of the budget is our own
# deadline coming back to us, and the file is finished — while a request cut
# down as a bystander of *another* file's kill is dropped at an arbitrary point
# in its own budget, which is the case CRASHED was written for and still gets.
_TASK_TIMEOUT_KILL_WINDOW = 0.9


def _task_timeout_ms(timeout: float) -> int:
    """Tika's per-OCR budget for a request this client will abandon at ``timeout``.

    Note what it is *not*: a cap on the whole parse. Tika applies it to each
    Tesseract call separately, so a fifty-page scan still gets fifty budgets and
    finishes — the HTTP timeout stays the limit on how long a single page may go
    quiet, which is what it was already.
    """
    wanted = int(timeout * 1000) - _TASK_TIMEOUT_MARGIN_MS
    return max(_MIN_TASK_TIMEOUT_MS, min(_MAX_TASK_TIMEOUT_MS, wanted))


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
# 300s is also the ceiling: tika-server refuses a larger per-task budget than
# its own taskTimeoutMillis, which defaults to exactly this. See _task_timeout_ms.
_EXTRACT_TIMEOUT = 300.0


def extract_text(payload: bytes, content_type: str, timeout: float = _EXTRACT_TIMEOUT):
    """Return extracted text, or a marker describing how the attempt failed.

    Three outcomes, which callers must keep apart:

    * ``str`` — success. An empty string means a document with no text in it.
    * ``None`` — Tika is unreachable, timed out, or returned 5xx. Temporary;
      the attachment should stay queued and be retried on a later pass.
    * ``UNPROCESSABLE`` — Tika rejected the bytes (4xx), or its watchdog killed
      the parse at the budget we set. Permanent; requeueing only blocks the
      queue behind a file that will never extract.
    * ``TIMEOUT`` — Tika took the bytes and did not answer within ``timeout``.
      Attributable to this payload rather than the service, so the caller must
      not treat it as "Tika is down" and stop draining.

    ``timeout`` bounds Tika's own work as well as our wait for it — see
    _task_timeout_ms — so an OCR that cannot finish in the budget comes back as
    a refusal rather than as silence we give up on.
    """
    if not payload:
        return ""
    url = settings.tika_url.rstrip("/") + "/tika"
    task_timeout_ms = _task_timeout_ms(timeout)
    headers = {
        "Accept": "text/plain",
        _TASK_TIMEOUT_HEADER: str(task_timeout_ms),
    }
    effective = _effective_type(payload, content_type)
    if effective:
        headers["Content-Type"] = effective
    started = time.monotonic()
    try:
        resp = httpx.put(url, content=payload, headers=headers, timeout=timeout)
    except httpx.TimeoutException:
        return TIMEOUT
    except httpx.ConnectError:
        # Refused or unreachable: this payload never got in, so it cannot be
        # what is wrong. Temporary, like the None below.
        return None
    except httpx.HTTPError:
        # Connected, sent, and then the far end vanished — either the watchdog
        # enforcing the budget we asked for, or a JVM that fell over. See
        # _TASK_TIMEOUT_KILL_WINDOW for why the clock is what tells them apart.
        spent_ms = (time.monotonic() - started) * 1000
        if spent_ms >= task_timeout_ms * _TASK_TIMEOUT_KILL_WINDOW:
            return UNPROCESSABLE
        return CRASHED
    except Exception:
        return None
    if resp.status_code >= 500:
        # A heap exhaustion is a 500, and it is entirely a property of this
        # file: rendering a PDF page for OCR allocates from the *decoded* size
        # of the images inside it, so a 2MB PDF holding one 9000x12000 scan
        # takes ~300MB every time it is parsed. Handing it back to Tika only
        # kills Tika again — and this server does not recover from that, it
        # answers 503 to everything until the container is restarted. So it is
        # burned like any other file Tika cannot read.
        if any(m in (resp.text or "") for m in _OOM_MARKERS):
            return UNPROCESSABLE
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
