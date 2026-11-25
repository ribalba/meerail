"""The pages of a PDF attachment as pictures, for the phone's attachment viewer.

A desktop browser draws a PDF itself, so there the reader hands it the file
and is done. A phone does not. Safari on iOS draws the first page of a PDF
that sits in a frame and nothing else, with no way to reach page two, and
Chrome on Android draws none of it. The viewer (app.mobile.js) used to frame
the file anyway, which meant a contract read on a phone ended at its cover
page. So it asks for the pages here instead, one picture each, which every
browser can show, scroll and pinch.

Drawn when asked for, never stored. The preview the agent keeps
(core/mail/thumbs.py) is one small tile per attachment. Every page of every
PDF at reading size would outweigh the PDFs themselves, for pages nobody
opens. This makes the server the second place that decodes an attachment, and
the rules that keep that safe in the agent hold here as well. The file is the
sender's, so nothing in it is trusted:

- PyMuPDF is not safe to call from two threads at once, and the server
  answers from a thread pool while a phone asks for several pages together.
  One lock goes around every call into the library. It also means one page is
  drawn at a time, so the memory a hostile file can ask for is one page's
  worth and not one per request.
- The size of the file, the number of pages listed and the pixels in one
  picture are all capped. A page tree can claim a million pages in a few
  hundred bytes, and a poster-sized page at reading width is gigabytes.
- Whatever goes wrong with a file ends as `Unreadable` with a sentence for
  the user. The viewer falls back to framing the file, which is what it did
  before this existed, so a PDF this cannot draw is no worse off.
"""

from __future__ import annotations

import math
import threading

# The same ceiling the thumbnailer uses, for the same reason. Opening the file
# is where an attachment gets to allocate memory in proportion to its size.
MAX_SOURCE_BYTES = 64 * 1024 * 1024

# How many pages one listing describes. Far more than a phone will scroll
# through, and it bounds the loop in `layout` for a file that lies about its
# length. The real count is reported next to it, so the viewer can say that
# there is more and point at the file itself.
MAX_PAGES = 1000

# What the route accepts for a picture's width, in pixels. The floor keeps a
# typo from asking for a postage stamp, and the ceiling is a phone at three
# device pixels per CSS pixel with some room left to zoom in.
MIN_WIDTH = 200
MAX_WIDTH = 2000

# The most pixels in one picture, whatever the width. A4 at MAX_WIDTH is under
# six million. This only bites on a page far taller than it is wide (a till
# receipt, a scrolled web page saved as one sheet), which would otherwise turn
# a reasonable width into an unreasonable bitmap.
MAX_PIXELS = 12_000_000

# A page of text is smaller and sharper as PNG, and quicker to encode. A scan
# is a photograph and comes out several megabytes that way, so a page that
# carries a picture goes out as JPEG, as does any PNG that grew past this.
PNG_MAX_BYTES = 1_200_000
JPEG_QUALITY = 82

# Stands in for a page whose own size cannot be read, so the viewer still
# reserves a sheet for it. A4 in points.
_FALLBACK_SIZE = (595.0, 842.0)

_PDF_TYPES = {"application/pdf", "application/x-pdf"}

# See the module docstring. Every call into PyMuPDF happens under this.
_LOCK = threading.Lock()


class Unreadable(Exception):
    """This file cannot be shown as pages. The message is written for the user."""


class NoSuchPage(Exception):
    """The PDF is fine and has no page with that number."""


def is_pdf(content_type: str) -> bool:
    return (content_type or "").split(";")[0].strip().lower() in _PDF_TYPES


def _open(payload: bytes):
    """Opens the document. Call with `_LOCK` held, and close what it returns."""
    try:
        import pymupdf
    except ImportError as e:
        # The server image always has it. A server run from a bare checkout
        # may not, and that is a missing preview, not a broken server.
        raise Unreadable("This server cannot draw PDF pages.") from e
    if not payload:
        raise Unreadable("This PDF is empty.")
    if len(payload) > MAX_SOURCE_BYTES:
        raise Unreadable("This PDF is too large to show page by page.")
    try:
        doc = pymupdf.open(stream=payload, filetype="pdf")
    except Exception as e:
        raise Unreadable("This PDF could not be read.") from e
    try:
        # A protected PDF opens without complaint and then draws blank sheets.
        # The browser's own viewer can ask for the password, this cannot.
        if doc.needs_pass:
            raise Unreadable("This PDF is password-protected.")
        if doc.page_count < 1:
            raise Unreadable("This PDF has no pages.")
    except Unreadable:
        doc.close()
        raise
    except Exception as e:
        doc.close()
        raise Unreadable("This PDF could not be read.") from e
    return doc


def layout(payload: bytes) -> tuple[int, list[tuple[float, float]]]:
    """How many pages the PDF has, and the size of each of the first MAX_PAGES.

    Sizes are width and height in points, as the page is displayed (rotation
    applied). The viewer only uses their ratio, to hold a sheet of the right
    shape open before its picture has arrived.
    """
    with _LOCK:
        doc = _open(payload)
        try:
            total = doc.page_count
            sizes: list[tuple[float, float]] = []
            for index in range(min(total, MAX_PAGES)):
                try:
                    rect = doc.load_page(index).rect
                    size = (round(rect.width, 1), round(rect.height, 1))
                except Exception:
                    # One damaged page is not a damaged document. It keeps its
                    # place in the list, and drawing it will say what is wrong.
                    size = _FALLBACK_SIZE
                sizes.append(size if size[0] > 0 and size[1] > 0 else _FALLBACK_SIZE)
            return total, sizes
        finally:
            doc.close()


def render(payload: bytes, number: int, width: int) -> tuple[bytes, str]:
    """Page `number` (the first page is 1) as a picture `width` pixels wide.

    Returns the bytes and their media type. Raises NoSuchPage past the end of
    the document, and Unreadable for a page that cannot be drawn.
    """
    width = max(MIN_WIDTH, min(MAX_WIDTH, int(width)))
    with _LOCK:
        doc = _open(payload)
        try:
            if number < 1 or number > doc.page_count:
                raise NoSuchPage(number)
            try:
                import pymupdf

                page = doc.load_page(number - 1)
                rect = page.rect
                if rect.width <= 0 or rect.height <= 0:
                    raise ValueError("page has no area")
                # Drawn straight at the size asked for. Rendering large and
                # shrinking afterwards is how a poster turns into a bitmap of
                # several hundred megabytes on the way to a phone screen.
                zoom = min(width / rect.width,
                           math.sqrt(MAX_PIXELS / (rect.width * rect.height)))
                pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
                if not page.get_images():
                    png = pix.tobytes("png")
                    if len(png) <= PNG_MAX_BYTES:
                        return png, "image/png"
                return pix.tobytes("jpeg", jpg_quality=JPEG_QUALITY), "image/jpeg"
            except Exception as e:
                raise Unreadable(f"Page {number} of this PDF could not be drawn.") from e
        finally:
            doc.close()
