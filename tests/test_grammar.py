"""/api/grammar through the live server: settings, validation, and real checks.

Two halves. The first needs only the test stack: the settings row, its
validation, and the refusals that happen before anything is sent. The test
server has GRAMMAR_URL set (docker-compose.test.yml), so the feature reads as
available whether or not a checker is running.

The second needs a real LanguageTool, which only starts under the compose
profile `grammar`: `COMPOSE_PROFILES=grammar make test`. Without it every check
with text in it answers 503, and those tests skip rather than fail, so a plain
`make test` (and CI) stays as fast as it was. What they pin is the thing a mock
cannot: that the offsets a real LanguageTool reports come back relative to each
segment and in the UTF-16 units the browser counts in.

Every test starts and ends with no settings row, through `fresh`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

import dbfixture
from helpers import api

KEY = "grammar"
DEFAULTS = {"available": True, "enabled": True, "language": "auto", "variants": [],
            "picky": False, "words": [], "disabled_rules": []}


def _drop_row():
    from core.models import Setting

    with dbfixture.session() as db:
        row = db.get(Setting, KEY)
        if row:
            db.delete(row)


def _row():
    from core.models import Setting

    with dbfixture.session() as db:
        row = db.get(Setting, KEY)
        return row.value if row else None


@pytest.fixture
def fresh(require_server):
    _drop_row()
    yield
    _drop_row()


def put(body):
    return api("PUT", "/api/grammar/config", body)


def ignore(body):
    return api("POST", "/api/grammar/ignore", body)


def check(segments, language=None):
    body = {"segments": segments}
    if language is not None:
        body["language"] = language
    return api("POST", "/api/grammar/check", body)


# --- Settings -------------------------------------------------------------------


def test_the_defaults_before_anything_is_saved(fresh):
    code, cfg = api("GET", "/api/grammar/config")
    assert code == 200
    assert cfg == DEFAULTS
    # Reading writes nothing.
    assert _row() is None


def test_a_saved_setting_round_trips(fresh):
    code, cfg = put({"language": "de-DE", "variants": ["de-AT", "en-GB", "de-AT"],
                     "picky": True})
    assert code == 200, cfg
    assert cfg == {**DEFAULTS, "language": "de-DE", "variants": ["de-AT", "en-GB"],
                   "picky": True}
    assert api("GET", "/api/grammar/config") == (200, cfg)

    # A PUT is a patch: what is left out keeps its value.
    code, cfg = put({"enabled": False})
    assert code == 200
    assert cfg["enabled"] is False and cfg["language"] == "de-DE" and cfg["picky"] is True


def test_words_saved_in_bulk_are_cleaned_up(fresh):
    code, cfg = put({"words": [" zebra ", "Kubernetes", "kubernetes", "apple"]})
    assert code == 200
    assert cfg["words"] == ["apple", "Kubernetes", "zebra"]


@pytest.mark.parametrize("body", [
    {"language": "english"},
    {"language": "de_DE"},
    {"language": "x" * 41},
    {"language": 5},
    {"variants": ["auto"]},
    {"variants": [f"de-X{i:02d}" for i in range(13)]},
    {"variants": "de-AT"},
    {"disabled_rules": ["has space"]},
    {"disabled_rules": ["two,rules"]},
    {"words": ["two\nlines"]},
    {"words": "Kubernetes"},
    {"words": [7]},
    {"enabled": "yes"},
    {"enabled": 1},
    {"picky": "true"},
    {"enabled": None},
    {"language": None},
])
def test_invalid_settings_are_refused_and_change_nothing(fresh, body):
    code, detail = put(body)
    assert code == 422, (body, code, detail)
    assert _row() is None
    assert api("GET", "/api/grammar/config") == (200, DEFAULTS)


def test_a_body_that_is_not_an_object_is_a_422(fresh):
    assert put(["enabled"])[0] == 422
    assert api("POST", "/api/grammar/check", ["Teh cat"])[0] == 422
    assert api("POST", "/api/grammar/ignore", "teh")[0] == 422


# --- Ignoring -------------------------------------------------------------------


def test_ignore_adds_words_once_ignoring_case(fresh):
    code, cfg = ignore({"word": "Kubernetes"})
    assert code == 200, cfg
    assert cfg["words"] == ["Kubernetes"]

    # The same word in another case is already there: nothing changes, and it
    # is not an error either.
    assert ignore({"word": "kubernetes"}) == (200, cfg)
    code, cfg = ignore({"word": "  Meerail "})
    assert cfg["words"] == ["Kubernetes", "Meerail"]
    code, cfg = ignore({"word": "apple"})
    assert cfg["words"] == ["apple", "Kubernetes", "Meerail"]
    assert api("GET", "/api/grammar/config")[1]["words"] == cfg["words"]


def test_ignore_switches_rules_off_once(fresh):
    code, cfg = ignore({"rule": "EN_A_VS_AN"})
    assert code == 200
    assert cfg["disabled_rules"] == ["EN_A_VS_AN"]
    code, cfg = ignore({"rule": "EN_A_VS_AN", "word": "Teh"})
    assert cfg["disabled_rules"] == ["EN_A_VS_AN"]
    assert cfg["words"] == ["Teh"]


@pytest.mark.parametrize("body", [{}, {"word": ""}, {"word": "   "}, {"word": "two\nlines"},
                                  {"word": None}, {"word": 3}, {"rule": "a b"},
                                  {"rule": ""}, {"rule": None}])
def test_ignore_refuses_what_it_cannot_store(fresh, body):
    code, detail = ignore(body)
    assert code == 422, (body, code, detail)
    assert _row() is None


def test_concurrent_ignores_all_land(fresh):
    """Two clicks at once must not cost one of the two words: each is a
    read-modify-write of the same row, and the first ever write has no row to
    lock yet. See app/routers/grammar.py::_save."""
    words = [f"word{i:02d}" for i in range(16)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda w: ignore({"word": w}), words))
    assert all(code == 200 for code, _ in results), results
    assert api("GET", "/api/grammar/config")[1]["words"] == words


# --- Checking: what is refused before anything is sent ---------------------------


def test_a_check_when_turned_off_is_refused(fresh):
    assert put({"enabled": False})[0] == 200
    code, body = check(["Teh cat is here."])
    assert code == 409
    assert body["detail"] == "Grammar checking is turned off in Settings."


def test_a_check_has_limits(fresh):
    assert check(["x"] * 501)[0] == 422
    assert check(["x" * 30_000, "x" * 30_001])[0] == 422
    assert check([1, 2])[0] == 422
    assert api("POST", "/api/grammar/check", {"segments": "Teh cat"})[0] == 422
    assert api("POST", "/api/grammar/check", {})[0] == 422
    assert check(["Teh cat"], language="english")[0] == 422
    # Right at the limits is fine (and has nothing to check, so no backend).
    code, body = check([" "] * 500)
    assert code == 200 and len(body["matches"]) == 500


def test_nothing_to_check_needs_no_checker(fresh):
    assert check([]) == (200, {"language": {"code": "auto", "name": "", "auto": True},
                               "matches": []})
    code, body = check(["", "  \n "], language="de-DE")
    assert code == 200
    assert body == {"language": {"code": "de-DE", "name": "", "auto": False},
                    "matches": [[], []]}


# --- Checking against a real LanguageTool ---------------------------------------


def live_check(segments, language=None):
    code, body = check(segments, language)
    if code == 503:
        pytest.skip("LanguageTool not running; COMPOSE_PROFILES=grammar make test runs this")
    assert code == 200, body
    return body


def _spelling(matches, offset, length):
    return [m for m in matches if m["type"] == "spelling"
            and (m["offset"], m["length"]) == (offset, length)]


def test_live_a_misspelling_is_found_with_its_fix(fresh):
    body = live_check(["Teh cat is here."], language="en-US")
    assert body["language"] == {"code": "en-US", "name": "English (US)", "auto": False}
    [match] = _spelling(body["matches"][0], 0, 3)
    assert "The" in match["replacements"]
    assert match["rule"].startswith("MORFOLOGIK_RULE")


def test_live_auto_detects_german_and_offsets_are_per_segment(fresh):
    segments = ["Hallo Max,", "ich habe keine zeit."]
    body = live_check(segments)
    assert body["language"]["auto"] is True
    assert body["language"]["code"].startswith("de")
    hits = [m for m in body["matches"][1]
            if segments[1][m["offset"]:m["offset"] + m["length"]] == "zeit"]
    assert hits, body["matches"]
    assert hits[0]["offset"] == 15 and "Zeit" in hits[0]["replacements"]


def test_live_offsets_are_utf16_units(fresh):
    """The emoji is one code point to Python and two units to Java and to the
    browser: the match for "Teh" is at 18, which is where JavaScript's
    String.prototype.indexOf would find it."""
    segment = "I has a apple. 😀 Teh cat is here."
    body = live_check([segment], language="en-US")
    assert _spelling(body["matches"][0], 18, 3)
    # And in a second segment, after one with emoji of its own.
    body = live_check(["😀😀 hello", "Teh cat."], language="en-US")
    assert _spelling(body["matches"][1], 0, 3)


def test_live_a_dictionary_word_is_no_longer_flagged(fresh):
    assert _spelling(live_check(["Teh cat is here."], language="en-US")["matches"][0], 0, 3)
    assert ignore({"word": "teh"})[0] == 200
    body = live_check(["Teh cat is here."], language="en-US")
    assert not _spelling(body["matches"][0], 0, 3), body


def test_live_a_disabled_rule_is_no_longer_reported(fresh):
    body = live_check(["I has a apple."], language="en-US")
    assert any(m["rule"] == "EN_A_VS_AN" for m in body["matches"][0]), body
    assert ignore({"rule": "EN_A_VS_AN"})[0] == 200
    body = live_check(["I has a apple."], language="en-US")
    assert not any(m["rule"] == "EN_A_VS_AN" for m in body["matches"][0]), body


def test_live_an_unknown_language_is_the_checker_refusing(fresh):
    live_check(["Hello."], language="en-US")      # skips when there is no checker
    code, body = check(["Hello."], language="xx")
    assert code == 400
    assert "xx" in body["detail"]


def test_live_languages_are_listed(fresh):
    code, body = api("GET", "/api/grammar/languages")
    if code == 503:
        pytest.skip("LanguageTool not running; COMPOSE_PROFILES=grammar make test runs this")
    assert code == 200, body
    codes = {row["code"] for row in body["languages"]}
    assert {"en-US", "de-DE"} <= codes
    names = [row["name"].casefold() for row in body["languages"]]
    assert names == sorted(names)
