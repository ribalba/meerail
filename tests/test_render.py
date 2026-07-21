"""Unit tests for HTML sanitization / remote-content blocking. Pure — no server."""

from app.mail.render import sanitize_html


def test_strips_script():
    safe, _ = sanitize_html("<p>hi</p><script>alert(1)</script>", 1, load_remote=False)
    assert "<script" not in safe.lower()
    assert "alert" not in safe


def test_blocks_remote_images_by_default():
    safe, blocked = sanitize_html('<img src="http://tracker.example/x.gif"><b>hi</b>', 1, False)
    assert blocked == 1
    assert "tracker.example" not in safe
    assert "hi" in safe


def test_loads_remote_when_requested():
    safe, blocked = sanitize_html('<img src="http://tracker.example/x.gif">', 1, load_remote=True)
    assert blocked == 0
    assert "tracker.example" in safe


def test_rewrites_cid_to_endpoint():
    safe, _ = sanitize_html('<img src="cid:logo123">', 42, load_remote=False)
    assert "/api/messages/42/cid/logo123" in safe
    assert "cid:" not in safe


def test_keeps_self_contained_data_uri():
    safe, blocked = sanitize_html('<img src="data:image/png;base64,AAAA">', 1, load_remote=False)
    assert "data:image/png" in safe
    assert blocked == 0
