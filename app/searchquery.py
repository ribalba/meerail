"""Filter tokens (`:unread`, `:from <pattern>`) lifted out of a search query.

The search box is a single line, so filters are typed into the query itself
rather than added as more controls above the results. Parsing happens here
rather than in the router because the thread view has to strip the same tokens
before it highlights hits — otherwise `:unread` would be marked up as if the
user had searched for that word.

Whatever is not a filter token is left in the query verbatim: it is the text
search, and in regex mode it has to survive character for character. A token
swallows the whitespace that follows it, so removing one from the middle of a
query does not leave a double space behind in the pattern.
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
