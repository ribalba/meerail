"""Unit coverage for app/grammar.py: settings, offsets, matches, errors, privacy.

Pure unit test: no server, no database, no LanguageTool. The checker is an
httpx.MockTransport standing in through grammar.use_transport, and name
resolution is a monkeypatched socket.getaddrinfo, so nothing here touches the
network. The live half, against a real LanguageTool, is tests/test_grammar.py.

What matters most, and why each part is pinned:

  * offsets, because they are counted in UTF-16 units on both ends and in code
    points by Python, and an off-by-one here is an underline on the wrong word
    rather than an error anybody sees;
  * the privacy guard, because the whole promise of the feature is that a
    draft goes nowhere off this machine unless the operator said it may;
  * the error mapping, because every failure has to reach the composer as a
    sentence it can show, and none of them may carry the draft.
"""

from __future__ import annotations

import logging
import socket
import time
from urllib.parse import parse_qs

import httpx
import pytest

import app.grammar as grammar
from core.config import Settings

LOCAL = "http://127.0.0.1:8010"

# Text that must never appear in an error message or in the log.
SECRET = "the merger closes on friday, tell nobody"


def _settings(**values) -> Settings:
    # _env_file=None keeps a checkout's own .env out of the assertions; the
    # suite runs with MEERAIL_CONFIG= so no meerail.toml is read either.
    return Settings(_env_file=None, **{"grammar_url": LOCAL, **values})


class Checker:
    """A fake LanguageTool: records each request and answers with `handler`."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.handler = lambda request: httpx.Response(200, json=answer())

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def form(self, index: int = -1) -> dict[str, str]:
        parsed = parse_qs(self.requests[index].content.decode("utf-8"), keep_blank_values=True)
        return {key: values[0] for key, values in parsed.items()}


def answer(matches=None, code="en-US", name="English (US)") -> dict:
    return {"language": {"code": code, "name": name}, "matches": matches or []}


def lt_match(offset, length, rule="MORFOLOGIK_RULE_EN_US", issue="misspelling",
             category="TYPOS", message="Possible spelling mistake found.",
             short="Spelling mistake", replacements=("The",)) -> dict:
    """A match in LanguageTool's own v2 shape."""
    return {
        "message": message, "shortMessage": short,
        "replacements": [{"value": r} for r in replacements],
        "offset": offset, "length": length,
        "rule": {"id": rule, "description": f"{rule} description", "issueType": issue,
                 "category": {"id": category, "name": category.title()}},
    }


@pytest.fixture
def configure(monkeypatch):
    """Swap the settings app/grammar.py reads. Returns the setter."""
    def apply(**values):
        settings = _settings(**values)
        monkeypatch.setattr(grammar, "get_settings", lambda: settings)
        return settings
    apply()
    return apply


@pytest.fixture
def checker(configure):
    fake = Checker()
    grammar.use_transport(httpx.MockTransport(fake))
    yield fake
    grammar.use_transport(None)


def config(**values) -> dict:
    return {**grammar.defaults(), "language": "en-US", **values}


# --- meerail.toml ---------------------------------------------------------------


def test_the_grammar_section_of_meerail_toml_reaches_the_settings(tmp_path, monkeypatch):
    path = tmp_path / "meerail.toml"
    path.write_text('[grammar]\nurl = "http://languagetool:8010"\n'
                    'timeout_seconds = 45\nallow_public_hosts = true\n')
    monkeypatch.setenv("MEERAIL_CONFIG", str(path))
    for name in ("GRAMMAR_URL", "GRAMMAR_TIMEOUT_SECONDS", "GRAMMAR_ALLOW_PUBLIC_HOSTS"):
        monkeypatch.delenv(name, raising=False)

    s = Settings(_env_file=None)

    assert (s.grammar_url, s.grammar_timeout_seconds, s.grammar_allow_public_hosts) == (
        "http://languagetool:8010", 45, True)


def test_the_example_grammar_section_is_valid_once_uncommented(tmp_path, monkeypatch):
    """The example file ships the section commented out; what it tells people
    to uncomment has to load."""
    from core.config import EXAMPLE_CONFIG_PATH

    text = EXAMPLE_CONFIG_PATH.read_text()
    block = text[text.index("# --- grammar"):text.index("# --- agent")]
    lines = [line[2:] for line in block.splitlines()
             if line.startswith(("# [grammar]", "# url =", "# timeout_seconds =",
                                 "# allow_public_hosts ="))]
    assert len(lines) == 4, lines
    path = tmp_path / "meerail.toml"
    path.write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("MEERAIL_CONFIG", str(path))
    for name in ("GRAMMAR_URL", "GRAMMAR_TIMEOUT_SECONDS", "GRAMMAR_ALLOW_PUBLIC_HOSTS"):
        monkeypatch.delenv(name, raising=False)

    s = Settings(_env_file=None)

    assert s.grammar_url == "http://languagetool:8010"
    assert s.grammar_timeout_seconds == 20 and s.grammar_allow_public_hosts is False


def test_the_defaults_leave_the_feature_off(monkeypatch):
    for name in ("GRAMMAR_URL", "GRAMMAR_TIMEOUT_SECONDS", "GRAMMAR_ALLOW_PUBLIC_HOSTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEERAIL_CONFIG", "")
    s = Settings(_env_file=None)
    assert (s.grammar_url, s.grammar_timeout_seconds, s.grammar_allow_public_hosts) == (
        "", 20, False)


# --- The settings row -----------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", "not json", "[1, 2]", '"text"', "42"])
def test_an_absent_or_unreadable_row_reads_as_the_defaults(raw):
    assert grammar.parse_stored(raw) == {
        "enabled": True, "language": "auto", "variants": [], "picky": False,
        "words": [], "disabled_rules": [],
    }


def test_a_stored_row_keeps_what_still_validates():
    """A row from another version: one bad field or one bad entry must not cost
    the rest of it, least of all the dictionary."""
    raw = ('{"enabled": false, "language": "english", "picky": "yes", '
           '"words": ["Kubernetes", "two\\nlines", 7], "disabled_rules": ["OK_RULE", "no spaces"],'
           ' "variants": "de-AT", "unknown": 1}')
    assert grammar.parse_stored(raw) == {
        "enabled": False, "language": "auto", "variants": [], "picky": False,
        "words": ["Kubernetes"], "disabled_rules": ["OK_RULE"],
    }


@pytest.mark.parametrize("code", ["auto", "en", "de-DE", "ca-ES-valencia",
                                  "de-DE-x-simple-language", "ast-ES", " en-GB "])
def test_language_codes_that_are_accepted(code):
    assert grammar.validate_language(code) == code.strip()


@pytest.mark.parametrize("code", ["EN", "english", "de_DE", "de-DE\nen-GB", "d", "", "a" * 41,
                                  "de-DE-x-simple-language-DE", 5, None, ["en"]])
def test_language_codes_that_are_refused(code):
    with pytest.raises(grammar.InvalidSetting):
        grammar.validate_language(code)


def test_variants_are_codes_deduplicated_in_order_and_never_auto():
    assert grammar.validate_variants(["de-AT", "en-GB", "de-AT"]) == ["de-AT", "en-GB"]
    with pytest.raises(grammar.InvalidSetting):
        grammar.validate_variants(["auto"])
    with pytest.raises(grammar.InvalidSetting):
        grammar.validate_variants("de-AT")
    with pytest.raises(grammar.InvalidSetting, match="12"):
        grammar.validate_variants([f"de-X{i:02d}" for i in range(13)])


def test_words_are_stripped_deduplicated_ignoring_case_and_sorted():
    words = grammar.validate_words([" zebra ", "Kubernetes", "kubernetes", "apple", "ZEBRA",
                                    "Äpfel"])
    # The first spelling of each is the one kept, and the order ignores case
    # (it is code point order after casefolding, so Ä sorts after z).
    assert words == ["apple", "Kubernetes", "zebra", "Äpfel"]


@pytest.mark.parametrize("word", ["", "   ", "a" * 101, "two\nlines", "tab\there", "nul\x00",
                                  "line\u2028sep", "lone\ud800", 7, None])
def test_words_that_are_refused(word):
    with pytest.raises(grammar.InvalidSetting):
        grammar.validate_word(word)


def test_the_dictionary_has_a_ceiling():
    full = grammar.validate_words([f"w{i}" for i in range(grammar.MAX_WORDS)])
    assert len(full) == grammar.MAX_WORDS
    with pytest.raises(grammar.InvalidSetting, match="10000"):
        grammar.validate_words([f"w{i}" for i in range(grammar.MAX_WORDS + 1)])


@pytest.mark.parametrize("rule", ["MORFOLOGIK_RULE_EN_US", "a.b:c-d", "X" * 120])
def test_rule_ids_that_are_accepted(rule):
    assert grammar.validate_rule(rule) == rule


@pytest.mark.parametrize("rule", ["", "has space", "two,rules", "X" * 121, "semi;colon", 3])
def test_rule_ids_that_are_refused(rule):
    """A comma above all: disabledRules is a comma-joined parameter, and one id
    carrying a comma would switch off a rule nobody asked about."""
    with pytest.raises(grammar.InvalidSetting):
        grammar.validate_rule(rule)


def test_rules_are_deduplicated_in_order_and_capped():
    assert grammar.validate_rules(["B", "A", "B"]) == ["B", "A"]
    with pytest.raises(grammar.InvalidSetting):
        grammar.validate_rules([f"R{i}" for i in range(grammar.MAX_RULES + 1)])


def test_merge_validates_the_whole_and_drops_unknown_keys():
    merged = grammar.merge(grammar.defaults(), {"picky": True, "available": False, "junk": 1})
    assert merged == {**grammar.defaults(), "picky": True}
    with pytest.raises(grammar.InvalidSetting):
        grammar.merge(grammar.defaults(), {"enabled": 1})
    # The router turns this into a 422, and relies on it being a ValueError.
    assert issubclass(grammar.InvalidSetting, ValueError)


def test_dump_round_trips_through_parse_stored():
    value = grammar.merge(grammar.defaults(), {"words": ["Straße", "naïve"], "language": "de-DE"})
    assert grammar.parse_stored(grammar.dump(value)) == value


# --- Segments and UTF-16 offsets ----------------------------------------------


def test_segments_are_joined_with_a_blank_line_and_measured_in_utf16():
    text, spans = grammar.join_segments(["Hi 😀", "", "Teh"])
    assert text == "Hi 😀\n\n\n\nTeh"
    # The emoji is two units, as it is in Java and in the browser.
    assert spans == [(0, 5), (7, 7), (9, 12)]


def test_an_emoji_before_the_match_counts_as_two(checker):
    """Verified on a real LanguageTool: in this sentence the match for "Teh" is
    at offset 18, not the 17 Python's own indexing would give."""
    segment = "I has a apple. 😀 Teh cat is here."
    assert segment.index("Teh") == 17
    checker.handler = lambda r: httpx.Response(200, json=answer([lt_match(18, 3)]))

    result = grammar.check([segment], config())

    [[match]] = result["matches"]
    assert (match["offset"], match["length"]) == (18, 3)
    assert grammar._slice(segment, 18, 3) == "Teh"


def test_offsets_are_relative_to_each_segment(checker):
    segments = ["😀😀", "Teh cat"]      # joined: 4 units, 2 of separator, then "Teh"
    checker.handler = lambda r: httpx.Response(200, json=answer([lt_match(6, 3)]))

    result = grammar.check(segments, config())

    assert result["matches"][0] == []
    assert [(m["offset"], m["length"]) for m in result["matches"][1]] == [(0, 3)]


def test_a_match_that_is_not_inside_one_segment_is_dropped(checker):
    segments = ["one", "two"]         # "one\n\ntwo": separator at 3 and 4
    checker.handler = lambda r: httpx.Response(200, json=answer([
        lt_match(2, 4),                # runs from "one" through the separator
        lt_match(3, 1),                # starts in the separator
        lt_match(4, 2),                # starts in the separator, ends in "two"
        lt_match(5, 3),                # exactly "two"
        lt_match(90, 2),               # past the end of everything
        lt_match(-1, 2),               # nonsense
        {"offset": "5", "length": 3},  # not numbers
        "not a match",
    ]))

    result = grammar.check(segments, config())

    assert result["matches"][0] == []
    assert [(m["offset"], m["length"]) for m in result["matches"][1]] == [(0, 3)]


def test_one_request_carries_the_whole_draft(checker):
    grammar.check(["first paragraph", "second paragraph"], config())

    assert len(checker.requests) == 1
    request = checker.requests[0]
    assert request.method == "POST" and request.url.path == "/v2/check"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert checker.form()["text"] == "first paragraph\n\nsecond paragraph"
    assert checker.form()["language"] == "en-US"


def test_a_lone_surrogate_does_not_break_the_request_or_the_offsets(checker):
    """JSON can carry "\\ud800", and Python holds it, but it cannot be encoded
    for the request. It is sent as U+FFFD, which is the same one unit long."""
    checker.handler = lambda r: httpx.Response(200, json=answer([lt_match(2, 3)]))

    result = grammar.check(["\ud800 Teh"], config())

    assert checker.form()["text"] == "\ufffd Teh"
    assert [(m["offset"], m["length"]) for m in result["matches"][0]] == [(2, 3)]


def test_nothing_to_check_is_answered_without_a_request(checker):
    for segments in ([], [""], ["  ", "\n\t"]):
        result = grammar.check(segments, config(language="auto"))
        assert result == {"language": {"code": "auto", "name": "", "auto": True},
                          "matches": [[] for _ in segments]}
    assert checker.requests == []


# --- Mapping the answer ---------------------------------------------------------


@pytest.mark.parametrize("issue, category, expected", [
    ("misspelling", "TYPOS", "spelling"),       # MORFOLOGIK_RULE_EN_US
    ("misspelling", "MISC", "grammar"),         # EN_A_VS_AN
    ("style", "BRE_STYLE_OXFORD_SPELLING", "style"),   # OXFORD_SPELLING_Z_NOT_S
    ("locale-violation", "TYPOS", "style"),
    ("register", "STYLE", "style"),
    ("grammar", "GRAMMAR", "grammar"),
    ("uncategorized", "CASING", "grammar"),
    ("", "", "grammar"),
])
def test_issue_types(issue, category, expected):
    assert grammar.issue_type(issue, category) == expected


def test_a_match_in_the_shape_the_browser_reads(checker):
    checker.handler = lambda r: httpx.Response(200, json=answer([lt_match(
        4, 3, replacements=("The", "", "Ten", "Tea", "Tech", "Ted", "TeX", "Tee"))]))

    [[match]] = grammar.check(["The Teh"], config())["matches"]

    assert match == {
        "offset": 4, "length": 3,
        "message": "Possible spelling mistake found.", "short": "Spelling mistake",
        # The first six, and "" kept: it means "delete this".
        "replacements": ["The", "", "Ten", "Tea", "Tech", "Ted"],
        "rule": "MORFOLOGIK_RULE_EN_US",
        "rule_description": "MORFOLOGIK_RULE_EN_US description",
        "category": "TYPOS", "type": "spelling",
    }


def test_suggestion_markup_is_rendered_as_quotes(checker):
    """LingoTweaker leaves <suggestion> raw where Java LanguageTool has already
    rendered it; the browser would otherwise show the tags."""
    checker.handler = lambda r: httpx.Response(200, json=answer([lt_match(
        0, 1, rule="EN_A_VS_AN", category="MISC",
        message="Use <suggestion>an</suggestion> instead of 'a', or <suggestion>the</suggestion>.",
        short="Use <suggestion>an</suggestion>")]))

    [[match]] = grammar.check(["a apple"], config())["matches"]

    assert match["message"] == "Use \u201can\u201d instead of 'a', or \u201cthe\u201d."
    assert match["short"] == "Use \u201can\u201d"


def test_the_detected_language_is_reported(checker):
    checker.handler = lambda r: httpx.Response(200, json=answer(code="de-DE", name="German (Germany)"))

    result = grammar.check(["Hallo Max,", "danke!"], config(language="auto"))

    assert result["language"] == {"code": "de-DE", "name": "German (Germany)", "auto": True}


def test_a_missing_language_object_falls_back_to_the_request(checker):
    checker.handler = lambda r: httpx.Response(200, json={"matches": []})
    assert grammar.check(["Hello"], config())["language"] == {"code": "en-US", "name": "",
                                                              "auto": False}


def test_dictionary_words_silence_spelling_matches_ignoring_case(checker):
    segment = "Teh kubectl said teh"
    checker.handler = lambda r: httpx.Response(200, json=answer([
        lt_match(0, 3),                                   # "Teh": in the dictionary
        lt_match(4, 7),                                   # "kubectl": in the dictionary
        lt_match(17, 3),                                  # "teh": in the dictionary
        lt_match(0, 3, rule="UPPERCASE_X", issue="grammar", category="GRAMMAR"),
    ]))

    result = grammar.check([segment], config(words=["TEH", "Kubectl"]))

    # Only spelling is silenced: a grammar match on the same word is about
    # something else and stays.
    assert [(m["offset"], m["rule"]) for m in result["matches"][0]] == [(0, "UPPERCASE_X")]


def test_disabled_rules_are_sent_and_filtered_as_well(checker):
    checker.handler = lambda r: httpx.Response(200, json=answer([
        lt_match(0, 3, rule="OFF_RULE", issue="grammar", category="GRAMMAR"),
        lt_match(4, 3),
    ]))

    result = grammar.check(["abc def"], config(disabled_rules=["OFF_RULE", "OTHER"]))

    assert checker.form()["disabledRules"] == "OFF_RULE,OTHER"
    # Filtered here too, for a checker that ignores the parameter.
    assert [m["rule"] for m in result["matches"][0]] == ["MORFOLOGIK_RULE_EN_US"]


def test_preferred_variants_go_only_with_auto(checker):
    grammar.check(["Hello"], config(language="auto", variants=["en-GB", "de-AT"]))
    assert checker.form()["preferredVariants"] == "en-GB,de-AT"
    assert checker.form()["language"] == "auto"

    # Both LanguageTool and LingoTweaker answer 400 to the pair.
    grammar.check(["Hello"], config(language="en-US", variants=["en-GB"]))
    assert "preferredVariants" not in checker.form()

    # And a per-check language counts, not the stored one.
    grammar.check(["Hello"], config(language="auto", variants=["en-GB"]), language="de-DE")
    assert "preferredVariants" not in checker.form()
    assert checker.form()["language"] == "de-DE"

    grammar.check(["Hello"], config(language="auto"))
    assert "preferredVariants" not in checker.form()


def test_picky_asks_for_the_picky_level(checker):
    grammar.check(["Hello"], config(picky=True))
    assert checker.form()["level"] == "picky"
    grammar.check(["Hello"], config(picky=False))
    assert "level" not in checker.form()
    assert "disabledRules" not in checker.form()


def test_a_per_check_language_is_validated(checker):
    with pytest.raises(grammar.InvalidSetting):
        grammar.check(["Hello"], config(), language="english")
    assert checker.requests == []


# --- Configuration ----------------------------------------------------------------


def test_no_url_is_not_configured(configure, checker):
    configure(grammar_url="")
    assert grammar.available() is False
    with pytest.raises(grammar.NotConfigured, match=r"\[grammar\] url"):
        grammar.check(["Hello"], config())
    with pytest.raises(grammar.NotConfigured):
        grammar.languages()
    assert checker.requests == []


@pytest.mark.parametrize("url", ["languagetool:8010", "ftp://lt:21", "http://", "http://lt:port"])
def test_a_url_that_is_not_one_says_so(configure, checker, url):
    configure(grammar_url=url)
    with pytest.raises(grammar.NotConfigured, match="http:// or https://"):
        grammar.check(["Hello"], config())
    assert checker.requests == []


@pytest.mark.parametrize("url", [LOCAL, LOCAL + "/", LOCAL + "/v2", LOCAL + "/v2/"])
def test_the_api_root_may_be_given_with_or_without_v2(configure, checker, url):
    configure(grammar_url=url)
    grammar.check(["Hello"], config())
    assert str(checker.requests[-1].url) == LOCAL + "/v2/check"


# --- Errors -----------------------------------------------------------------------


def _raise(exc_type, message="boom"):
    def handler(request):
        raise exc_type(message, request=request)
    return handler


@pytest.mark.parametrize("exc_type", [httpx.ConnectError, httpx.ConnectTimeout,
                                      httpx.RemoteProtocolError, httpx.ReadError])
def test_a_checker_that_cannot_be_reached(checker, exc_type):
    checker.handler = _raise(exc_type)
    with pytest.raises(grammar.Unreachable) as info:
        grammar.check([SECRET], config())
    assert str(info.value) == grammar.UNREACHABLE


def test_a_checker_that_does_not_answer_in_time(checker, configure):
    configure(grammar_timeout_seconds=7)
    checker.handler = _raise(httpx.ReadTimeout)
    with pytest.raises(grammar.Unreachable, match="7 seconds"):
        grammar.check([SECRET], config())


def test_java_languagetool_refusal_is_passed_on_trimmed(checker):
    codes = ", ".join(f"x{i}-XX" for i in range(200))
    checker.handler = lambda r: httpx.Response(
        400, text=f"Error: 'xx' is not a language code known to LanguageTool. "
                  f"Supported language codes are: {codes}.")
    with pytest.raises(grammar.Refused) as info:
        grammar.check([SECRET], config())
    detail = str(info.value)
    assert detail.startswith("'xx' is not a language code known to LanguageTool.")
    assert len(detail) <= grammar.MAX_DETAIL_CHARS


@pytest.mark.parametrize("status", [400, 413, 501])
def test_lingotweaker_json_refusal_is_passed_on(checker, status):
    """501 is what LingoTweaker answers for a language its build lacks, and it
    is a refusal like a 4xx rather than the server failing."""
    checker.handler = lambda r: httpx.Response(
        status, json={"error": {"message": "xx is not a language code known to LingoTweaker."}})
    with pytest.raises(grammar.Refused, match="^xx is not a language code known to LingoTweaker.$"):
        grammar.check([SECRET], config())


def test_a_refusal_with_nothing_to_say_still_says_something(checker):
    checker.handler = lambda r: httpx.Response(400, text="   ")
    with pytest.raises(grammar.Refused, match="HTTP 400"):
        grammar.check([SECRET], config())


@pytest.mark.parametrize("response", [
    httpx.Response(500, text="java.lang.NullPointerException"),
    httpx.Response(503, text="overloaded"),
    httpx.Response(302, headers={"location": "https://elsewhere.example/"}),
    httpx.Response(200, text="<html>not the checker</html>"),
    httpx.Response(200, json=[]),
    httpx.Response(200, json={"matches": "none"}),
    httpx.Response(200, json={"language": {"code": "en-US"}}),
])
def test_an_answer_that_cannot_be_read(checker, response):
    checker.handler = lambda r: response
    with pytest.raises(grammar.BadAnswer) as info:
        grammar.check([SECRET], config())
    assert str(info.value) == grammar.BAD_ANSWER
    # And the redirect was not followed: a checker that answers with a 3xx
    # does not get to send the draft somewhere else.
    assert len(checker.requests) == 1


def test_no_error_and_no_log_line_carries_the_draft(checker, caplog):
    caplog.set_level(logging.DEBUG, logger="app.grammar")
    failures = [
        _raise(httpx.ConnectError, SECRET), _raise(httpx.ReadTimeout, SECRET),
        lambda r: httpx.Response(500, text=SECRET),
        lambda r: httpx.Response(200, text=SECRET),
    ]
    for handler in failures:
        checker.handler = handler
        with pytest.raises(grammar.GrammarError) as info:
            grammar.check([SECRET], config())
        assert SECRET not in str(info.value)
    assert caplog.records, "the failures should have been logged"
    assert SECRET not in caplog.text


# --- The privacy guard ----------------------------------------------------------


def _resolver(monkeypatch, *addresses, error=None):
    """Make every name resolve to `addresses` (or fail with `error`). Returns
    the list of names looked up, to count lookups by."""
    looked_up: list[str] = []

    def fake(host, port, *args, **kwargs):
        looked_up.append(host)
        if error is not None:
            raise error
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 (a, 0)) for a in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return looked_up


def test_a_checker_on_a_public_address_is_refused_before_anything_is_sent(
        checker, configure, monkeypatch):
    configure(grammar_url="https://api.languagetool.example")
    _resolver(monkeypatch, "93.184.216.34")

    with pytest.raises(grammar.PublicHost) as info:
        grammar.check([SECRET], config())

    assert str(info.value) == grammar.PUBLIC_REFUSAL
    assert "allow_public_hosts" in str(info.value)
    assert checker.requests == []
    with pytest.raises(grammar.PublicHost):
        grammar.languages()
    assert checker.requests == []


def test_one_public_address_among_private_ones_is_enough_to_refuse(checker, configure, monkeypatch):
    configure(grammar_url="http://lt.example:8010")
    _resolver(monkeypatch, "10.0.0.5", "2606:2800:220:1::1")
    with pytest.raises(grammar.PublicHost):
        grammar.check([SECRET], config())
    assert checker.requests == []


@pytest.mark.parametrize("address", ["172.18.0.4", "10.1.2.3", "192.168.1.20", "127.0.0.1",
                                     "::1", "fd00::5", "::ffff:10.0.0.1"])
def test_a_checker_on_this_machine_or_network_is_used(checker, configure, monkeypatch, address):
    configure(grammar_url="http://languagetool:8010")
    _resolver(monkeypatch, address)
    grammar.check(["Hello"], config())
    assert len(checker.requests) == 1


def test_allow_public_hosts_lets_a_public_checker_through(checker, configure, monkeypatch):
    configure(grammar_url="https://lt.example", grammar_allow_public_hosts=True)
    looked_up = _resolver(monkeypatch, "93.184.216.34")
    grammar.check(["Hello"], config())
    assert len(checker.requests) == 1
    # Nothing is resolved at all when there is nothing to refuse.
    assert looked_up == []


def test_a_name_that_does_not_resolve_is_unreachable_not_refused(checker, configure, monkeypatch):
    """In a compose stack with the `grammar` profile off, this is exactly what
    languagetool:8010 does, and the helpful sentence is the container one."""
    configure(grammar_url="http://languagetool:8010")
    looked_up = _resolver(monkeypatch, error=socket.gaierror(-2, "Name or service not known"))

    for _ in range(3):
        with pytest.raises(grammar.Unreachable) as info:
            grammar.check(["Hello"], config())
        assert str(info.value) == grammar.UNREACHABLE
    # Remembered briefly, because the failing lookup is the slow part...
    assert looked_up == ["languagetool"]
    assert checker.requests == []

    # ...and only briefly: once that has run out, a checker that has come up
    # in the meantime is found and used.
    base = "http://languagetool:8010"
    expires, verdict = grammar._guard_cache[base]
    assert verdict == "unresolved"
    assert 0 < expires - time.monotonic() <= grammar.UNRESOLVED_TTL
    grammar._guard_cache[base] = (0.0, verdict)
    _resolver(monkeypatch, "172.18.0.4")
    grammar.check(["Hello"], config())
    assert len(checker.requests) == 1


def test_the_verdict_is_cached_so_a_keystroke_costs_no_lookup(checker, configure, monkeypatch):
    configure(grammar_url="http://languagetool:8010")
    looked_up = _resolver(monkeypatch, "172.18.0.4")
    for _ in range(3):
        grammar.check(["Hello"], config())
    assert looked_up == ["languagetool"]
    assert len(checker.requests) == 3


def test_a_refusal_is_cached_too(checker, configure, monkeypatch):
    configure(grammar_url="https://lt.example")
    looked_up = _resolver(monkeypatch, "93.184.216.34")
    for _ in range(3):
        with pytest.raises(grammar.PublicHost):
            grammar.check(["Hello"], config())
    assert looked_up == ["lt.example"]


def test_empty_segments_need_no_verdict(checker, configure, monkeypatch):
    configure(grammar_url="https://lt.example")
    looked_up = _resolver(monkeypatch, "93.184.216.34")
    assert grammar.check(["", " "], config())["matches"] == [[], []]
    assert looked_up == [] and checker.requests == []


# --- The language list ------------------------------------------------------------


JAVA_LANGUAGES = [
    {"name": "German (Germany)", "code": "de", "longCode": "de-DE"},
    {"name": "English (US)", "code": "en", "longCode": "en-US"},
    {"name": "Arabic", "code": "ar", "longCode": "ar"},
    {"name": "Simple German", "code": "de-DE-x-simple-language",
     "longCode": "de-DE-x-simple-language"},
    {"name": "Simple German", "code": "de-DE-x-simple-language",
     "longCode": "de-DE-x-simple-language-DE"},
    {"name": "English (US)", "code": "en", "longCode": "en-US"},
    {"name": "nameless"},
    "junk",
]


def test_languages_are_listed_by_name_with_their_long_codes(checker):
    checker.handler = lambda r: httpx.Response(200, json=JAVA_LANGUAGES)

    assert grammar.languages() == [
        {"code": "ar", "name": "Arabic"},
        {"code": "en-US", "name": "English (US)"},
        {"code": "de-DE", "name": "German (Germany)"},
        # Only the entry whose code settings would accept.
        {"code": "de-DE-x-simple-language", "name": "Simple German"},
    ]
    request = checker.requests[-1]
    assert request.method == "GET" and request.url.path == "/v2/languages"


def test_languages_are_cached(checker):
    checker.handler = lambda r: httpx.Response(200, json=JAVA_LANGUAGES)
    first = grammar.languages()
    first.append({"code": "xx", "name": "mutated by a caller"})
    assert grammar.languages() == first[:-1]
    assert len(checker.requests) == 1


def test_a_language_list_that_is_not_a_list(checker):
    checker.handler = lambda r: httpx.Response(200, json={"languages": []})
    with pytest.raises(grammar.BadAnswer):
        grammar.languages()
    # A failure is not cached.
    checker.handler = lambda r: httpx.Response(200, json=JAVA_LANGUAGES[:1])
    assert grammar.languages() == [{"code": "de-DE", "name": "German (Germany)"}]


def test_lingotweaker_language_shape(checker):
    checker.handler = lambda r: httpx.Response(200, json=[
        {"code": "en", "longCode": "en-US", "name": "English (US)"},
        {"code": "es", "longCode": "es", "name": "Spanish"},
    ])
    assert grammar.languages() == [{"code": "en-US", "name": "English (US)"},
                                   {"code": "es", "name": "Spanish"}]


def test_requests_neither_follow_redirects_nor_use_a_proxy(checker):
    """Checked on the client itself, not only through a 3xx answer above. A
    proxy from HTTP_PROXY would carry the draft off the machine past the guard,
    which only ever looks at the configured host."""
    client = grammar._http()
    assert client.follow_redirects is False
    assert client.trust_env is False
