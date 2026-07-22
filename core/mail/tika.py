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


def should_extract(content_type: str, filename: str = "") -> bool:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _EXTRACTABLE_TYPES or ct in _OCR_TYPES:
        return True
    return any(ct.startswith(p) for p in _EXTRACTABLE_PREFIXES)


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


def extract_text(payload: bytes, content_type: str, timeout: float = 60.0):
    """Return extracted text, or a marker describing how the attempt failed.

    Three outcomes, which callers must keep apart:

    * ``str`` — success. An empty string means a document with no text in it.
    * ``None`` — Tika is unreachable, timed out, or returned 5xx. Temporary;
      the attachment should stay queued and be retried on a later pass.
    * ``UNPROCESSABLE`` — Tika rejected the bytes (4xx). Permanent; requeueing
      only blocks the queue behind a file that will never extract.
    * ``TIMEOUT`` — Tika took the bytes and did not answer within ``timeout``.
      Attributable to this payload rather than the service, so the caller must
      not treat it as "Tika is down" and stop draining.
    """
    if not payload:
        return ""
    url = settings.tika_url.rstrip("/") + "/tika"
    headers = {"Accept": "text/plain"}
    effective = _effective_type(payload, content_type)
    if effective:
        headers["Content-Type"] = effective
    try:
        resp = httpx.put(url, content=payload, headers=headers, timeout=timeout)
    except httpx.TimeoutException:
        return TIMEOUT
    except Exception:
        return None
    if resp.status_code >= 500 or resp.status_code in _TRANSIENT_STATUSES:
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
