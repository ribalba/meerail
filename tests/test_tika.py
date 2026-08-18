"""Pure tests for the attachment-extraction client's retry contract."""

import httpx

from core.mail import tika


class Response:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


def _capture(monkeypatch, response=None):
    """Stub httpx.put and record the headers it was called with."""
    seen = {}

    def put(_url, content=None, headers=None, **_kwargs):
        seen["content"] = content
        seen["headers"] = headers
        return response if response is not None else Response()

    monkeypatch.setattr(tika.httpx, "put", put)
    return seen


def test_transport_failure_is_distinct_from_an_empty_document(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise httpx.ConnectError("Tika is down")

    monkeypatch.setattr(tika.httpx, "put", unavailable)
    assert tika.extract_text(b"document", "text/plain") is None


def test_successful_empty_document_returns_empty_string(monkeypatch):
    _capture(monkeypatch, Response(200, "  "))
    assert tika.extract_text(b"document", "text/plain") == ""


def test_server_error_is_retryable(monkeypatch):
    _capture(monkeypatch, Response(503))
    assert tika.extract_text(b"document", "text/plain") is None


def test_rejected_bytes_are_permanent(monkeypatch):
    """422 is Tika saying the file is unparseable — requeueing it wedges the queue."""
    _capture(monkeypatch, Response(422))
    assert tika.extract_text(b"document", "text/plain") is tika.UNPROCESSABLE


def test_a_heap_exhaustion_is_the_file_not_the_service(monkeypatch):
    """Tika answers an OOM with a 500 whose body names it.

    It is the one 5xx that must not be retried: rendering a PDF page for OCR
    allocates from the decoded size of the images inside it, so the same file
    takes the same heap every pass, and this server does not recover from an OOM
    — it answers 503 to everything afterwards. Handing it back is a queue that
    never moves again.
    """
    _capture(monkeypatch, Response(500, "java.lang.OutOfMemoryError: Java heap space"))
    assert tika.extract_text(b"document", "application/pdf") is tika.UNPROCESSABLE


def test_an_ordinary_server_error_is_still_retryable(monkeypatch):
    _capture(monkeypatch, Response(500, "something else went wrong"))
    assert tika.extract_text(b"document", "application/pdf") is None


def test_a_dropped_connection_is_blamed_on_the_payload(monkeypatch):
    """Distinct from ConnectError: the request was accepted and then the far end
    went away, which is what a JVM exiting under this very file looks like."""
    def died(*_args, **_kwargs):
        raise httpx.RemoteProtocolError("server disconnected")

    monkeypatch.setattr(tika.httpx, "put", died)
    assert tika.extract_text(b"document", "application/pdf") is tika.CRASHED


def test_back_pressure_stays_retryable(monkeypatch):
    _capture(monkeypatch, Response(429))
    assert tika.extract_text(b"document", "text/plain") is None


def test_mislabelled_image_is_sent_under_its_real_type(monkeypatch):
    """Outlook labels JPEG bodies as image/png; Tika trusts our header and throws."""
    seen = _capture(monkeypatch)
    tika.extract_text(b"\xff\xd8\xff\xe0jpegbody", "image/png")
    assert seen["headers"]["Content-Type"] == "image/jpeg"


def test_correctly_labelled_image_is_left_alone(monkeypatch):
    seen = _capture(monkeypatch)
    tika.extract_text(b"\x89PNG\r\n\x1a\npngbody", "image/png")
    assert seen["headers"]["Content-Type"] == "image/png"


def test_unrecognised_image_bytes_let_tika_detect(monkeypatch):
    """A label we know is suspect is worse than no label at all."""
    seen = _capture(monkeypatch)
    tika.extract_text(b"not an image at all", "image/png")
    assert "Content-Type" not in seen["headers"]


def test_non_image_types_are_never_second_guessed(monkeypatch):
    """Office formats are all ZIP containers and would sniff alike."""
    seen = _capture(monkeypatch)
    tika.extract_text(b"PK\x03\x04stuff", "application/vnd.oasis.opendocument.text")
    assert seen["headers"]["Content-Type"] == "application/vnd.oasis.opendocument.text"


def test_webp_signature_is_recognised(monkeypatch):
    seen = _capture(monkeypatch)
    tika.extract_text(b"RIFF\x00\x00\x00\x00WEBPvp8", "image/png")
    assert seen["headers"]["Content-Type"] == "image/webp"
