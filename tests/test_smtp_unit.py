"""Unit coverage for the agent's SMTP wire format.

Pure unit test: nothing here opens a socket. What it pins down is the one
transformation that stands between the MIME the server built and the bytes a
mail server actually reads.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import smtp as agent_smtp  # noqa: E402


def test_bare_lf_becomes_crlf():
    """``EmailMessage.as_string()`` writes bare LF and ``sendmail`` leaves a
    bytes payload alone, so this is the only thing making the message legal."""
    assert agent_smtp.to_crlf(b"a\nb\nc") == b"a\r\nb\r\nc"


def test_crlf_is_left_alone():
    """Idempotent — a producer that already emits CRLF must not end up with
    blank lines between every line of the message."""
    assert agent_smtp.to_crlf(b"a\r\nb\r\nc") == b"a\r\nb\r\nc"


def test_lone_cr_is_normalised_too():
    assert agent_smtp.to_crlf(b"a\rb") == b"a\r\nb"


def test_mime_boundaries_end_properly():
    """The case that actually matters. A boundary is only a boundary when the
    line carrying it ends the way the spec says; a parser that misses one has
    no parts to choose between, and the alternative goes missing."""
    raw = (b"Content-Type: multipart/alternative; boundary=\"BOUND\"\n"
           b"\n"
           b"--BOUND\n"
           b"Content-Type: text/plain\n"
           b"\n"
           b"hello\n"
           b"--BOUND--\n")
    out = agent_smtp.to_crlf(raw)
    assert b"\r\n--BOUND\r\n" in out
    assert b"\r\n--BOUND--\r\n" in out
    assert out.count(b"\n") == out.count(b"\r\n")     # no LF survives on its own
