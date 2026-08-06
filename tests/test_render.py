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


# --- Remote content that is not an <img> ------------------------------------
#
# Blocking the image and leaving the stylesheet is a tracking pixel with one
# extra step: mail is laid out in inline CSS, so `style` has to survive, and a
# url() inside it is a request the sender gets to watch us make. The reader says
# images are blocked; that has to be true of every way the markup can ask.


def test_blocks_a_tracking_pixel_hidden_in_css():
    safe, blocked = sanitize_html(
        '<div style="background-image:url(https://tracker.example/open.gif)">hi</div>',
        1, load_remote=False)
    assert blocked == 1
    assert "tracker.example" not in safe
    assert "hi" in safe                      # the message itself is untouched
    assert "style=" in safe                  # and so is its layout


def test_blocks_every_property_that_fetches():
    """It matches the url(), not the property in front of it: background-image
    is the famous one, and these are the same request under other names."""
    for css in ("background: #fff url('http://t.example/p.gif') no-repeat",
                "list-style-image:url(//t.example/a.png)",
                "border-image:url(http://t.example/b.png) 30",
                "cursor:url(https://t.example/c.cur),auto",
                "content:url(https://t.example/d.png)"):
        safe, blocked = sanitize_html(f'<div style="{css}">x</div>', 1, load_remote=False)
        assert blocked == 1, css
        assert "t.example" not in safe, css


def test_blocks_a_url_that_hides_its_own_scheme():
    """CSS lets a url() escape its characters, so matching remote URLs by their
    spelling is a list of ways to be wrong. Everything that is not inert goes."""
    safe, blocked = sanitize_html(
        r'<div style="background-image:url(\68 ttps://t.example/x.gif)">x</div>',
        1, load_remote=False)
    assert blocked == 1
    assert "t.example" not in safe


def test_keeps_a_self_contained_css_image():
    safe, blocked = sanitize_html(
        '<div style="background-image:url(data:image/png;base64,AAAA)">x</div>',
        1, load_remote=False)
    assert blocked == 0
    assert "data:image/png" in safe


def test_css_images_load_with_the_rest_when_asked_for():
    safe, blocked = sanitize_html(
        '<div style="background-image:url(https://tracker.example/open.gif)">hi</div>',
        1, load_remote=True)
    assert blocked == 0
    assert "tracker.example" in safe


def test_blocks_a_src_that_spells_https_with_backslashes():
    r"""`https:\\host/x.gif` is an ordinary HTTPS request to a browser, which
    normalises the backslashes — and was not one of the spellings of "remote"
    the filter used to look for. Nothing that is not carried in the document
    itself is loaded now, so there is no list of spellings left to get wrong."""
    for src in (r"https:\\tracker.example/p.gif", r"\\\\tracker.example/p.gif",
                "HtTpS://tracker.example/p.gif", "relative/pixel.png"):
        safe, blocked = sanitize_html(f'<img src="{src}">', 1, load_remote=False)
        assert blocked == 1, src
        assert "tracker.example" not in safe, src
        assert "relative" not in safe, src


def test_blocks_a_css_url_whose_own_name_is_escaped():
    r"""CSS escapes work inside function names, so `u\72l(…)` is a url() to the
    browser and not to a regular expression looking for "url(". The value is
    decoded before anything decides what it does."""
    for css in (r"background-image:u\72l(https://tracker.example/p.gif)",
                r"background-image:\75\72\6C(https://tracker.example/p.gif)",
                r"background-image:URL(https://tracker.example/p.gif)"):
        safe, blocked = sanitize_html(f'<div style="{css}">x</div>', 1, load_remote=False)
        assert blocked == 1, css
        assert "tracker.example" not in safe, css


def test_blocks_a_url_that_never_used_url_at_all():
    """image-set() and its relatives take their URLs as plain strings, so there
    is no url() to catch them by. What is left after the url()s are dealt with
    is checked for a URL of its own."""
    safe, blocked = sanitize_html(
        '<div style="background-image:image-set(\'https://tracker.example/a.png\' 1x)">x</div>',
        1, load_remote=False)
    assert blocked == 1
    assert "tracker.example" not in safe


def test_a_base64_payload_full_of_slashes_still_survives():
    """The check for a stray URL runs on what is left once the url()s are out —
    base64 contains slashes, and dropping every inline image over that would be
    the blocker breaking the mail it is meant to be protecting."""
    safe, blocked = sanitize_html(
        '<div style="background-image:url(data:image/png;base64,a//b+c=);color:red">ok</div>',
        1, load_remote=False)
    assert blocked == 0
    assert "base64,a//b+c=" in safe
    assert "color:red" in safe
