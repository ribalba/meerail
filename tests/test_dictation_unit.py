"""Unit coverage for app/dictation.py: settings, the prompt, segments, errors, privacy.

Pure unit test, with no server, no database and no Whisper. The service is an
httpx.MockTransport standing in through dictation.use_transport, and name
resolution is a monkeypatched socket.getaddrinfo, so nothing here touches the
network. The live half, against a real Whisper, is tests/test_dictation.py.

What matters most, and why each part is pinned:

  * the exact request, because the service reads everything but the audio from
    the query string and silently ignores a parameter it does not know, so a
    misspelt one is a feature that quietly does nothing
  * the segment filter, because a credit Whisper invented on silence lands in
    somebody's mail, and a real sentence dropped by mistake is worse
  * the privacy guard, because the whole promise of the feature is that a
    recording goes nowhere off this machine unless the operator said it may
  * the logs, because the prompt carries the end of the draft in a URL, and
    httpx logs URLs
"""

from __future__ import annotations

import json
import logging
import socket
import time

import httpx
import pytest

import app.dictation as dictation
from core.config import Settings

LOCAL = "http://127.0.0.1:9000"

# Text that must never appear in an error message or in the log.
SECRET = "the merger closes on friday, tell nobody"
# What the service "heard". It is the user's words and must not be logged either.
SPOKEN = "please wire the money to account four two seven"
# A recording. The bytes after the header are a marker the log check looks for.
AUDIO = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"AUDIO-MARKER-9f3c" * 8

WORDS = ("meerail", "Kubernetes")


def _settings(**values) -> Settings:
    # _env_file=None keeps a checkout's own .env out of the assertions. The
    # suite runs with MEERAIL_CONFIG= so no meerail.toml is read either.
    return Settings(_env_file=None, **{"dictation_url": LOCAL, **values})


class Service:
    """A fake whisper-asr-webservice that records each request and answers with
    `handler`."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.handler = lambda request: reply(answer([segment("Hello there.")]))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        # Read the multipart body now, while the request is still open.
        request.read()
        self.requests.append(request)
        return self.handler(request)

    def params(self, index: int = -1) -> dict[str, str]:
        return dict(self.requests[index].url.params)


def answer(segments=None, language="en") -> dict:
    """The service's json output for faster-whisper, as WriteJSON dumps it."""
    segments = segments or []
    return {"text": "".join(s.get("text", "") for s in segments if isinstance(s, dict)),
            "segments": segments, "language": language}


def segment(text, no_speech_prob=0.02, avg_logprob=-0.25, compression_ratio=1.3, **extra) -> dict:
    """One segment in faster-whisper's own shape (dataclasses.asdict(Segment))."""
    seg = {"id": 1, "seek": 0, "start": 0.0, "end": 2.0, "text": text, "tokens": [50364],
           "avg_logprob": avg_logprob, "compression_ratio": compression_ratio,
           "no_speech_prob": no_speech_prob, "words": None, "temperature": 0.0}
    seg.update(extra)
    return {k: v for k, v in seg.items() if v is not _MISSING}


_MISSING = object()


def reply(body, status=200) -> httpx.Response:
    # The service declares even its JSON answer as text/plain.
    return httpx.Response(status, content=json.dumps(body).encode("utf-8"),
                          headers={"content-type": "text/plain; charset=utf-8"})


@pytest.fixture
def configure(monkeypatch):
    """Swap the settings app/dictation.py reads. Returns the setter."""
    def apply(**values):
        settings = _settings(**values)
        monkeypatch.setattr(dictation, "get_settings", lambda: settings)
        return settings
    apply()
    return apply


@pytest.fixture
def service(configure):
    fake = Service()
    dictation.use_transport(httpx.MockTransport(fake))
    yield fake
    dictation.use_transport(None)


def config(**values) -> dict:
    return {**dictation.defaults(), "enabled": True, **values}


# --- meerail.toml ---------------------------------------------------------------

_ENV = ("DICTATION_URL", "DICTATION_TIMEOUT_SECONDS", "DICTATION_ALLOW_PUBLIC_HOSTS")


def test_the_dictation_section_of_meerail_toml_reaches_the_settings(tmp_path, monkeypatch):
    path = tmp_path / "meerail.toml"
    path.write_text('[dictation]\nurl = "http://whisper:9000"\n'
                    'timeout_seconds = 90\nallow_public_hosts = true\n')
    monkeypatch.setenv("MEERAIL_CONFIG", str(path))
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)

    s = Settings(_env_file=None)

    assert (s.dictation_url, s.dictation_timeout_seconds, s.dictation_allow_public_hosts) == (
        "http://whisper:9000", 90, True)


def test_the_example_dictation_section_is_valid_once_uncommented(tmp_path, monkeypatch):
    """The example file ships the section commented out. What it tells people
    to uncomment has to load."""
    from core.config import EXAMPLE_CONFIG_PATH

    text = EXAMPLE_CONFIG_PATH.read_text()
    # Up to the next section's heading, whichever section follows.
    start = text.index("# --- dictation")
    block = text[start:text.index("\n# --- ", start + 1)]
    lines = [line[2:] for line in block.splitlines()
             if line.startswith(("# [dictation]", "# url =", "# timeout_seconds =",
                                 "# allow_public_hosts ="))]
    assert len(lines) == 4, lines
    path = tmp_path / "meerail.toml"
    path.write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("MEERAIL_CONFIG", str(path))
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)

    s = Settings(_env_file=None)

    assert s.dictation_url == "http://whisper:9000"
    assert s.dictation_timeout_seconds == 60 and s.dictation_allow_public_hosts is False


def test_the_defaults_leave_the_feature_off(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEERAIL_CONFIG", "")
    s = Settings(_env_file=None)
    assert (s.dictation_url, s.dictation_timeout_seconds, s.dictation_allow_public_hosts) == (
        "", 60, False)


# --- The settings row -----------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", "not json", "[1, 2]", '"text"', "42"])
def test_an_absent_or_unreadable_row_reads_as_the_defaults(raw):
    assert dictation.parse_stored(raw) == {"enabled": False, "language": "auto", "words": []}


def test_the_defaults_are_off():
    """The microphone is the user's to switch on."""
    assert dictation.DEFAULTS["enabled"] is False
    fresh = dictation.defaults()
    fresh["words"].append("x")
    assert dictation.DEFAULTS["words"] == []


def test_a_stored_row_keeps_what_still_validates():
    """A row from another version. One bad field or one bad entry must not cost
    the rest of it."""
    raw = ('{"enabled": 1, "language": "EN", '
           '"words": ["meerail", "two\\nlines", 7, "Meerail", "Kubernetes"], "unknown": 1}')
    assert dictation.parse_stored(raw) == {"enabled": False, "language": "auto",
                                           "words": ["meerail", "Kubernetes"]}
    raw = '{"enabled": true, "language": " de ", "words": "meerail"}'
    assert dictation.parse_stored(raw) == {"enabled": True, "language": "de", "words": []}


def test_a_stored_row_over_the_limits_keeps_its_first_words():
    many = [chr(0x4E00 + i) for i in range(dictation.MAX_WORDS + 20)]
    stored = dictation.parse_stored(json.dumps({"words": many}))
    assert stored["words"] == many[:dictation.MAX_WORDS]

    long = [f"{chr(97 + i)}{'x' * 49}" for i in range(12)]          # 12 x 50 chars
    stored = dictation.parse_stored(json.dumps({"words": long}))
    # 7 words are 7 * 50 + 6 * 2 = 362 characters, and an eighth would be 414.
    assert stored["words"] == long[:7]


def test_whisper_languages_are_whisper_s_own_table():
    langs = dictation.WHISPER_LANGUAGES
    assert isinstance(langs, tuple)
    assert len(langs) == 100 == len(set(langs))
    assert langs[:6] == ("en", "zh", "de", "es", "ru", "ko")
    assert langs[-5:] == ("ha", "ba", "jw", "su", "yue")
    assert "haw" in langs and "auto" not in langs
    assert all(code == code.strip().lower() for code in langs)


@pytest.mark.parametrize("code", ["auto", "en", "de", "haw", "yue", "jw", " de ", "fr\n"])
def test_language_codes_that_are_accepted(code):
    assert dictation.validate_language(code) == code.strip()


@pytest.mark.parametrize("code", ["EN", "De", "Auto", "en-US", "de-DE", "english", "xx", "",
                                  "   ", "e", 5, None, True, ["en"]])
def test_language_codes_that_are_refused(code):
    with pytest.raises(dictation.InvalidSetting):
        dictation.validate_language(code)


def test_a_refused_language_says_what_to_send_instead():
    with pytest.raises(dictation.InvalidSetting, match="en or de, or auto"):
        dictation.validate_language("english")
    with pytest.raises(dictation.InvalidSetting) as info:
        dictation.validate_language("x" * 500)
    assert len(str(info.value)) < 150


@pytest.mark.parametrize("value", [1, 0, "true", "false", None, [], "yes"])
def test_enabled_is_strictly_a_boolean(value):
    with pytest.raises(dictation.InvalidSetting, match="enabled must be true or false"):
        dictation.validate({"enabled": value})


def test_words_are_stripped_and_deduplicated_ignoring_case_in_the_order_given():
    words = dictation.validate_words([" zebra ", "Kubernetes", "kubernetes", "apple", "ZEBRA",
                                      "Äpfel", "äpfel"])
    # The first spelling of each is kept, and the order is the user's, not sorted.
    assert words == ["zebra", "Kubernetes", "apple", "Äpfel"]


@pytest.mark.parametrize("word", ["", "   ", "a" * 61, "two\nlines", "tab\there", "nul\x00",
                                  "line sep", "para sep", "lone\ud800", 7, None])
def test_words_that_are_refused(word):
    with pytest.raises(dictation.InvalidSetting):
        dictation.validate_word(word)


def test_a_word_may_be_sixty_characters_and_have_spaces_inside():
    assert dictation.validate_word(" " + "a" * 60 + " ") == "a" * 60
    assert dictation.validate_word("Green Coding Solutions") == "Green Coding Solutions"


def test_each_word_limit_has_its_own_sentence():
    with pytest.raises(dictation.InvalidSetting, match="between 1 and 60 characters"):
        dictation.validate_word("a" * 61)
    with pytest.raises(dictation.InvalidSetting, match="line breaks"):
        dictation.validate_word("two\nlines")
    with pytest.raises(dictation.InvalidSetting, match="must be text"):
        dictation.validate_word(7)
    with pytest.raises(dictation.InvalidSetting, match="must be a list"):
        dictation.validate_words("meerail")


def test_the_vocabulary_holds_at_most_a_hundred_words():
    # Single characters, so a hundred of them stay under the character limit.
    full = [chr(0x4E00 + i) for i in range(dictation.MAX_WORDS)]
    assert dictation.validate_words(full) == full
    with pytest.raises(dictation.InvalidSetting, match="at most 100 words"):
        dictation.validate_words(full + ["one more"])
    # Counted after deduplication, so a repeat does not use up a place.
    assert dictation.validate_words(full + [full[0]]) == full


def test_the_vocabulary_holds_at_most_four_hundred_characters():
    # Six words of 55 characters and one of 58, with six ", " between them, are
    # exactly 400.
    words = [f"{chr(97 + i)}{'x' * 54}" for i in range(6)] + ["g" + "x" * 57]
    assert len(", ".join(words)) == dictation.MAX_VOCABULARY_CHARS
    assert dictation.validate_words(words) == words
    with pytest.raises(dictation.InvalidSetting, match="at most 400 characters"):
        dictation.validate_words(words[:-1] + ["g" + "x" * 58])


def test_merge_validates_the_whole_and_drops_unknown_keys():
    merged = dictation.merge(dictation.defaults(),
                             {"language": "de", "available": False, "languages": [], "junk": 1})
    assert merged == {**dictation.defaults(), "language": "de"}
    with pytest.raises(dictation.InvalidSetting):
        dictation.merge(dictation.defaults(), {"enabled": 1})
    # The router turns this into a 422, and relies on it being a ValueError.
    assert issubclass(dictation.InvalidSetting, ValueError)


def test_dump_round_trips_through_parse_stored():
    value = dictation.merge(dictation.defaults(),
                            {"enabled": True, "words": ["Straße", "naïve", "Zoë"], "language": "de"})
    assert dictation.parse_stored(dictation.dump(value)) == value


# --- The prompt -----------------------------------------------------------------


def test_the_prompt_from_the_vocabulary_alone():
    assert dictation.build_prompt(["meerail", "Kubernetes"], None) == "meerail, Kubernetes."
    assert dictation.build_prompt(["meerail"], "  \n ") == "meerail."


def test_the_prompt_from_the_draft_alone_with_its_whitespace_collapsed():
    assert dictation.build_prompt([], "Hallo Max,\n\n  danke für\tdie") == "Hallo Max, danke für die"


def test_the_prompt_puts_the_vocabulary_first_and_the_draft_last():
    """The draft's tail has to be the end of the prompt, because Whisper
    continues from the end."""
    assert dictation.build_prompt(list(WORDS), "Hallo Max,") == "meerail, Kubernetes. Hallo Max,"


def test_no_vocabulary_and_no_draft_is_no_prompt():
    assert dictation.build_prompt([], None) == ""
    assert dictation.build_prompt([], "") == ""
    assert dictation.build_prompt([], " \n\t ") == ""


def test_a_long_draft_is_cut_to_its_tail_at_a_word_boundary():
    draft = "abcdefghij " * 40              # 439 characters once collapsed
    tail = dictation.build_prompt([], draft)
    # The last 300 would start with "hij", the end of a word cut in half. That
    # piece goes, and what is left starts on a whole word.
    assert tail == " ".join(["abcdefghij"] * 27)
    assert len(tail) <= dictation.CONTEXT_CHARS


def test_a_cut_that_falls_between_two_words_keeps_the_first_one():
    whole = "abcd " * 59 + "abcde"          # exactly 300 characters
    assert len(whole) == dictation.CONTEXT_CHARS
    assert dictation.build_prompt([], "front " + whole) == whole


def test_a_long_draft_without_spaces_is_still_cut():
    assert dictation.build_prompt([], "x" * 500) == "x" * dictation.CONTEXT_CHARS


def test_the_prompt_is_bounded_by_the_two_caps():
    # The largest vocabulary there can be, and a draft far longer than is used.
    words = dictation.validate_words([f"{chr(97 + i)}{'x' * 54}" for i in range(6)]
                                     + ["g" + "x" * 57])
    prompt = dictation.build_prompt(words, "word " * 2000)
    assert len(prompt) <= dictation.MAX_VOCABULARY_CHARS + 2 + dictation.CONTEXT_CHARS


# --- The request ----------------------------------------------------------------


def test_the_request_the_service_gets(service):
    result = dictation.transcribe(AUDIO, config(language="de", words=list(WORDS)),
                                  context="Hallo Max,\n\nich wollte")

    assert result == {"text": "Hello there.", "language": "en"}
    assert len(service.requests) == 1
    request = service.requests[0]
    assert request.method == "POST"
    assert str(request.url).startswith(LOCAL + "/asr?")
    assert service.params() == {
        "task": "transcribe", "output": "json", "encode": "true", "vad_filter": "true",
        "language": "de", "initial_prompt": "meerail, Kubernetes. Hallo Max, ich wollte",
    }
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    body = request.content
    assert b'name="audio_file"; filename="dictation.wav"' in body
    assert b"Content-Type: audio/wav" in body
    assert AUDIO in body
    # Nothing but the file goes in the body. The service reads its options from
    # the query string only.
    assert body.count(b"Content-Disposition") == 1


def test_auto_sends_no_language_and_an_empty_prompt_is_not_sent(service):
    dictation.transcribe(AUDIO, config(language="auto"))
    assert service.params() == {"task": "transcribe", "output": "json", "encode": "true",
                                "vad_filter": "true"}
    dictation.transcribe(AUDIO, config(language="auto"), context="   \n")
    assert "initial_prompt" not in service.params()


def test_a_per_piece_language_overrides_the_stored_one(service):
    dictation.transcribe(AUDIO, config(language="de"), language="auto")
    assert "language" not in service.params()
    dictation.transcribe(AUDIO, config(language="auto"), language=" fr ")
    assert service.params()["language"] == "fr"


def test_a_per_piece_language_is_validated(service):
    with pytest.raises(dictation.InvalidSetting):
        dictation.transcribe(AUDIO, config(), language="english")
    assert service.requests == []


@pytest.mark.parametrize("url", [LOCAL, LOCAL + "/", LOCAL + "//"])
def test_a_trailing_slash_on_the_url_is_dropped(configure, service, url):
    configure(dictation_url=url)
    dictation.transcribe(AUDIO, config())
    assert service.requests[-1].url.path == "/asr"


def test_no_url_is_not_configured(configure, service):
    configure(dictation_url="  ")
    assert dictation.available() is False
    with pytest.raises(dictation.NotConfigured, match=r"\[dictation\] url"):
        dictation.transcribe(AUDIO, config())
    assert service.requests == []


@pytest.mark.parametrize("url", ["whisper:9000", "ftp://whisper:21", "http://", "http://whisper:port"])
def test_a_url_that_is_not_one_says_so(configure, service, url):
    configure(dictation_url=url)
    assert dictation.available() is True
    with pytest.raises(dictation.NotConfigured, match="http:// or https://"):
        dictation.transcribe(AUDIO, config())
    assert service.requests == []


def test_the_read_timeout_is_the_setting_and_connect_is_three_seconds(configure):
    configure(dictation_timeout_seconds=45)
    timeout = dictation._timeout()
    assert (timeout.read, timeout.connect) == (45.0, dictation.CONNECT_TIMEOUT) == (45.0, 3.0)


# --- Segments -------------------------------------------------------------------


@pytest.mark.parametrize("seg", [
    segment("Hello."),
    # Only one half of the no-speech rule holds, so it is speech.
    segment("Quietly said.", no_speech_prob=0.9, avg_logprob=-0.5),
    segment("Kubernetes Zoë.", no_speech_prob=0.3, avg_logprob=-1.6),
    # Right at the thresholds is still kept. The rule is strictly past them.
    segment("Edge.", no_speech_prob=0.6, avg_logprob=-1.5),
    segment("Edge.", no_speech_prob=0.9, avg_logprob=-1.0),
    segment("Edge.", compression_ratio=2.4),
    # A score that is missing or not a number never drops a segment.
    {"text": "No scores at all."},
    segment("Strings.", no_speech_prob="0.9", avg_logprob="-1.5", compression_ratio="9"),
    segment("Booleans.", no_speech_prob=True, avg_logprob=-5.0),
    segment("Null.", no_speech_prob=None, avg_logprob=None, compression_ratio=None),
])
def test_segments_that_are_kept(seg):
    assert dictation.keep_segment(seg) is True


@pytest.mark.parametrize("seg", [
    segment(""),
    segment("   "),
    segment("Silence.", no_speech_prob=0.61, avg_logprob=-1.01),
    segment("Silence.", no_speech_prob=0.95, avg_logprob=-2.3),
    segment("la la la la la la la la", compression_ratio=2.41),
    segment("Thank you for watching!"),
    {"text": 5},
    {"no_speech_prob": 0.1},
    "Hello.",
    None,
])
def test_segments_that_are_dropped(seg):
    assert dictation.keep_segment(seg) is False


@pytest.mark.parametrize("text", [
    "Thank you for watching!",
    " Thanks for watching.",
    "Please subscribe.",
    "PLEASE SUBSCRIBE",
    "Vielen Dank fürs Zuschauen!",
    "Danke fürs Zuschauen.",
    "Untertitel im Auftrag des ZDF",
    "Untertitel im Auftrag des ZDF für funk.",
    "Untertitel der Amara.org-Community",
    "Untertitel von Stephanie Geiges",
    "Subtitles by the Amara.org community",
    # The patterns catch a credit with a year or a different broadcaster in it.
    "Untertitel im Auftrag des ZDF, 2017",
    "Untertitelung des ZDF, 2020",
    "Untertitelung. WDR 2019",
    "Subtitles by Amara.org",
    "Subtitle: ARD",
    "Copyright WDR 2021",
    "Copyright SWR 2019",
])
def test_known_inventions_are_recognised(text):
    assert dictation.is_hallucination(text) is True


@pytest.mark.parametrize("text", [
    # Things a person could really say in a mail stay, even though Whisper
    # invents some of them too.
    "Vielen Dank",
    "Vielen Dank!",
    "Bis zum nächsten Mal.",
    "Thank you.",
    "Danke.",
    "Thanks for watching the kids yesterday.",
    "Ich habe die Untertitel vom ZDF gesehen.",
    "Untertitel",
    "Subtitles are on.",
    "Copyright",
    "Copyright 2024 Green Coding",
    "",
])
def test_ordinary_sentences_are_not_inventions(text):
    assert dictation.is_hallucination(text) is False


def test_dropped_segments_leave_the_rest_joined(service):
    service.handler = lambda r: reply(answer([
        segment(" Hello Max,"),
        segment(" Untertitel im Auftrag des ZDF, 2017"),
        segment("  this is  the   plan. "),
        segment(" ", no_speech_prob=0.1),
        segment(" la la la la", compression_ratio=3.1),
        segment(" Hmm.", no_speech_prob=0.8, avg_logprob=-1.4),
        segment("Thanks."),
    ]))
    result = dictation.transcribe(AUDIO, config())
    assert result["text"] == "Hello Max, this is the plan. Thanks."


@pytest.mark.parametrize("segments", [[], [segment(" Thanks for watching!")],
                                      [segment(" ", no_speech_prob=0.0)]])
def test_nothing_recognised_is_an_empty_text(service, segments):
    service.handler = lambda r: reply(answer(segments))
    assert dictation.transcribe(AUDIO, config())["text"] == ""


@pytest.mark.parametrize("answered, requested, expected", [
    ("de", "auto", "de"),
    ("yue", "auto", "yue"),
    ("de", "en", "de"),
    ("xx", "auto", "auto"),
    ("DE", "en", "en"),
    (5, "fr", "fr"),
    (None, "auto", "auto"),
    (_MISSING, "de", "de"),
])
def test_the_language_reported_back(service, answered, requested, expected):
    body = answer([segment("Hallo.")])
    if answered is _MISSING:
        del body["language"]
    else:
        body["language"] = answered
    service.handler = lambda r: reply(body)
    assert dictation.transcribe(AUDIO, config(language=requested))["language"] == expected


def test_an_answer_declared_as_json_is_read_as_well(service):
    service.handler = lambda r: httpx.Response(200, json=answer([segment("Hi.")]))
    assert dictation.transcribe(AUDIO, config())["text"] == "Hi."


# --- Errors -----------------------------------------------------------------------


def _raise(exc_type, message="boom"):
    def handler(request):
        raise exc_type(message, request=request)
    return handler


@pytest.mark.parametrize("exc_type", [httpx.ConnectError, httpx.ConnectTimeout,
                                      httpx.RemoteProtocolError, httpx.ReadError,
                                      httpx.PoolTimeout])
def test_a_service_that_cannot_be_reached(service, exc_type):
    service.handler = _raise(exc_type)
    with pytest.raises(dictation.Unreachable) as info:
        dictation.transcribe(AUDIO, config(), context=SECRET)
    assert str(info.value) == dictation.UNREACHABLE
    assert "whisper container" in str(info.value) and "first start" in str(info.value)


def test_a_service_that_does_not_answer_in_time(service, configure):
    configure(dictation_timeout_seconds=7)
    service.handler = _raise(httpx.ReadTimeout)
    with pytest.raises(dictation.Unreachable, match="within 7 seconds"):
        dictation.transcribe(AUDIO, config(), context=SECRET)


def test_a_fastapi_detail_is_passed_on(service):
    service.handler = lambda r: reply({"detail": "Method Not Allowed"}, status=405)
    with pytest.raises(dictation.Refused, match="^Method Not Allowed$"):
        dictation.transcribe(AUDIO, config())


def test_a_validation_error_passes_on_the_message_and_never_the_input(service):
    """FastAPI echoes the offending value as "input", and the query carries the
    end of the draft."""
    codes = ", ".join(f"'{c}'" for c in sorted(dictation.WHISPER_LANGUAGES))
    service.handler = lambda r: reply({"detail": [
        {"type": "enum", "loc": ["query", "language"], "msg": f"Input should be {codes}",
         "input": SECRET, "ctx": {"expected": SECRET}},
    ]}, status=422)
    with pytest.raises(dictation.Refused) as info:
        dictation.transcribe(AUDIO, config(), context=SECRET)
    detail = str(info.value)
    assert detail.startswith("Input should be 'af', 'am'")
    assert len(detail) <= dictation.MAX_DETAIL_CHARS and detail.endswith("...")
    assert SECRET not in detail


def test_several_validation_messages_become_sentences(service):
    service.handler = lambda r: reply({"detail": [{"msg": "Field required"},
                                                  {"msg": "Input should be a valid boolean."},
                                                  {"msg": 5}, "junk"]}, status=422)
    with pytest.raises(dictation.Refused,
                       match=r"^Field required\. Input should be a valid boolean\.$"):
        dictation.transcribe(AUDIO, config())


def test_a_plain_text_refusal_is_passed_on(service):
    service.handler = lambda r: httpx.Response(413, text="Request\n  Entity Too Large")
    with pytest.raises(dictation.Refused, match="^Request Entity Too Large$"):
        dictation.transcribe(AUDIO, config())


@pytest.mark.parametrize("response", [
    httpx.Response(400, text="   "),
    httpx.Response(404, json={"detail": []}),
    httpx.Response(400, json={"error": "something"}),
])
def test_a_refusal_with_nothing_to_say_still_says_something(service, response):
    service.handler = lambda r: response
    with pytest.raises(dictation.Refused, match=rf"HTTP {response.status_code}"):
        dictation.transcribe(AUDIO, config())


@pytest.mark.parametrize("response", [
    httpx.Response(500, text="Internal Server Error"),
    httpx.Response(503, text="overloaded"),
    httpx.Response(302, headers={"location": "https://elsewhere.example/asr"}),
    httpx.Response(200, text="Hello there."),
    httpx.Response(200, text="<html>not the service</html>"),
    httpx.Response(200, json=[]),
    httpx.Response(200, json={"text": "Hello", "language": "en"}),
    httpx.Response(200, json={"segments": "none"}),
    httpx.Response(200, json="Hello"),
])
def test_an_answer_that_cannot_be_read(service, response):
    service.handler = lambda r: response
    with pytest.raises(dictation.BadAnswer) as info:
        dictation.transcribe(AUDIO, config())
    assert str(info.value) == dictation.BAD_ANSWER
    # And the redirect was not followed. A service that answers with a 3xx does
    # not get to send the recording somewhere else.
    assert len(service.requests) == 1


def test_requests_neither_follow_redirects_nor_use_a_proxy(service):
    """Checked on the client itself, not only through a 3xx answer above. A
    proxy from HTTP_PROXY would carry the recording off the machine past the
    guard, which only ever looks at the configured host."""
    client = dictation._http()
    assert client.follow_redirects is False
    assert client.trust_env is False


def test_nothing_user_provided_reaches_an_error_or_a_log_line(service, caplog):
    """Not the audio, not the draft, not the vocabulary, not the transcript. The
    root logger is captured at DEBUG, so httpx's own request line is in there
    too, and it must have lost the query string that carries the prompt."""
    caplog.set_level(logging.DEBUG)
    words = ["Zyxwvut", "Qwertzuiop"]
    service.handler = lambda r: reply(answer([segment(SPOKEN)]))
    assert dictation.transcribe(AUDIO, config(words=words), context=SECRET)["text"] == SPOKEN

    failures = [
        _raise(httpx.ConnectError, SECRET), _raise(httpx.ReadTimeout, SECRET),
        lambda r: httpx.Response(500, text=SECRET),
        lambda r: httpx.Response(200, text=SECRET),
        lambda r: reply({"segments": SECRET}),
        lambda r: reply({"detail": [{"msg": "Bad value", "input": SECRET}]}, status=422),
    ]
    for handler in failures:
        service.handler = handler
        with pytest.raises(dictation.DictationError) as info:
            dictation.transcribe(AUDIO, config(words=words), context=SECRET)
        assert SECRET not in str(info.value)

    assert any(r.name == "httpx" for r in caplog.records), "httpx should have logged the request"
    assert any(r.name == "app.dictation" for r in caplog.records), "the failures should be logged"
    text = caplog.text
    for private in (SECRET, SPOKEN, "AUDIO-MARKER", "initial_prompt", *words):
        assert private not in text, private
    # The request line itself is kept, only without its query.
    assert f"POST {LOCAL}/asr " in text


def test_the_log_filter_leaves_other_requests_alone(caplog):
    """Only this module's own requests lose their query string in httpx's log."""
    caplog.set_level(logging.INFO, logger="httpx")
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(204))) as client:
        client.get("http://example.invalid/other?keep=this")
    assert "keep=this" in caplog.text


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


def test_a_service_on_a_public_address_is_refused_before_anything_is_sent(
        service, configure, monkeypatch):
    configure(dictation_url="https://whisper.example")
    _resolver(monkeypatch, "93.184.216.34")

    with pytest.raises(dictation.PublicHost) as info:
        dictation.transcribe(AUDIO, config(), context=SECRET)

    assert str(info.value) == dictation.PUBLIC_REFUSAL
    assert "allow_public_hosts" in str(info.value)
    assert service.requests == []


def test_one_public_address_among_private_ones_is_enough_to_refuse(service, configure, monkeypatch):
    configure(dictation_url="http://whisper.example:9000")
    _resolver(monkeypatch, "10.0.0.5", "2606:2800:220:1::1")
    with pytest.raises(dictation.PublicHost):
        dictation.transcribe(AUDIO, config())
    assert service.requests == []


@pytest.mark.parametrize("address", ["172.18.0.4", "10.1.2.3", "192.168.1.20", "127.0.0.1",
                                     "::1", "fd00::5", "::ffff:10.0.0.1"])
def test_a_service_on_this_machine_or_network_is_used(service, configure, monkeypatch, address):
    configure(dictation_url="http://whisper:9000")
    _resolver(monkeypatch, address)
    dictation.transcribe(AUDIO, config())
    assert len(service.requests) == 1


def test_allow_public_hosts_lets_a_public_service_through(service, configure, monkeypatch):
    configure(dictation_url="https://whisper.example", dictation_allow_public_hosts=True)
    looked_up = _resolver(monkeypatch, "93.184.216.34")
    dictation.transcribe(AUDIO, config())
    assert len(service.requests) == 1
    # Nothing is resolved at all when there is nothing to refuse.
    assert looked_up == []


def test_a_name_that_does_not_resolve_is_unreachable_not_refused(service, configure, monkeypatch):
    """In a compose stack with the `dictation` profile off, this is exactly what
    whisper:9000 does, and the helpful sentence is the container one."""
    configure(dictation_url="http://whisper:9000")
    looked_up = _resolver(monkeypatch, error=socket.gaierror(-2, "Name or service not known"))

    for _ in range(3):
        with pytest.raises(dictation.Unreachable) as info:
            dictation.transcribe(AUDIO, config())
        assert str(info.value) == dictation.UNREACHABLE
    # Remembered briefly, because the failing lookup is the slow part...
    assert looked_up == ["whisper"]
    assert service.requests == []

    # ...and only briefly. Once that has run out, a service that has come up in
    # the meantime is found and used.
    base = "http://whisper:9000"
    expires, verdict = dictation._guard_cache[base]
    assert verdict == "unresolved"
    assert 0 < expires - time.monotonic() <= dictation.UNRESOLVED_TTL
    dictation._guard_cache[base] = (0.0, verdict)
    _resolver(monkeypatch, "172.18.0.4")
    dictation.transcribe(AUDIO, config())
    assert len(service.requests) == 1


def test_the_verdict_is_cached_so_a_pause_costs_no_lookup(service, configure, monkeypatch):
    configure(dictation_url="http://whisper:9000")
    looked_up = _resolver(monkeypatch, "172.18.0.4")
    for _ in range(3):
        dictation.transcribe(AUDIO, config())
    assert looked_up == ["whisper"]
    assert len(service.requests) == 3
    expires, verdict = dictation._guard_cache["http://whisper:9000"]
    assert verdict == "private"
    assert dictation.UNRESOLVED_TTL < expires - time.monotonic() <= dictation.GUARD_TTL


def test_a_refusal_is_cached_too(service, configure, monkeypatch):
    configure(dictation_url="https://whisper.example")
    looked_up = _resolver(monkeypatch, "93.184.216.34")
    for _ in range(3):
        with pytest.raises(dictation.PublicHost):
            dictation.transcribe(AUDIO, config())
    assert looked_up == ["whisper.example"]


def test_use_transport_forgets_the_verdicts(service, configure, monkeypatch):
    configure(dictation_url="https://whisper.example")
    _resolver(monkeypatch, "93.184.216.34")
    with pytest.raises(dictation.PublicHost):
        dictation.transcribe(AUDIO, config())
    assert dictation._guard_cache
    dictation.use_transport(httpx.MockTransport(service))
    assert dictation._guard_cache == {}
