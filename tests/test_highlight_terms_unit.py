"""Unit tests for the term split shared by the WHERE clause and the highlighter.

A quoted run is one term in the search, so it has to be one term everywhere
else: splitting it on whitespace leaves `"was` and `sent"` — quote characters
attached — and a mail found by an exact phrase then opens with nothing marked.
app/routers/messages.py builds its highlight patterns from these terms, and
app/static/js/app.highlight.js keeps the browser's copy of the same rule.

No server or DB: the split is pure text.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.searchquery import keyword_terms, like_escape, parse  # noqa: E402


def _patterns(q):
    """The keyword half of messages.match_patterns, over the same helpers."""
    return [re.compile(re.escape(t), re.I) for t in keyword_terms(parse(q).text)]


def _spans(pats, text):
    return [m.group(0) for p in pats for m in p.finditer(text)]


def test_quoted_run_is_one_term():
    assert keyword_terms('"was sent"') == ["was sent"]
    assert keyword_terms('urgent "was sent" today') == ["urgent", "was sent", "today"]
    assert keyword_terms("was sent") == ["was", "sent"]
    assert keyword_terms("   ") == []
    # Unbalanced quote closes at end of query — search fires per keystroke.
    assert keyword_terms('"was se') == ["was se"]


def test_a_phrase_is_highlighted_as_a_phrase():
    pats = _patterns('"was sent"')
    assert _spans(pats, "The mail was sent yesterday") == ["was sent"]
    assert _spans(pats, "it WAS SENT") == ["WAS SENT"]   # ILIKE ignores case; so does this
    assert _spans(pats, "was never sent") == []          # the words apart are not the phrase

    # Unquoted, the same words are independent substrings — as the search ANDs them.
    assert _spans(_patterns("was sent"), "was never sent") == ["was", "sent"]


def test_filters_are_not_highlighted():
    # `:unread` narrowed the results; it is not something the user searched for.
    assert [p.pattern for p in _patterns(':unread "was sent"')] == [re.escape("was sent")]
    assert _patterns(":unread") == []


def test_like_escape_keeps_a_term_literal():
    # `50% off` is a term the user typed, not an ILIKE wildcard.
    assert like_escape("50% off") == r"50\% off"
    assert like_escape("a_b") == r"a\_b"
    assert like_escape("c:\\path") == "c:\\\\path"
