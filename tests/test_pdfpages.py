"""A PDF attachment page by page. Covers app/pdfpages.py and the two routes in front of it.

The phone's attachment viewer shows a PDF as one picture per page, because a
phone browser does not draw a framed PDF past its first page. The first half
here is the renderer by itself, with no server. The second half asks the test
server for pages of a real attachment.

What is pinned, and why:

  * the page list, because the viewer lays out every sheet from it before any
    picture has arrived, and a wrong shape is a page that jumps as it loads
  * the caps, because the file is the sender's and each cap is what stands
    between one request and a bitmap of several gigabytes
  * the refusals, because the viewer falls back to framing the file on any of
    them, and a 500 in their place is a viewer that stays empty
"""

from __future__ import annotations

import struct
import threading
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime

import pytest

import dbfixture
from helpers import api, api_bytes, build_pdf, build_png

pymupdf = pytest.importorskip("pymupdf", reason="the page renderer needs PyMuPDF")

import app.pdfpages as pdfpages  # noqa: E402

T0 = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)


def make_pdf(sizes, picture: bool = False, **save) -> bytes:
    """A PDF with one page per `(width, height)` in points, each saying which it is."""
    doc = pymupdf.open()
    for number, (w, h) in enumerate(sizes, start=1):
        page = doc.new_page(width=w, height=h)
        page.insert_text((40, 60), f"Page {number} of {len(sizes)}", fontsize=20)
        if picture:
            page.insert_image(pymupdf.Rect(40, 100, 240, 250), stream=build_png(200, 150))
    data = doc.tobytes(**save)
    doc.close()
    return data


def png_size(data: bytes) -> tuple[int, int]:
    assert data.startswith(b"\x89PNG\r\n\x1a\n"), data[:8]
    return struct.unpack(">II", data[16:24])


A4 = (595, 842)


# --- The renderer by itself ----------------------------------------------

def test_layout_lists_every_page_in_the_shape_it_is_shown():
    total, sizes = pdfpages.layout(make_pdf([A4, (842, 595), A4]))
    assert total == 3
    assert sizes == [(595.0, 842.0), (842.0, 595.0), (595.0, 842.0)]


def test_layout_applies_a_pages_rotation():
    """A page stored upright and marked as turned is listed the way it is drawn."""
    doc = pymupdf.open()
    doc.new_page(width=595, height=842).set_rotation(90)
    data = doc.tobytes()
    doc.close()
    assert pdfpages.layout(data) == (1, [(842.0, 595.0)])


def test_layout_stops_listing_at_the_cap_and_still_reports_the_real_count(monkeypatch):
    monkeypatch.setattr(pdfpages, "MAX_PAGES", 2)
    total, sizes = pdfpages.layout(make_pdf([A4] * 5))
    assert total == 5
    assert len(sizes) == 2


def test_a_text_page_is_drawn_as_png_at_the_width_asked_for():
    data, media = pdfpages.render(make_pdf([A4, A4]), 2, 600)
    assert media == "image/png"
    width, height = png_size(data)
    assert width == 600
    assert abs(height - 600 * 842 / 595) <= 1


def test_a_page_with_a_picture_is_drawn_as_jpeg():
    """A scan as PNG is megabytes. Anything carrying an image goes out as JPEG."""
    data, media = pdfpages.render(make_pdf([A4], picture=True), 1, 600)
    assert media == "image/jpeg"
    assert data.startswith(b"\xff\xd8\xff")


def test_a_png_that_grew_too_large_falls_back_to_jpeg(monkeypatch):
    monkeypatch.setattr(pdfpages, "PNG_MAX_BYTES", 100)
    data, media = pdfpages.render(make_pdf([A4]), 1, 600)
    assert media == "image/jpeg"


def test_the_width_is_clamped_to_the_range_the_route_accepts():
    pdf = make_pdf([A4])
    assert png_size(pdfpages.render(pdf, 1, 5)[0])[0] == pdfpages.MIN_WIDTH
    assert png_size(pdfpages.render(pdf, 1, 10**6)[0])[0] == pdfpages.MAX_WIDTH


def test_a_very_tall_page_is_capped_by_pixels_not_only_by_width():
    """A till receipt at full width would otherwise be a bitmap without limit."""
    data, _ = pdfpages.render(make_pdf([(200, 14000)]), 1, 2000)
    width, height = png_size(data)
    assert width < 2000
    assert width * height <= pdfpages.MAX_PIXELS * 1.01


def test_a_page_past_the_end_is_its_own_error():
    pdf = make_pdf([A4])
    for number in (0, 2, 99):
        with pytest.raises(pdfpages.NoSuchPage):
            pdfpages.render(pdf, number, 600)


@pytest.mark.parametrize("payload", [b"", b"this is not a pdf at all", b"%PDF-1.4\ngarbage"])
def test_bytes_that_are_not_a_pdf_are_unreadable(payload):
    with pytest.raises(pdfpages.Unreadable):
        pdfpages.layout(payload)
    with pytest.raises(pdfpages.Unreadable):
        pdfpages.render(payload, 1, 600)


def test_a_password_protected_pdf_is_unreadable_rather_than_blank():
    locked = make_pdf([A4], encryption=pymupdf.PDF_ENCRYPT_AES_256,
                      user_pw="open sesame", owner_pw="owner")
    with pytest.raises(pdfpages.Unreadable, match="password"):
        pdfpages.layout(locked)
    with pytest.raises(pdfpages.Unreadable, match="password"):
        pdfpages.render(locked, 1, 600)


def test_a_file_over_the_size_cap_is_refused_before_it_is_opened(monkeypatch):
    pdf = make_pdf([A4])
    monkeypatch.setattr(pdfpages, "MAX_SOURCE_BYTES", len(pdf) - 1)
    with pytest.raises(pdfpages.Unreadable, match="too large"):
        pdfpages.layout(pdf)


def test_pages_asked_for_from_several_threads_at_once_all_arrive():
    """The server answers from a thread pool and PyMuPDF is not thread-safe.

    A phone opens a document by asking for its first few pages together. This
    is that and more of it. Every answer has to be a whole picture of the
    right width, with no crash and no exception on the way.
    """
    pdf = make_pdf([A4] * 6)
    results: list = []

    def work(number: int) -> None:
        try:
            results.append(png_size(pdfpages.render(pdf, number, 400)[0])[0])
        except Exception as e:   # collected, so the assertion names it
            results.append(e)

    threads = [threading.Thread(target=work, args=(n % 6 + 1,)) for n in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [400] * 24


def test_only_pdf_content_types_qualify():
    assert pdfpages.is_pdf("application/pdf")
    assert pdfpages.is_pdf("Application/PDF; name=x.pdf")
    assert not pdfpages.is_pdf("image/png")
    assert not pdfpages.is_pdf("")


# --- Through the server --------------------------------------------------

def _mail_with(email: str, subject: str, *attachments) -> None:
    m = EmailMessage()
    m["Message-ID"] = f"<pages-{uuid.uuid4().hex}@t>"
    m["Subject"] = subject
    m["From"] = "x@y.com"
    m["To"] = email
    m["Date"] = format_datetime(T0)
    m.set_content("body")
    for name, maintype, subtype, data in attachments:
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    dbfixture.ingest_raw_message(email, m.as_bytes(), uid=1)


def _attachments(account_id: int, subject: str) -> dict:
    _, listing = api("GET", "/api/messages?scope=unified_inbox&limit=200")
    row = next(r for r in listing["rows"]
               if r["subject"] == subject and r["account_id"] == account_id)
    _, detail = api("GET", f"/api/messages/{row['id']}")
    return {a["filename"]: a for a in detail["attachments"]}


def test_the_server_lists_and_draws_the_pages_of_a_pdf_attachment(account):
    email, aid = account["email"], account["id"]
    _mail_with(email, "Three pages", ("three.pdf", "application", "pdf",
                                      make_pdf([A4, (842, 595), A4])))
    att = _attachments(aid, "Three pages")["three.pdf"]

    code, body = api("GET", f"/api/attachments/{att['id']}/pages")
    assert code == 200
    assert body == {"total": 3, "pages": [[595.0, 842.0], [842.0, 595.0], [595.0, 842.0]]}

    code, data, headers = api_bytes(f"/api/attachments/{att['id']}/pages/2?w=600")
    assert code == 200
    assert headers["Content-Type"] == "image/png"
    assert png_size(data) == (600, 424)          # the landscape one
    assert headers["X-Content-Type-Options"] == "nosniff"
    # Kept by the phone and by nobody in between.
    assert "private" in headers["Cache-Control"]
    assert "immutable" in headers["Cache-Control"]


def test_the_server_refuses_what_it_cannot_draw_with_a_sentence(account):
    email, aid = account["email"], account["id"]
    locked = make_pdf([A4], encryption=pymupdf.PDF_ENCRYPT_AES_256,
                      user_pw="open sesame", owner_pw="owner")
    _mail_with(email, "Odd ones",
               ("one.pdf", "application", "pdf", build_pdf("One page")),
               ("locked.pdf", "application", "pdf", locked),
               ("broken.pdf", "application", "pdf", b"%PDF-1.4\nnot really"),
               ("photo.png", "image", "png", build_png()))
    atts = _attachments(aid, "Odd ones")
    one = atts["one.pdf"]["id"]

    # Past the end is "not there". A width outside the range never reaches the
    # renderer, and neither does a page number that is not one.
    assert api("GET", f"/api/attachments/{one}/pages/2")[0] == 404
    assert api("GET", f"/api/attachments/{one}/pages/0")[0] == 422
    assert api("GET", f"/api/attachments/{one}/pages/1?w=10")[0] == 422
    assert api("GET", f"/api/attachments/{one}/pages/1?w=999999")[0] == 422
    assert api("GET", f"/api/attachments/{one}/pages/first")[0] == 422

    for name, expect in (("locked.pdf", "password"), ("broken.pdf", "could not be read"),
                         ("photo.png", "Only a PDF")):
        for path in ("/pages", "/pages/1"):
            code, body = api("GET", f"/api/attachments/{atts[name]['id']}{path}")
            assert code == 400, (name, path, body)
            assert expect in body["detail"], (name, path, body)

    assert api("GET", "/api/attachments/999999999/pages")[0] == 404
    assert api("GET", "/api/attachments/999999999/pages/1")[0] == 404


def test_pages_of_deleted_mail_are_gone_with_it(account):
    """An attachment's pages are a read path like any other.

    Mail deleted for good keeps its rows until the agent collects them, and
    nothing may be readable off them in the meantime.
    """
    email, aid = account["email"], account["id"]
    _mail_with(email, "Soon gone", ("gone.pdf", "application", "pdf", build_pdf("Bye")))
    att = _attachments(aid, "Soon gone")["gone.pdf"]
    assert api("GET", f"/api/attachments/{att['id']}/pages")[0] == 200

    assert dbfixture.drop_placements(email, "INBOX") >= 1

    assert api("GET", f"/api/attachments/{att['id']}/pages")[0] == 404
    assert api("GET", f"/api/attachments/{att['id']}/pages/1")[0] == 404
