"""/api/dictation through the live server: settings, validation, and real transcription.

Two halves. The first needs only the test stack. It covers the settings row, its
validation, and the refusals that happen before anything is sent. The test
server has DICTATION_URL set (docker-compose.test.yml), so the feature reads as
available whether or not a recogniser is running.

The second needs a real Whisper, which only starts under the compose profile
`dictation`, so run `COMPOSE_PROFILES=dictation make test`. Without it every
transcription answers 503, and those tests skip rather than fail, so a plain
`make test` (and CI) does not download a speech model. What they pin is what a
mock cannot. The real service takes the request exactly as it is built, query
string, prompt and all, and two seconds of silence come back as no text at all
rather than as a subtitle credit.

Every test starts and ends with no settings row, through `fresh`.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor

import pytest

import dbfixture
from app.dictation import (MAX_AUDIO_BYTES, MAX_CONTEXT_IN, UNREACHABLE, WHISPER_LANGUAGES)
from helpers import SERVER, api

KEY = "dictation"
DEFAULTS = {"available": True, "enabled": False, "language": "auto", "words": [],
            "languages": list(WHISPER_LANGUAGES)}
TURNED_OFF = "Dictation is turned off in Settings."


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


@pytest.fixture
def enabled(fresh):
    assert put({"enabled": True})[0] == 200


def put(body):
    return api("PUT", "/api/dictation/config", body)


def wav(seconds: float = 2.0, rate: int = 16_000) -> bytes:
    """Silence, as the browser would send it: 16 kHz mono 16-bit PCM in WAV."""
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return out.getvalue()


def _multipart(fields: list[tuple[str, str | tuple[str, bytes, str]]]) -> tuple[bytes, str]:
    """A multipart/form-data body, stdlib only. A field is text, or a
    (filename, bytes, content type) file."""
    boundary = "----meerail" + uuid.uuid4().hex
    parts = []
    for name, value in fields:
        if isinstance(value, tuple):
            filename, data, content_type = value
            head = (f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                    f"Content-Type: {content_type}\r\n\r\n")
        else:
            head = f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            data = value.encode("utf-8")
        parts.append(f"--{boundary}\r\n".encode() + head.encode() + data + b"\r\n")
    return b"".join(parts) + f"--{boundary}--\r\n".encode(), boundary


def _post(body: bytes, content_type: str):
    req = urllib.request.Request(SERVER + "/api/dictation/transcribe", data=body,
                                 method="POST", headers={"Content-Type": content_type})
    try:
        # Generous, for the first piece after the whisper container started.
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors="replace")


def transcribe(audio: bytes | None, language: str | None = None, context: str | None = None,
               filename: str = "dictation.wav", content_type: str = "audio/wav"):
    """POST /api/dictation/transcribe the way app.dictation.js does."""
    fields: list = []
    if audio is not None:
        fields.append(("audio", (filename, audio, content_type)))
    if language is not None:
        fields.append(("language", language))
    if context is not None:
        fields.append(("context", context))
    body, boundary = _multipart(fields)
    return _post(body, f"multipart/form-data; boundary={boundary}")


# --- Settings -------------------------------------------------------------------


def test_the_defaults_before_anything_is_saved(fresh):
    code, cfg = api("GET", "/api/dictation/config")
    assert code == 200
    assert cfg == DEFAULTS
    # Whisper's own order, which is not alphabetical.
    assert cfg["languages"][:3] == ["en", "zh", "de"] and cfg["languages"][-1] == "yue"
    # Reading writes nothing.
    assert _row() is None


def test_a_saved_setting_round_trips(fresh):
    code, cfg = put({"enabled": True, "language": " de ",
                     "words": [" meerail ", "Kubernetes", "MEERAIL", "Zoë"]})
    assert code == 200, cfg
    assert cfg == {**DEFAULTS, "enabled": True, "language": "de",
                   "words": ["meerail", "Kubernetes", "Zoë"]}
    assert api("GET", "/api/dictation/config") == (200, cfg)

    # A PUT is a patch, so what is left out keeps its value.
    code, cfg = put({"language": "auto"})
    assert code == 200
    assert cfg["enabled"] is True and cfg["words"] == ["meerail", "Kubernetes", "Zoë"]
    code, cfg = put({"enabled": False})
    assert cfg["enabled"] is False and cfg["language"] == "auto"


def test_the_vocabulary_keeps_the_order_it_was_given(fresh):
    code, cfg = put({"words": ["zebra", "apple", "Mango"]})
    assert code == 200
    assert cfg["words"] == ["zebra", "apple", "Mango"]


def test_the_vocabulary_limits_are_right_at_their_edges(fresh):
    # A hundred single characters are within both limits.
    hundred = [chr(0x4E00 + i) for i in range(100)]
    assert put({"words": hundred}) == (200, {**DEFAULTS, "words": hundred})
    # Exactly 400 characters, counting ", " between words.
    words = [f"{chr(97 + i)}{'x' * 54}" for i in range(6)] + ["g" + "x" * 57]
    assert put({"words": words})[1]["words"] == words
    assert put({"words": ["a" * 60]})[1]["words"] == ["a" * 60]


@pytest.mark.parametrize("body", [
    {"enabled": 1},
    {"enabled": "true"},
    {"enabled": None},
    {"language": None},
    {"words": None},
    {"language": "EN"},
    {"language": "en-US"},
    {"language": "english"},
    {"language": "xx"},
    {"language": ""},
    {"language": "x" * 17},
    {"language": 5},
    {"words": "meerail"},
    {"words": [7]},
    {"words": ["two\nlines"]},
    {"words": ["tab\there"]},
    {"words": [""]},
    {"words": ["   "]},
    {"words": ["a" * 61]},
    {"words": [chr(0x4E00 + i) for i in range(101)]},
    {"words": [f"{chr(97 + i)}{'x' * 54}" for i in range(6)] + ["g" + "x" * 58]},
])
def test_invalid_settings_are_refused_and_change_nothing(fresh, body):
    code, detail = put(body)
    assert code == 422, (body, code, detail)
    assert _row() is None
    assert api("GET", "/api/dictation/config") == (200, DEFAULTS)


@pytest.mark.parametrize("body, sentence", [
    ({"words": ["   "]}, "between 1 and 60 characters"),
    ({"words": ["two\nlines"]}, "line breaks"),
    ({"words": [f"{chr(97 + i)}{'x' * 54}" for i in range(6)] + ["g" + "x" * 58]},
     "at most 400 characters"),
    ({"language": "english"}, "is not a language Whisper knows"),
    ({"enabled": None}, "enabled cannot be null"),
])
def test_a_refusal_says_why(fresh, body, sentence):
    code, detail = put(body)
    assert code == 422
    assert sentence in json.dumps(detail, ensure_ascii=False), detail


def test_a_body_that_is_not_an_object_is_a_422(fresh):
    assert put(["enabled"])[0] == 422
    assert put("enabled")[0] == 422


def test_concurrent_first_writes_all_land(fresh):
    """Several saves at once on a row that does not exist yet. Each is a
    read-modify-write, and the first ever write has no row to lock, so all but
    one insert fail on the key and are retried. See
    app/routers/dictation.py::_save."""
    langs = ["de", "fr", "es", "it", "nl", "pl", "sv", "fi"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda code: put({"language": code}), langs))
    assert all(code == 200 for code, _ in results), results
    assert api("GET", "/api/dictation/config")[1]["language"] in langs


# --- What is refused before anything is sent ------------------------------------


def test_transcribing_when_turned_off_is_refused(fresh):
    code, body = transcribe(wav())
    assert code == 409
    assert body["detail"] == TURNED_OFF


@pytest.mark.parametrize("audio", [b"", None])
def test_an_empty_or_missing_recording_is_a_422(fresh, audio):
    code, body = transcribe(audio, language="auto")
    assert code == 422, body
    if audio is not None:
        assert body["detail"] == "The recording is empty."


def test_a_recording_over_the_limit_is_a_422(fresh):
    big = wav(0.1)
    big += b"\x00" * (MAX_AUDIO_BYTES + 1 - len(big))
    code, body = transcribe(big)
    assert code == 422
    assert body["detail"] == (f"That recording is {MAX_AUDIO_BYTES + 1} bytes. "
                              f"The limit is {MAX_AUDIO_BYTES}.")
    # Right at the limit is accepted, and turned away only for being off.
    assert transcribe(big[:-1]) == (409, {"detail": TURNED_OFF})


@pytest.mark.parametrize("audio", [
    b"OggS\x00\x02" + b"\x00" * 100,
    b"\x1aE\xdf\xa3" + b"\x00" * 100,          # WebM, which MediaRecorder makes
    b"RIFF\x24\x00\x00\x00AVI " + b"\x00" * 100,
    b"RIFF",
], ids=["ogg", "webm", "avi", "riff-only"])
def test_a_recording_that_is_not_wav_is_a_422(fresh, audio):
    code, body = transcribe(audio, content_type="audio/ogg")
    assert code == 422
    assert body["detail"] == "The recording must be a WAV file."


@pytest.mark.parametrize("language", ["english", "EN", "en-US", "xx", "   "])
def test_an_unknown_language_is_a_422(fresh, language):
    code, body = transcribe(wav(), language=language)
    assert code == 422, body
    assert "is not a language Whisper knows" in body["detail"]


def test_a_context_that_is_too_long_is_a_422(fresh):
    code, body = transcribe(wav(), context="x" * (MAX_CONTEXT_IN + 1))
    assert code == 422
    assert body["detail"] == (f"The draft text sent with the recording is {MAX_CONTEXT_IN + 1} "
                              f"characters. The limit is {MAX_CONTEXT_IN}.")
    # Counted in characters, not bytes, and right at the limit is fine.
    assert transcribe(wav(), context="ü" * MAX_CONTEXT_IN) == (409, {"detail": TURNED_OFF})


def test_an_empty_language_is_refused_and_an_empty_context_is_none(fresh):
    """FastAPI passes an empty form field through as "", which is not a
    language. An empty context is simply no context."""
    code, body = transcribe(wav(), language="")
    assert code == 422 and "is not a language Whisper knows" in body["detail"]
    assert transcribe(wav(), context="") == (409, {"detail": TURNED_OFF})


def test_a_body_that_is_not_multipart_is_refused(fresh):
    assert api("POST", "/api/dictation/transcribe", {"audio": "UklGRg=="})[0] == 422
    code, _ = _post(b"--nope\r\nnot a form\r\n", "multipart/form-data; boundary=other")
    assert code in (400, 422)


def test_without_the_service_it_is_a_503_that_says_so(enabled):
    """With the `dictation` profile off, whisper:9000 does not resolve. That is
    a service that is not running, and the sentence is the one about the
    container."""
    code, body = transcribe(wav(), context="Hallo Max,")
    if code == 200:
        pytest.skip("whisper is running, so there is no 503 to see")
    assert code == 503, body
    assert body["detail"] == UNREACHABLE


# --- Transcribing with a real Whisper -------------------------------------------


def live_transcribe(*args, **kwargs):
    code, body = transcribe(*args, **kwargs)
    if code == 503:
        pytest.skip("whisper not running. COMPOSE_PROFILES=dictation make test runs this")
    assert code == 200, body
    return body


def test_live_silence_is_no_text(enabled):
    body = live_transcribe(wav(2.0))
    assert body["text"] == ""
    assert body["language"] == "auto" or body["language"] in WHISPER_LANGUAGES


def test_live_a_fixed_language_and_a_prompt_go_through(enabled):
    """The prompt travels in the query string, with umlauts and punctuation in
    it, and a language from Whisper's table is accepted by the service as is."""
    assert put({"language": "de", "words": ["meerail", "Kubernetes", "Zoë"]})[0] == 200
    body = live_transcribe(wav(2.0), context="Hallo Max,\n\nwie geht's? Grüße & Dank: 100 %")
    assert body == {"text": "", "language": "de"}
    # And a per-piece language wins over the stored one.
    body = live_transcribe(wav(1.0), language="fr")
    assert body == {"text": "", "language": "fr"}
