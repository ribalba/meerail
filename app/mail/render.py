"""Sanitize email HTML for safe display.

- nh3 (ammonia) strips scripts/dangerous markup.
- Remote images are dropped by default (blocks tracking pixels); the reader can
  re-request with images=1 to load them. That means every remote fetch the
  markup can ask for, not just <img src>: inline CSS is kept for layout, so a
  `background-image: url(…)` is a tracking pixel by another name and is taken
  out with the same hand.
- Inline `cid:` images are rewritten to our attachment endpoint so they render.

The result is shown in a *sandboxed* iframe (no allow-scripts) as defense in depth.
"""

from __future__ import annotations

import re
from urllib.parse import quote

import nh3

# A generous tag set so real-world HTML mail keeps its layout, minus anything
# executable. <script>/<style> are intentionally excluded.
ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "center", "code", "col",
    "colgroup", "div", "em", "font", "h1", "h2", "h3", "h4", "h5", "h6", "hr",
    "i", "img", "li", "ol", "p", "pre", "s", "small", "span", "strike", "strong",
    "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
}

ALLOWED_ATTRS = {
    "*": {"style", "class", "align", "valign", "width", "height", "dir", "title", "bgcolor"},
    # NB: no "rel" here — nh3 manages it via link_rel and panics if both are set.
    "a": {"href", "name", "target", "title"},
    "img": {"src", "alt", "width", "height"},
    "td": {"colspan", "rowspan", "valign", "align", "bgcolor", "width", "height"},
    "th": {"colspan", "rowspan", "valign", "align", "bgcolor", "width", "height"},
    "table": {"border", "cellpadding", "cellspacing", "bgcolor", "width", "align"},
    "font": {"color", "face", "size"},
    "col": {"span", "width"},
}


# The two schemes that fetch nothing: a data: payload is carried in the document
# itself, and cid: names a part of this message, which the reader already has.
# Everything else — remote, relative, or unreadable — is a request.
_INERT = ("data:", "cid:")

# A url() in a style attribute, with whatever quoting and spacing was used.
# Non-greedy so a declaration carrying two of them loses both.
_CSS_URL = re.compile(r"""url\(\s*['"]?\s*([^'")]+?)\s*['"]?\s*\)""", re.I)

# A CSS escape: a backslash, then up to six hex digits (optionally closed by one
# whitespace), or any other single character standing for itself.
_CSS_ESCAPE = re.compile(r"\\(?:([0-9a-fA-F]{1,6})[ \t\n\r\f]?|(.))", re.S)

# The other ways a value can name something to fetch, once every url() in it has
# been dealt with. image-set() and friends take their URLs as plain strings, so
# there is no url() to catch them by.
_FETCHING_FUNCTIONS = ("image-set(", "image(", "cross-fade(", "element(", "src(")


def _unescape_css(value: str) -> str:
    """CSS as the browser will read it, with the escapes spelled out.

    ``u\\72l(https://tracker)`` is a url() to every browser and to no regular
    expression looking for the letters "url(" — CSS escapes work inside function
    names and identifiers, not just inside strings. Anything deciding what a
    declaration *does* therefore has to look at the decoded form, so this runs
    first and the decoded text is what is kept.
    """
    def decode(match: re.Match) -> str:
        if match.group(1) is not None:
            try:
                point = int(match.group(1), 16)
            except ValueError:            # unreachable via the pattern; cheap to hold
                return ""
            # Out of range or a lone surrogate: what a browser substitutes, and
            # what keeps this from raising on hostile input.
            if point == 0 or point > 0x10FFFF or 0xD800 <= point <= 0xDFFF:
                return "�"
            return chr(point)
        return match.group(2) or ""
    return _CSS_ESCAPE.sub(decode, value)


def _declarations(value: str) -> list[str]:
    """Split a style attribute into declarations on the semicolons that separate
    them — not on the ones inside a url(), where a data: payload keeps its own."""
    out, depth, start = [], 0, 0
    for i, char in enumerate(value):
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == ";" and depth == 0:
            out.append(value[start:i])
            start = i + 1
    out.append(value[start:])
    return out


def _strip_remote_css(value: str, blocked: list[int]) -> str:
    """Take the remote fetches out of one style attribute.

    ``style`` has to survive — mail is laid out in inline CSS, and dropping it
    wholesale turns half the world's newsletters into a column of unstyled text —
    but every property that takes a url() is a request the sender gets to watch
    us make. ``background-image`` is the obvious one; ``list-style-image``,
    ``border-image``, ``content``, ``cursor`` and ``filter`` are the same thing
    with different names, which is why this matches the url() rather than the
    property in front of it.

    Blocking the <img> src and leaving these was a tracking pixel with an extra
    step: one <div style="background-image:url(https://…/open.gif)"> and the
    message reports itself read on open, past a reader that says images are
    blocked. The count goes up with the images, so the "show remote content"
    banner appears for it and loading it stays the reader's decision.

    Three things in order, because each one is a way round the one before it.
    The value is decoded first, so an escaped ``u\\72l(`` is read as the url() it
    is. Every url() then keeps only the two inert schemes — matching remote URLs
    by their prefix instead is a list of spellings to get wrong, and a relative
    url() aims at whatever origin the reader is served from. What is left is
    checked for a URL that never needed a url() at all: ``image-set("https://…")``
    takes plain strings, and a declaration still carrying ``//`` after its url()s
    have been taken out has something in it we have not accounted for. That whole
    declaration goes; the rest of the style stays.
    """
    def replace(match: re.Match) -> str:
        if match.group(1).strip().lower().startswith(_INERT):
            return match.group(0)
        blocked[0] += 1
        return "none"           # a valid value for every property that takes one

    kept = []
    for declaration in _declarations(_unescape_css(value)):
        cleaned = _CSS_URL.sub(replace, declaration)
        # The url()s are already decided, and a kept data: payload is base64 —
        # which contains slashes. So they come out before the rest is read.
        rest = _CSS_URL.sub("", cleaned).lower()
        if "//" in rest or any(fn in rest for fn in _FETCHING_FUNCTIONS):
            blocked[0] += 1
            continue
        kept.append(cleaned)
    return ";".join(kept)


def sanitize_html(html: str, message_id: int, load_remote: bool) -> tuple[str, int]:
    """Return (safe_html, blocked_remote_count)."""
    blocked = [0]

    def attribute_filter(tag: str, attr: str, value: str) -> str | None:
        if tag == "img" and attr == "src":
            v = value.strip()
            if v.lower().startswith("cid:"):
                cid = v[4:].strip().strip("<>")
                return f"/api/messages/{message_id}/cid/{quote(cid, safe='')}"
            if load_remote:
                return value
            # Everything that is not carried in the document itself is dropped,
            # rather than only the spellings of "remote" we thought to list.
            # `https:\\tracker.example/p.gif` is not one of those spellings and
            # is an ordinary HTTPS request to a browser, which normalises the
            # backslashes — as it does for `\\\\tracker.example/p.gif`, and for
            # any relative src, which resolves against wherever the reader is
            # served from. A message has no business asking for any of them.
            if not v.lower().startswith("data:"):
                blocked[0] += 1
                return None  # drop the src -> the image does not load
        if attr == "style" and not load_remote:
            return _strip_remote_css(value, blocked)
        return value

    safe = nh3.clean(
        html or "",
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        attribute_filter=attribute_filter,
        link_rel="noopener noreferrer",
        strip_comments=True,
        # Keep cid: (rewritten by the filter) and data: (self-contained) image
        # sources; without cid here nh3 strips it before the filter runs.
        url_schemes={"http", "https", "mailto", "tel", "cid", "data"},
    )
    return safe, blocked[0]
