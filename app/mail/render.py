"""Sanitize email HTML for safe display.

- nh3 (ammonia) strips scripts/dangerous markup.
- Remote images are dropped by default (blocks tracking pixels); the reader can
  re-request with images=1 to load them.
- Inline `cid:` images are rewritten to our attachment endpoint so they render.

The result is shown in a *sandboxed* iframe (no allow-scripts) as defense in depth.
"""

from __future__ import annotations

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


def sanitize_html(html: str, message_id: int, load_remote: bool) -> tuple[str, int]:
    """Return (safe_html, blocked_remote_count)."""
    blocked = [0]

    def attribute_filter(tag: str, attr: str, value: str) -> str | None:
        if tag == "img" and attr == "src":
            v = value.strip()
            if v.lower().startswith("cid:"):
                cid = v[4:].strip().strip("<>")
                return f"/api/messages/{message_id}/cid/{quote(cid, safe='')}"
            if v.lower().startswith(("http://", "https://", "//")):
                if load_remote:
                    return value
                blocked[0] += 1
                return None  # drop remote src -> image doesn't load
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
