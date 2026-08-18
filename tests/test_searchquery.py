"""Unit tests for the search-query filter parser (no server/DB).

The parser has to survive a half-typed query on every keystroke, and it has to
hand the text part back to a regex engine unharmed — those are the two things
worth pinning down here.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import searchquery


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


def test_a_plain_word_is_answered_by_the_index_alone():
    """The case that has to be exact, because nothing rechecks it.

    `search_tsv` holds every suffix of every word, so a run of letters and
    digits is a prefix of one of them wherever it occurs — including in the
    middle of a German compound, which is the whole reason the index is built
    that way.
    """
    assert searchquery.tsquery("rechnung") == ("'rechnung':*", True)
    assert searchquery.tsquery("RECHNUNG") == ("'rechnung':*", True)
    assert searchquery.tsquery("Rechnungsprüfung") == ("'rechnungsprüfung':*", True)
    # Two characters is the shortest word the SQL function indexes, so it is
    # also the shortest term that can be asked for through the index.
    assert searchquery.tsquery("ab") == ("'ab':*", True)


def test_a_term_spanning_separators_is_only_a_prefilter():
    """Anything with a space or a symbol in it is ANDed, and flagged inexact.

    The index splits on those characters, so it cannot tell "how to build" from
    "build to how" — the caller has to recheck against the text. What matters
    here is that the second half of the pair says so.
    """
    assert searchquery.tsquery("how to build") == ("'how':* & 'to':* & 'build':*", False)
    assert searchquery.tsquery("50% off") == ("'50':* & 'off':*", False)
    assert searchquery.tsquery("ada@example.com") == ("'ada':* & 'example':* & 'com':*", False)
    # Underscore separates on both sides of the fence — see the SQL function.
    assert searchquery.tsquery("foo_bar") == ("'foo':* & 'bar':*", False)


def test_a_term_with_nothing_indexable_falls_back_entirely():
    """No lexemes to ask for: the caller has to use ILIKE and nothing else.

    A one-character term contributes no lexeme (the function skips words below
    two), and punctuation contributes none at all. Returning an empty query
    rather than a partial one is what keeps the fallback from being wrong.
    """
    assert searchquery.tsquery("a") == ("", False)
    assert searchquery.tsquery("$$$") == ("", False)
    assert searchquery.tsquery("") == ("", False)
    # One indexable piece, but the term is more than that piece, so it still
    # needs the recheck.
    assert searchquery.tsquery("x-ray") == ("'ray':*", False)


def test_lexemes_are_quoted_so_nothing_reparses_them():
    """The query is tsquery syntax, not something to hand the text parser.

    `to_tsquery` runs Postgres' text-search parser over its argument, and that
    parser reads `845e33d9` as a number in scientific notation and cuts it into
    `845e33 <-> d9` — a phrase query. `search_tsv` is built by splitting on
    non-alphanumerics, with no parser and no positions, so a phrase query
    against it matches nothing at all: 4.8% of random hex ids came back empty,
    which is exactly the shape of thing (an order number, an invoice, a tracking
    code) that people search their mail for. Quoting the lexeme is what stops
    anything reading it twice.
    """
    assert searchquery.tsquery("845e33d9") == ("'845e33d9':*", True)
    assert searchquery.tsquery("3e5d599f") == ("'3e5d599f':*", True)
    assert searchquery.tsquery("1e5") == ("'1e5':*", True)
    # A piece cannot contain a quote — it is letters and digits — but the
    # doubling that tsquery syntax asks for stays true anyway.
    assert "''" not in searchquery.tsquery("abc")[0]
