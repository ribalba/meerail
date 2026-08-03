"""Filter tokens (`:unread`, `:from <pattern>`) lifted out of a search query,
and the split of what is left into the terms the search ANDs together.

The search box is a single line, so filters are typed into the query itself
rather than added as more controls above the results. Parsing happens here
rather than in the router because the thread view has to strip the same tokens
before it highlights hits — otherwise `:unread` would be marked up as if the
user had searched for that word.

Whatever is not a filter token is left in the query verbatim: it is the text
search, and in regex mode it has to survive character for character. A token
swallows the whitespace that follows it, so removing one from the middle of a
query does not leave a double space behind in the pattern.

`keyword_terms` lives here for the same reason the parser does: the WHERE
clause, the attachment-hit windows and the reader's highlighter must all cut the
query the same way, or a mail found by an exact phrase opens with nothing marked
(app/static/js/app.highlight.js keeps the browser's copy of this rule).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_FLAG_RE = re.compile(
    r"(?:(?<=\s)|\A):(unread|read|has-attachment|has-attachments)(?:\s+|\Z)", re.I
)
# `:from a@b`, `:from="Ada Lovelace"`. The value may not start with a colon, so
# `:from :unread` reads as a filter still being typed rather than as a search
# for the sender ":unread".
_ADDR_RE = re.compile(
    r'(?:(?<=\s)|\A):(from|to)(?:\s+|=)("[^"]*"|[^\s:]\S*)(?:\s+|\Z)', re.I
)
# Search runs on every keystroke, so `:from` with the address not yet typed has
# to mean "no filter yet" rather than "find the literal text :from".
_PARTIAL_RE = re.compile(r"(?:(?<=\s)|\A):(?:from|to)=?\s*\Z", re.I)
# A double-quoted run, or a bare run of non-space characters.
_TERMS_RE = re.compile(r'"([^"]*)"|(\S+)')


@dataclass
class Query:
    """A search query split into its text part and the filters around it."""

    text: str = ""
    unread: bool | None = None          # None = don't care
    has_attachments: bool | None = None
    from_pat: str | None = None
    to_pat: str | None = None

    @property
    def filtered(self) -> bool:
        return any(v is not None for v in
                   (self.unread, self.has_attachments, self.from_pat, self.to_pat))


def parse(q: str) -> Query:
    """Split `q` into filters and the free text that is left over.

    A repeated filter keeps the last one typed, which is what editing the tail
    of the query looks like from the outside.
    """
    parsed = Query()

    def take_addr(m: re.Match) -> str:
        value = m.group(2)
        if value.startswith('"'):
            value = value.strip('"')
        if value:
            setattr(parsed, f"{m.group(1).lower()}_pat", value)
        return ""

    def take_flag(m: re.Match) -> str:
        name = m.group(1).lower()
        if name == "unread":
            parsed.unread = True
        elif name == "read":
            parsed.unread = False
        else:
            parsed.has_attachments = True
        return ""

    text = _ADDR_RE.sub(take_addr, q)
    text = _FLAG_RE.sub(take_flag, text)
    text = _PARTIAL_RE.sub("", text)
    parsed.text = text.strip()
    return parsed


def keyword_terms(q: str) -> list[str]:
    """Split a keyword query into the substrings to AND together.

    Quoted runs survive as a single term, so `"how to build"` matches that
    phrase rather than the three words scattered anywhere in the mail. An
    unbalanced trailing quote is treated as an open phrase to end-of-query
    (`"how to bui` while still typing) instead of erroring — search runs on
    every keystroke, so a half-typed quote must not blank the results.
    """
    if q.count('"') % 2:
        q += '"'
    terms = [(a or b) for a, b in _TERMS_RE.findall(q)]
    return [t for t in terms if t.strip()]


def like_escape(term: str) -> str:
    """Make a term a literal for ILIKE: `50% off` is not a wildcard."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
