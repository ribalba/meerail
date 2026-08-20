"""Filter tokens (`:unread`, `:from <pattern>`, `:similar <fingerprint>`) lifted
out of a search query,
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
    r"(?:(?<=\s)|\A):(unread|read|has-attachment|has-attachments|no-trash)(?:\s+|\Z)", re.I
)
# `:from a@b`, `:from="Ada Lovelace"`. The value may not start with a colon, so
# `:from :unread` reads as a filter still being typed rather than as a search
# for the sender ":unread".
_ADDR_RE = re.compile(
    r'(?:(?<=\s)|\A):(from|to)(?:\s+|=)("[^"]*"|[^\s:]\S*)(?:\s+|\Z)', re.I
)
# `:similar <token>` / `:similar=<message id>` — mail whose body says the same
# thing as this one. The value is a body fingerprint token or a message id, not
# a pattern: the whole point is that it names a *shape* of mail that no wording
# you could type would pick out. See core/bodysig.py, and the Cleanup panel,
# which is where these queries usually come from.
_SIMILAR_RE = re.compile(
    r'(?:(?<=\s)|\A):(similar)(?:\s+|=)("[^"]*"|[^\s:]\S*)(?:\s+|\Z)', re.I
)
# Search runs on every keystroke, so `:from` with the address not yet typed has
# to mean "no filter yet" rather than "find the literal text :from".
_PARTIAL_RE = re.compile(r"(?:(?<=\s)|\A):(?:from|to|similar)=?\s*\Z", re.I)
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
    similar: str | None = None
    # True = hide mail that only exists in Trash. Never False: "show me the
    # deleted ones too" is the default, so the filter has nothing to turn off.
    no_trash: bool | None = None

    @property
    def filtered(self) -> bool:
        return any(v is not None for v in
                   (self.unread, self.has_attachments, self.from_pat, self.to_pat,
                    self.similar, self.no_trash))


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
        elif name == "no-trash":
            parsed.no_trash = True
        else:
            parsed.has_attachments = True
        return ""

    def take_similar(m: re.Match) -> str:
        value = m.group(2).strip('"')
        if value:
            parsed.similar = value
        return ""

    text = _SIMILAR_RE.sub(take_similar, q)
    text = _ADDR_RE.sub(take_addr, text)
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


# Runs of letters and digits — the same cut `meerail_search_lexemes` makes over
# the stored text, so what is asked for and what was indexed are split by one
# rule. Underscore is a separator on both sides.
_ALNUM_RE = re.compile(r"[^\W_]+", re.UNICODE)

# Words shorter than this contribute no lexemes (see the SQL function), so a
# piece below it cannot be asked for through the index.
_MIN_LEXEME = 2


def tsquery(term: str) -> tuple[str, bool]:
    """The tsquery matching `term`, and whether it matches it *exactly*.

    `messages.search_tsv` holds every suffix of every word in the message, so a
    substring of a word is a prefix of one of that word's suffixes: asking for
    `rechnung:*` finds the lexeme "rechnung" that "Stromrechnung" put there.
    For a term that is a single run of letters and digits that is not an
    approximation of ILIKE but the same answer — every occurrence of the term
    lies inside some word, and every word contributed the suffix that starts
    where the term does.

    Anything else — a quoted phrase, an address, `50% off` — spans the
    separators the index splits on, so the query it returns is a *superset* and
    the caller has to recheck it against the text. That is cheap in a way the
    bare ILIKE was not: by then there are a handful of candidate rows rather
    than thousands.

    Returns ("", False) when there is nothing to ask the index for (a one-letter
    term, or punctuation alone), meaning "fall back to ILIKE entirely".

    The one gap: words over 100 characters are indexed whole rather than by
    suffix, so a term hiding inside one is found only if it starts there. Those
    are hashes, tracking ids and base64 — not something anyone searches the
    middle of — and expanding them would cost more index than the case is worth.
    """
    lowered = term.lower()
    runs = _ALNUM_RE.findall(lowered)
    pieces = [r for r in runs if len(r) >= _MIN_LEXEME]
    if not pieces:
        return "", False
    exact = len(runs) == 1 and len(pieces) == 1 and pieces[0] == lowered
    # tsquery syntax, quoted lexeme by quoted lexeme, rather than something for
    # `to_tsquery` to parse. `to_tsquery` runs the text-search parser over its
    # argument, and that parser has opinions: it reads `845e33d9` — an ordinary
    # hex id, of the kind that is half of what anyone searches mail for — as a
    # number in scientific notation and splits it into `845e33 <-> d9`, a
    # *phrase* query. `search_tsv` is built by cutting the text on
    # non-alphanumerics, with no parser and no positions, so a phrase query
    # against it can never match anything: 4.8% of random hex ids silently found
    # nothing at all. Both sides have to be cut by the same rule, and this is
    # the side that was using someone else's.
    #
    # A piece is a run of letters and digits, so it cannot contain the quote
    # that would end the lexeme early; doubling it is what tsquery syntax asks
    # for regardless, and costs nothing to keep true.
    return " & ".join("'" + p.replace("'", "''") + "':*" for p in pieces), exact
