"""Unit tests for the search-query filter parser (no server/DB).

The parser has to survive a half-typed query on every keystroke, and it has to
hand the text part back to a regex engine unharmed — those are the two things
worth pinning down here.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import searchquery  # noqa: E402


def test_flags_are_lifted_out_of_the_text():
    p = searchquery.parse("invoice :unread")
    assert p.text == "invoice"
    assert p.unread is True

    p = searchquery.parse(":read")
    assert p.text == "" and p.unread is False and p.filtered

    p = searchquery.parse(":has-attachment quarterly")
    assert p.text == "quarterly" and p.has_attachments is True

    # The plural reads as the same filter — it is how the field is named.
    assert searchquery.parse(":has-attachments").has_attachments is True


def test_addresses_take_the_next_token():
    p = searchquery.parse(r":from @acme\.com :to ada@")
    assert p.from_pat == r"@acme\.com"
    assert p.to_pat == "ada@"
    assert p.text == ""

    # Quoted (a display name with a space in it) and the = form.
    assert searchquery.parse(':from "Ada Lovelace"').from_pat == "Ada Lovelace"
    assert searchquery.parse(":to=ada@x.com").to_pat == "ada@x.com"


def test_removing_a_filter_leaves_the_pattern_intact():
    """Regex mode gets the text back character for character, seams included."""
    assert searchquery.parse(r"^Re: \d+$ :unread").text == r"^Re: \d+$"
    assert searchquery.parse(r":unread ^Re: \d+$").text == r"^Re: \d+$"
    # No double space where the filter used to be.
    assert searchquery.parse("alpha :unread beta").text == "alpha beta"


def test_a_colon_word_that_is_not_a_filter_stays_in_the_text():
    for q in ("re:unread", ":todo", "a:from b", "http://x/:to"):
        assert searchquery.parse(q).text == q
        assert not searchquery.parse(q).filtered


def test_half_typed_filters_do_not_become_search_terms():
    """Search runs on every keystroke: `:from ` mid-type must not go looking for
    the literal ":from" and blank the results the user is watching."""
    for q in (":from", ":from ", ":to=", "urgent :from "):
        assert "from" not in searchquery.parse(q).text
        assert "to" not in searchquery.parse(q).text
    assert searchquery.parse("urgent :from ").text == "urgent"

    # A value may not start with a colon, so this is still "no sender yet".
    p = searchquery.parse(":from :unread")
    assert p.from_pat is None and p.unread is True


def test_the_last_of_a_repeated_filter_wins():
    p = searchquery.parse(":unread :read")
    assert p.unread is False
    assert searchquery.parse(":from a@x :from b@x").from_pat == "b@x"
