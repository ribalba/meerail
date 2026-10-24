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

from app.searchquery import keyword_terms, like_escape, parse, split_terms


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


def test_a_dash_leads_a_term_to_leave_out():
    """The question from the mail that asked for this: Eco-CI, but not NGINX."""
    assert split_terms("Eco-CI -NGINX") == (["Eco-CI"], ["NGINX"])
    # Only a dash where a term starts counts. The one inside a word is the word.
    assert split_terms("Eco-CI") == (["Eco-CI"], [])
    assert split_terms("x-ray -re-run") == (["x-ray"], ["re-run"])
    # A phrase can be left out as a whole.
    assert split_terms('deploy -"build failed"') == (["deploy"], ["build failed"])
    # Nothing but exclusions is still a query: everything without the word.
    assert split_terms("-nginx") == ([], ["nginx"])
    # A quoted dash is a dash to search for, which is how `-v` is asked for.
    assert split_terms('"-v" "-"') == (["-v", "-"], [])


def test_a_half_typed_exclusion_stands_for_nothing_yet():
    """Search fires per keystroke, so the dash before the word, and the first
    letter after it, must not blank or flood the results being watched."""
    assert split_terms("Eco-CI -") == (["Eco-CI"], [])
    assert split_terms("Eco-CI -N") == (["Eco-CI"], [])
    assert split_terms("Eco-CI -NG") == (["Eco-CI"], ["NG"])
    assert split_terms("a - b") == (["a", "b"], [])
    # An open quote after the dash reads as an open phrase, as it does without.
    assert split_terms('Eco-CI -"build fa') == (["Eco-CI"], ["build fa"])
    assert split_terms('Eco-CI -"') == (["Eco-CI"], [])


def test_a_term_left_out_is_not_highlighted():
    # It names what the results do not say. Marking it elsewhere in the thread
    # would light up the reason a message was passed over.
    assert [p.pattern for p in _patterns("Eco-CI -NGINX")] == [re.escape("Eco-CI")]
    assert keyword_terms('Eco-CI -NGINX -"build failed"') == ["Eco-CI"]
    assert _patterns("-NGINX :unread") == []
