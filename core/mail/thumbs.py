"""Attachment thumbnail generation.

Previews are precomputed at ingest rather than rendered on demand: attachment
bytes live in Postgres, so a browser-side preview would mean pulling the whole
8 MB photo down to draw a 240px chip. The agent's thumbnail pass writes a small
WebP into ``attachments.thumb`` once, and the web app serves that.

Two decoders, one encoder: PDFs get page 1 rasterised by PyMuPDF, images are
decoded by Pillow, and both then go through the same downscale-and-encode step.
PyMuPDF is used rather than pdf2image because the agent runs on the host's
Python, where there is no opportunity to apt-get poppler.

Everything here is best-effort in the same way as ``tika.extract_text``: a
corrupt or hostile attachment returns None and the caller records an error. A
bad PDF must never take down a sync pass.
"""

from __future__ import annotations

import io

# Longest edge of the generated preview, in pixels. Chips render at ~60px and
# the hover preview at ~240px; 2x that keeps it crisp on retina panels.
MAX_EDGE = 480

# WebP quality. 72 is visually clean at thumbnail scale and keeps these in the
# 5-15 KB range, which is a rounding error next to the payloads themselves.
QUALITY = 72

# Skip absurd payloads outright. Decoding is the one place a mail attachment
# gets to allocate memory proportional to attacker-controlled input, and a
# thumbnail is never worth an OOM in the agent.
MAX_SOURCE_BYTES = 64 * 1024 * 1024

_PDF_TYPES = {"application/pdf", "application/x-pdf"}

# Image types Pillow decodes reliably. Deliberately an allowlist and not
# `image/*`: image/svg+xml is a document format that Pillow won't take anyway,
# and enumerating keeps surprises out of the agent.
_IMAGE_TYPES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/webp",
    "image/bmp",
    "image/tiff",
    "image/x-ms-bmp",
}


# Every type with a preview, for callers that need to express the allowlist as a
# query rather than a per-row predicate (see ingest.backfill_thumbs).
THUMBABLE_TYPES = frozenset(_PDF_TYPES | _IMAGE_TYPES)


def _norm(content_type: str) -> str:
    return (content_type or "").split(";")[0].strip().lower()


def should_thumb(content_type: str) -> bool:
    ct = _norm(content_type)
    return ct in _PDF_TYPES or ct in _IMAGE_TYPES


def _encode(img) -> bytes:
    """Downscale in place and encode to WebP."""
    from PIL import Image, ImageOps

    # Phone photos are almost always stored rotated with the real orientation in
    # EXIF; without this the preview comes out sideways.
    img = ImageOps.exif_transpose(img)

    if img.mode not in ("RGB", "RGBA"):
        # Palette images with transparency lose it converting straight to RGB.
        img = img.convert("RGBA" if "transparency" in img.info else "RGB")

    img.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=QUALITY, method=4)
    return buf.getvalue()


def _from_pdf(payload: bytes) -> bytes | None:
    import pymupdf
    from PIL import Image

    with pymupdf.open(stream=payload, filetype="pdf") as doc:
        if doc.page_count == 0:
            return None
        # An encrypted PDF opens fine but renders blank, so treat it as no preview
        # rather than emitting an empty white tile.
        if doc.needs_pass:
            return None
        page = doc.load_page(0)
        # Render straight to the size we want instead of at full resolution and
        # downscaling after — a poster-sized page would otherwise rasterise to
        # hundreds of megabytes before we ever shrink it.
        rect = page.rect
        longest = max(rect.width, rect.height) or 1
        zoom = min(MAX_EDGE / longest, 4.0)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        with Image.open(io.BytesIO(pix.tobytes("png"))) as img:
            return _encode(img)


def _from_image(payload: bytes) -> bytes | None:
    from PIL import Image

    with Image.open(io.BytesIO(payload)) as img:
        return _encode(img)


def make_thumb(payload: bytes, content_type: str) -> bytes | None:
    """Return WebP preview bytes, or None if this attachment has no preview.

    Best-effort: returns None on anything malformed rather than raising.
    """
    if not payload or len(payload) > MAX_SOURCE_BYTES:
        return None

    ct = _norm(content_type)
    try:
        if ct in _PDF_TYPES:
            return _from_pdf(payload)
        if ct in _IMAGE_TYPES:
            return _from_image(payload)
    except Exception:
        # Truncated downloads, mislabelled content types, Pillow's decompression
        # bomb guard, encrypted PDFs — all just mean "no preview".
        return None
    return None


def available() -> bool:
    """Whether the imaging libraries are installed, for the preflight check."""
    try:
        import pymupdf  # noqa: F401
        from PIL import Image  # noqa: F401

        return True
    except ImportError:
        return False
