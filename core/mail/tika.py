"""Apache Tika client for attachment text extraction.

Runs as a separate container; we PUT bytes and get back plain text. We only call
Tika for content types likely to hold extractable text, to avoid wasting work on
images/video/archives (unless you switch to the Tika `-full` image for OCR).
"""

from __future__ import annotations

import httpx

from ..config import get_settings

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


def should_extract(content_type: str, filename: str = "") -> bool:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _EXTRACTABLE_TYPES:
        return True
    return any(ct.startswith(p) for p in _EXTRACTABLE_PREFIXES)


def extract_text(payload: bytes, content_type: str, timeout: float = 60.0) -> str:
    """Return extracted plain text, or '' on any failure (best-effort)."""
    if not payload:
        return ""
    url = settings.tika_url.rstrip("/") + "/tika"
    headers = {"Accept": "text/plain"}
    if content_type:
        headers["Content-Type"] = content_type
    try:
        resp = httpx.put(url, content=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.text.strip()
    except Exception:
        return ""


def health() -> bool:
    try:
        resp = httpx.get(settings.tika_url.rstrip("/") + "/version", timeout=5.0)
        return resp.status_code == 200
    except Exception:
        return False
