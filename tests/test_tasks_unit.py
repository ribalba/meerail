"""Unit coverage for the Meerato client's two pure pieces.

Pure unit test: no server, no database, no Meerato. `parse_endpoint` has to be
right before anything is stored, since everything downstream builds its URLs off
what it returns; `create_task`'s request body has to be right because a wrong
shape files the task in the wrong place rather than failing loudly.
"""

import httpx
import pytest

from app.meerato import create_task, parse_endpoint


def test_splits_the_url_meerato_hands_out():
    base, token = parse_endpoint("https://meerato.example.com/api/create?token=abc123")
    assert base == "https://meerato.example.com"
    assert token == "abc123"


def test_keeps_a_sub_path_mount():
    """Only the /api/create suffix is stripped — a Meerato behind a path prefix
    still needs that prefix to reach its attachment endpoint."""
    base, _ = parse_endpoint("https://host.example/todo/api/create?token=t")
    assert base == "https://host.example/todo"


def test_accepts_a_bare_origin_with_the_token():
    base, token = parse_endpoint("http://localhost:8080?token=t")
    assert base == "http://localhost:8080"
    assert token == "t"


def test_surrounding_whitespace_is_ignored():
    # Pasted URLs routinely arrive with a trailing newline.
    base, token = parse_endpoint("  https://m.example/api/create?token=abc\n")
    assert (base, token) == ("https://m.example", "abc")


@pytest.mark.parametrize("raw", ["", "   ", "meerato.example.com/api/create?token=t",
                                 "ftp://host/api/create?token=t"])
def test_rejects_anything_that_is_not_an_http_url(raw):
    with pytest.raises(ValueError, match="full http"):
        parse_endpoint(raw)


def test_rejects_a_url_with_no_token():
    with pytest.raises(ValueError, match="token"):
        parse_endpoint("https://meerato.example.com/api/create")


# --- The create request ----------------------------------------------------


def _sent_body(monkeypatch, **kwargs) -> dict:
    """Run create_task against a stubbed transport and hand back what it posted."""
    seen: dict = {}

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def post(self, url, params=None, json=None):
            seen.update(url=url, params=params, json=json)
            return httpx.Response(200, json={"id": "1", "public_token": "p", "title": "t"},
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "Client", FakeClient)
    create_task("https://m.example", "tok", "Title", "Body", **kwargs)
    return seen


def test_a_plain_task_carries_no_schedule(monkeypatch):
    seen = _sent_body(monkeypatch)
    assert seen["params"] == {"token": "tok"}
    assert seen["json"] == {"title": "Title", "text": "Body"}


def test_a_scheduled_task_parks_in_the_backlog_and_moves_to_now(monkeypatch):
    """What "Send & Ticket" asks for: filed under a bucket, status Backlog, and
    a date on which Meerato flips it onto the list by itself."""
    seen = _sent_body(monkeypatch, bucket_id="b1", status="open", schedule_date="2026-08-01")
    assert seen["json"] == {
        "title": "Title", "text": "Body", "bucket_id": "b1", "status": "open",
        "schedule": {"date": "2026-08-01", "status": "on_list"},
    }
