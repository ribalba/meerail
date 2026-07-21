"""Shared test helpers: HTTP client (stdlib only), sample-mail builders, probes."""

from __future__ import annotations

import base64
import json
import os
import socket
import uuid
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import format_datetime

SERVER = os.environ.get("MEERAIL_URL", "http://localhost:8000")

# A 1x1 transparent PNG, handy for inline-image tests.
PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def api(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        SERVER + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw.decode(errors="replace")


def upload_attachment(data: bytes, filename: str, content_type: str = "application/octet-stream"):
    """POST a file to /api/compose/attachments as multipart/form-data (stdlib only)."""
    boundary = "----meerail" + uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n".encode()
        + f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
        + f"Content-Type: {content_type}\r\n\r\n".encode()
        + data + b"\r\n"
        + f"--{boundary}--\r\n".encode()
    )
    req = urllib.request.Request(
        SERVER + "/api/compose/attachments", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def server_up() -> bool:
    try:
        code, _ = api("GET", "/healthz")
        return code == 200
    except Exception:
        return False


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def build_pdf(text: str) -> bytes:
    """A tiny but valid single-page PDF whose visible text is `text`."""
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
    ]
    stream = b"BT /F1 24 Tf 72 720 Td (" + text.encode("latin-1") + b") Tj ET"
    objs.append(b"<</Length " + str(len(stream)).encode() + b">>\nstream\n" + stream + b"\nendstream")
    objs.append(b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += str(i).encode() + b" 0 obj\n" + o + b"\nendobj\n"
    xref_pos = len(out)
    n = len(objs) + 1
    out += b"xref\n0 " + str(n).encode() + b"\n0000000000 65535 f \n"
    for off in offsets:
        out += ("%010d 00000 n \n" % off).encode()
    out += (b"trailer\n<</Size " + str(n).encode() + b"/Root 1 0 R>>\nstartxref\n"
            + str(xref_pos).encode() + b"\n%%EOF")
    return out


def make_message(mid, subject, frm, to, body, when, in_reply_to=None, refs=None,
                 text_attachment=None, pdf_text=None) -> bytes:
    m = EmailMessage()
    m["Message-ID"] = mid
    m["Subject"] = subject
    m["From"] = frm
    m["To"] = to
    m["Date"] = format_datetime(when)
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    if refs:
        m["References"] = " ".join(refs)
    m.set_content(body)
    if text_attachment is not None:
        m.add_attachment(text_attachment, filename="notes.txt")
    if pdf_text is not None:
        m.add_attachment(build_pdf(pdf_text), maintype="application", subtype="pdf",
                         filename="report.pdf")
    return m.as_bytes()
