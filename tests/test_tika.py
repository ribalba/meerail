"""Pure tests for the attachment-extraction client's retry contract."""

import json

import httpx

from core.mail import tika


class Response:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)


def _capture(monkeypatch, response=None):
    """Stub httpx.put and record the url and headers it was called with."""
    seen = {}

    def put(url, content=None, headers=None, **_kwargs):
        seen["url"] = url
        seen["content"] = content
        seen["headers"] = headers
        return response if response is not None else Response()

    monkeypatch.setattr(tika.httpx, "put", put)
    return seen


def _pipes(status_code, status, message=""):
    """The JSON body tika-server answers a failed parse with."""
    return Response(status_code, json.dumps({"status": status, "message": message}))


def test_transport_failure_is_distinct_from_an_empty_document(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise httpx.ConnectError("Tika is down")

    monkeypatch.setattr(tika.httpx, "put", unavailable)
    assert tika.extract_text(b"document", "text/plain") is None


def test_successful_empty_document_returns_empty_string(monkeypatch):
    _capture(monkeypatch, Response(200, "  "))
    assert tika.extract_text(b"document", "text/plain") == ""


def test_text_is_asked_for_by_path_not_by_accept(monkeypatch):
    """The bare /tika endpoint returns Markdown in 4.x, whatever Accept says.

    Getting this wrong is silent: every extracted body would carry the
    document's title and `#`/`-` markup into the search text, and nothing would
    fail to tell us.
    """
    seen = _capture(monkeypatch)
    tika.extract_text(b"document", "text/plain")
    assert seen["url"].endswith("/tika/text")


def test_server_error_is_retryable(monkeypatch):
    _capture(monkeypatch, Response(503))
    assert tika.extract_text(b"document", "text/plain") is None


def test_rejected_bytes_are_permanent(monkeypatch):
    """422 with nothing in it is Tika saying it got nowhere with the file."""
    _capture(monkeypatch, Response(422))
    assert tika.extract_text(b"document", "text/plain") is tika.UNPROCESSABLE


def test_a_partial_parse_keeps_what_it_extracted(monkeypatch):
    """422 carries the text from before the exception, and that text is the point.

    A PDF truncated after page three still has three pages worth of body in it.
    Reading the status alone and burning the row throws them away for nothing.
    """
    _capture(monkeypatch, Response(422, " three pages of it "))
    assert tika.extract_text(b"document", "application/pdf") == "three pages of it"


def test_a_heap_exhaustion_is_the_file_not_the_service(monkeypatch):
    """Tika names an OOM in the body, and it is the file's doing, not the server's.

    Rendering a PDF page for OCR allocates from the decoded size of the images
    inside it, so the same file takes the same heap every pass. The forked
    parser dies, the server itself is fine and serving the next request — so
    this is a row to burn, not a service to wait for.
    """
    _capture(monkeypatch, _pipes(503, "OOM", "java.lang.OutOfMemoryError: Java heap space"))
    assert tika.extract_text(b"document", "application/pdf") is tika.UNPROCESSABLE


def test_a_parse_that_ran_out_of_time_is_also_the_file(monkeypatch):
    """The budget is the server's now, and a document that exhausts it does so again."""
    _capture(monkeypatch, _pipes(503, "TIMEOUT", "Task timed out after 240000ms"))
    assert tika.extract_text(b"document", "application/pdf") is tika.UNPROCESSABLE


def test_an_unattributed_fork_death_gets_one_more_go(monkeypatch):
    """UNSPECIFIED_CRASH is a fork that died without saying why.

    That happens to whichever document was inside it, which need not be this
    one — an import draining alongside the agent loses its in-flight request the
    same way. Twice is what separates a poison pill from a bystander.
    """
    _capture(monkeypatch, _pipes(503, "UNSPECIFIED_CRASH", "EOFException"))
    assert tika.extract_text(b"document", "application/pdf") is tika.CRASHED


def test_an_ordinary_server_error_is_still_retryable(monkeypatch):
    _capture(monkeypatch, Response(500, "something else went wrong"))
    assert tika.extract_text(b"document", "application/pdf") is None


def test_a_server_that_cannot_start_a_parser_is_the_service(monkeypatch):
    """FAILED_TO_INITIALIZE is a fork that never came up — misconfiguration, not this file."""
    _capture(monkeypatch, _pipes(500, "FAILED_TO_INITIALIZE", "couldn't connect to server"))
    assert tika.extract_text(b"document", "application/pdf") is None


def test_a_dropped_connection_is_a_crash(monkeypatch):
    """Distinct from ConnectError: the request was accepted and then the far end went.

    A forked parser dying no longer looks like this — the server outlives it and
    answers 503 — so what reaches here is the server process itself going with
    this request inside it, and that earns the one retry CRASHED buys.
    """
    def died(*_args, **_kwargs):
        raise httpx.RemoteProtocolError("server disconnected")

    monkeypatch.setattr(tika.httpx, "put", died)
    assert tika.extract_text(b"document", "application/pdf") is tika.CRASHED


def test_back_pressure_stays_retryable(monkeypatch):
    """429 is the fork pool being full: nothing failed, so nothing is burned."""
    _capture(monkeypatch, Response(429))
    assert tika.extract_text(b"document", "text/plain") is None


def test_mislabelled_image_is_sent_under_its_real_type(monkeypatch):
    """Outlook labels JPEG bodies as image/png; the honest type is what we send."""
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


def test_no_per_request_budget_is_sent(monkeypatch):
    """4.x ignores the timeout header 3.x read, so sending one is a lie about the budget.

    What actually stops a runaway parse is totalTaskTimeoutMillis in the image's
    tika-config.json, set below this client's own wait so Tika answers rather
    than being abandoned mid-parse. A header here would read as if this client
    still set it.
    """
    seen = _capture(monkeypatch)
    tika.extract_text(b"scan", "image/jpeg", timeout=60.0)
    assert not any(h.lower().startswith("x-tika-") for h in seen["headers"])
