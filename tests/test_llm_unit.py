"""What actually goes out to a model provider, and what comes back in.

The two AI features are only as good as this layer: one wrong field in the
request body and a provider answers 400 for a configuration that is perfectly
correct, and one missed field in the response and a refusal reads as a crash.
Neither shows up in a test that mocks `llm.complete` itself, so the HTTP client
is faked here instead and the request is inspected as it leaves.

Three things are pinned:

  * the request bodies differ per provider in ways that are load-bearing —
    Anthropic requires `max_tokens` and rejects `temperature` outright, and the
    OpenAI shape has to stay minimal to work against the dozen servers that
    imitate it;
  * a refusal, a truncation and an auth failure are three different outcomes,
    and code above this has to be able to tell them apart; and
  * only the user-supplied endpoint goes through the SSRF guard. The two hosted
    providers are constants in the module, so guarding them would only be a way
    to break them when DNS hiccups.
"""

from __future__ import annotations

import json

import pytest

from app import aiprompts, llm


# --- A stand-in for httpx ----------------------------------------------------


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self.reason_phrase = text or "Fake"
        self._payload = payload if payload is not None else {}

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeClient:
    """Records every request and answers from a queue the test fills."""

    calls: list[dict] = []
    queue: list[FakeResponse] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, method, url, json=None, headers=None, **extra):
        FakeClient.calls.append({"method": method, "url": url, "json": json,
                                 "headers": headers or {}, "extra": extra,
                                 "client_kwargs": self.kwargs})
        if not FakeClient.queue:
            return FakeResponse(200, {})
        return FakeClient.queue.pop(0)


@pytest.fixture
def http(monkeypatch):
    FakeClient.calls = []
    FakeClient.queue = []
    monkeypatch.setattr(llm.httpx, "Client", FakeClient)
    return FakeClient


@pytest.fixture
def local_ok(monkeypatch):
    """An install that has opted into private endpoints, so a made-up base URL
    reaches the fake client instead of the resolver."""
    settings = llm.get_settings()
    monkeypatch.setattr(settings, "llm_allow_private_hosts", True)
    return settings


def anthropic(model="claude-opus-5"):
    return llm.Config(provider="anthropic", model=model, api_key="sk-ant-test")


def openai(model="gpt-test"):
    return llm.Config(provider="openai", model=model, api_key="sk-test")


def compatible(model="llama", base="http://127.0.0.1:11434/v1", key=""):
    return llm.Config(provider="compatible", model=model, api_key=key, base_url=base)


def answer_anthropic(text, stop="end_turn"):
    return FakeResponse(200, {"content": [{"type": "text", "text": text}],
                              "stop_reason": stop, "model": "claude-opus-5"})


def answer_openai(text, finish="stop"):
    return FakeResponse(200, {"choices": [{"message": {"content": text},
                                           "finish_reason": finish}],
                              "model": "gpt-test"})


# --- The request that leaves --------------------------------------------------


def test_anthropic_gets_the_headers_and_the_body_it_requires(http):
    """`max_tokens` is mandatory on /v1/messages and `anthropic-version` is how
    that API is versioned at all — a request missing either is a 400 before the
    model ever sees it."""
    http.queue.append(answer_anthropic("hello"))
    llm.complete(anthropic(), system="be brief", user="hi", max_tokens=1234)

    call = http.calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == "sk-ant-test"
    assert call["headers"]["anthropic-version"] == llm.ANTHROPIC_VERSION
    assert call["json"]["max_tokens"] == 1234
    assert call["json"]["system"] == "be brief"
    assert call["json"]["messages"] == [{"role": "user", "content": "hi"}]


def test_no_sampling_parameters_are_ever_sent(http):
    """Current Claude models reject `temperature`, `top_p` and `top_k` with a 400.

    Neither has anything to offer "write this query" or "summarise this thread",
    so nothing here sends one — to either provider. This is the regression that
    would make every Anthropic call fail while every OpenAI call kept working.
    """
    http.queue.extend([answer_anthropic("a"), answer_openai("b")])
    llm.complete(anthropic(), system="s", user="u")
    llm.complete(openai(), system="s", user="u")

    for call in http.calls:
        assert not {"temperature", "top_p", "top_k"} & set(call["json"])


def test_the_openai_body_stays_minimal(http):
    """Model and messages, and nothing else.

    Every optional field is one more thing some server in the `compatible` set
    rejects: OpenAI's own reasoning models 400 on `max_tokens` (they want
    `max_completion_tokens`), and several third-party servers 400 without it.
    Sending neither is the only shape that works everywhere.
    """
    http.queue.append(answer_openai("hello"))
    llm.complete(openai(), system="be brief", user="hi", max_tokens=999)

    call = http.calls[0]
    assert call["url"] == "https://api.openai.com/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    assert set(call["json"]) == {"model", "messages"}
    assert call["json"]["messages"][0] == {"role": "system", "content": "be brief"}


def test_a_local_endpoint_may_have_no_key_at_all(http, local_ok):
    """A model running on the same machine has nothing to authenticate, so an
    empty key is a valid configuration rather than a missing one — and no empty
    Authorization header goes out to confuse the server."""
    http.queue.append(answer_openai("hi"))
    llm.complete(compatible(), system="s", user="u")

    assert "Authorization" not in http.calls[0]["headers"]
    assert http.calls[0]["url"] == "http://127.0.0.1:11434/v1/chat/completions"


def test_redirects_are_not_followed(http):
    """A provider answering a POST with a 3xx is not one of ours, and following
    it would carry the API key to wherever it points."""
    http.queue.append(answer_anthropic("hi"))
    llm.complete(anthropic(), system="s", user="u")

    assert http.calls[0]["client_kwargs"]["follow_redirects"] is False


# --- Where a request is allowed to go ----------------------------------------


def test_only_a_custom_endpoint_is_checked_against_the_network(http, monkeypatch):
    """The hosted providers are constants in app/llm.py — nobody can aim them at
    a private address, so resolving them on every call would buy nothing and
    would fail the feature whenever DNS hiccuped. The endpoint somebody typed in
    is the one that has to be checked."""
    monkeypatch.setattr(llm.get_settings(), "llm_allow_private_hosts", False)

    http.queue.append(answer_anthropic("fine"))
    llm.complete(anthropic(), system="s", user="u")          # not resolved, not blocked

    with pytest.raises(llm.LLMError) as caught:
        llm.complete(compatible(), system="s", user="u")
    assert "llm_allow_private_hosts" in str(caught.value)


# --- What comes back ---------------------------------------------------------


def test_a_refusal_is_not_an_empty_answer(http):
    """Claude declining comes back as an ordinary 200 with nothing in `content`.

    Read content-first, that is indistinguishable from a broken response — so
    `stop_reason` is checked before the text, and the caller gets a decision it
    can report rather than a parse failure it cannot explain.
    """
    http.queue.append(FakeResponse(200, {"content": [], "stop_reason": "refusal"}))
    with pytest.raises(llm.Refused):
        llm.complete(anthropic(), system="s", user="u")


def test_an_openai_refusal_is_the_same_outcome(http):
    http.queue.append(FakeResponse(200, {"choices": [
        {"message": {"content": None, "refusal": "no thanks"}, "finish_reason": "stop"}]}))
    with pytest.raises(llm.Refused):
        llm.complete(openai(), system="s", user="u")


@pytest.mark.parametrize("cfg_fn, response", [
    (anthropic, answer_anthropic("half an ans", stop="max_tokens")),
    (openai, answer_openai("half an ans", finish="length")),
])
def test_running_out_of_room_is_reported_rather_than_hidden(http, cfg_fn, response):
    """The text is real but stops mid-sentence. The search writer parses what it
    gets back, so it has to be able to tell "the model wrote nonsense" from "the
    model was cut off"."""
    http.queue.append(response)
    reply = llm.complete(cfg_fn(), system="s", user="u")
    assert reply.truncated is True
    assert reply.text == "half an ans"


def test_a_rejected_key_says_so_specifically(http):
    """So the router can answer 400 — the person's field to fix — rather than
    502, which reads as "the provider is down" and invites a retry."""
    http.queue.append(FakeResponse(401, {"error": {"message": "bad key"}}))
    with pytest.raises(llm.AuthFailed) as caught:
        llm.complete(anthropic(), system="s", user="u")
    assert "bad key" in str(caught.value)


def test_an_error_body_is_read_in_either_shape(http):
    """Anthropic and OpenAI both nest the sentence under `error`, differently
    enough that a naive reader gets a status line instead of the reason."""
    http.queue.append(FakeResponse(400, {"type": "error",
                                         "error": {"type": "invalid_request_error",
                                                   "message": "model not found"}}))
    with pytest.raises(llm.LLMError) as caught:
        llm.complete(anthropic(), system="s", user="u")
    assert "model not found" in str(caught.value)


def test_an_empty_answer_is_an_error_not_an_empty_string(http):
    """Handing "" up to the search writer would put an empty query in the box and
    look like meerail losing it."""
    http.queue.append(answer_anthropic("   "))
    with pytest.raises(llm.LLMError):
        llm.complete(anthropic(), system="s", user="u")


def test_content_that_arrives_as_parts_is_joined(http):
    """Some OpenAI-compatible servers answer with a list of content parts rather
    than a string."""
    http.queue.append(FakeResponse(200, {"choices": [{"message": {"content": [
        {"type": "text", "text": "one "}, {"type": "text", "text": "two"}]},
        "finish_reason": "stop"}]}))
    assert llm.complete(openai(), system="s", user="u").text == "one two"


# --- Listing the models ------------------------------------------------------


def test_the_model_list_comes_from_the_provider(http):
    """Rather than from a table in this repository, which is wrong within weeks:
    models are released and retired on a schedule nothing here knows about."""
    http.queue.append(FakeResponse(200, {"data": [
        {"id": "claude-opus-5", "display_name": "Claude Opus 5"},
        {"id": "claude-haiku-4-5"},
    ]}))
    models = llm.list_models(anthropic())

    assert http.calls[0]["url"] == "https://api.anthropic.com/v1/models"
    assert models == [{"id": "claude-opus-5", "label": "Claude Opus 5"},
                      {"id": "claude-haiku-4-5", "label": "claude-haiku-4-5"}]


def test_an_endpoint_with_no_listing_is_not_a_broken_endpoint(http, local_ok):
    """A self-hosted server serving one model often has no /models route at all,
    and completions still work — so this has to be distinguishable from a wrong
    key, or the settings form would refuse a configuration that works."""
    http.queue.append(FakeResponse(404, {"error": {"message": "not found"}}))
    with pytest.raises(llm.ModelsUnsupported):
        llm.list_models(compatible())


def test_a_rejected_key_is_still_a_rejected_key_when_listing(http):
    """The one failure that must not be softened into ModelsUnsupported: it is
    the whole reason the settings form probes before it saves."""
    http.queue.append(FakeResponse(401, {"error": {"message": "nope"}}))
    with pytest.raises(llm.AuthFailed):
        llm.list_models(anthropic())


# --- Configuration -----------------------------------------------------------


def test_a_hosted_provider_needs_a_key_and_a_local_one_does_not():
    assert llm.Config(provider="anthropic", model="m").ready is False
    assert llm.Config(provider="anthropic", model="m", api_key="k").ready is True
    assert llm.Config(provider="compatible", model="m",
                      base_url="http://x/v1").ready is True
    # No model is no configuration, whichever provider it is.
    assert llm.Config(provider="compatible", model="", base_url="http://x/v1").ready is False


def test_an_unknown_provider_is_refused_where_it_is_built():
    with pytest.raises(llm.LLMError):
        llm.Config(provider="gemini-direct", model="m", api_key="k")


# --- Reading the search writer's answer --------------------------------------


def test_a_clean_json_answer_round_trips():
    out = aiprompts.parse_search_reply(
        json.dumps({"mode": "regex", "query": "(a|b)", "note": "either"}))
    assert out == {"mode": "regex", "query": "(a|b)", "note": "either"}


@pytest.mark.parametrize("raw, query", [
    ('```json\n{"mode":"keyword","query":":unread"}\n```', ":unread"),
    ('Sure! {"query": "invoice :from ada"} — hope that helps', "invoice :from ada"),
    (':unread :has-attachment', ":unread :has-attachment"),
])
def test_the_answer_is_read_back_however_it_is_dressed(raw, query):
    """"Reply with JSON and nothing else" is followed by most models most of the
    time, and this has to work on whichever one the person configured — a code
    fence or a sentence of preamble is not a failure worth showing them."""
    assert aiprompts.parse_search_reply(raw)["query"] == query


def test_an_unusable_mode_falls_back_rather_than_propagating():
    """`mode` reaches the regex switch. Anything that is not one of the two
    would leave the box in a state the UI has no name for."""
    assert aiprompts.parse_search_reply('{"mode":"fuzzy","query":"x"}')["mode"] == "keyword"


def test_prose_with_no_query_in_it_does_not_become_a_query():
    """A model that answers "could you say more?" must not have that pasted into
    the search box as a set of words to AND together."""
    out = aiprompts.parse_search_reply("I am not sure what you mean.\nCould you say more?")
    assert out["query"] == ""
    assert "not sure" in out["note"]


# --- Reading the reminder suggestion's answer --------------------------------


def test_a_suggested_moment_round_trips():
    out = aiprompts.parse_remind_reply(
        '{"when": "2026-08-14T09:00", "reason": "Ada said Thursday"}')
    assert out == {"when": "2026-08-14T09:00", "reason": "Ada said Thursday"}


def test_a_moment_is_found_in_prose_when_the_json_is_not_there():
    """The date is the one thing this needs, and a menu entry with no
    explanation beats an error about a format the person never saw."""
    assert aiprompts.parse_remind_reply(
        "I'd bring it back on 2026-08-14 09:00.")["when"] == "2026-08-14T09:00"


def test_seconds_and_offsets_are_trimmed_off():
    """The field this feeds is a local wall-clock instant. A trailing `:00` or a
    `+02:00` would make `new Date()` in the browser mean something else — the
    offset one silently in another timezone."""
    assert aiprompts.parse_remind_reply(
        '{"when": "2026-08-14T09:00:00+02:00"}')["when"] == "2026-08-14T09:00"


# --- Sending a picture -------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64


def test_an_image_is_sent_in_each_provider_own_shape(http, monkeypatch):
    """The two wire formats disagree about how a picture rides in a message, and
    a body in the wrong one is a 400 rather than a wrong answer — so both are
    pinned, ordering included: Anthropic asks for the image first, OpenAI puts
    the text first, and each vendor documents its own arrangement."""
    http.queue.extend([answer_anthropic("a receipt"), answer_openai("a receipt")])

    llm.complete(anthropic(), system="s", user="what is this?", images=[("image/png", PNG)])
    blocks = http.calls[0]["json"]["messages"][0]["content"]
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["media_type"] == "image/png"
    assert blocks[0]["source"]["type"] == "base64"
    assert blocks[-1] == {"type": "text", "text": "what is this?"}

    llm.complete(openai(), system="s", user="what is this?", images=[("image/png", PNG)])
    blocks = http.calls[1]["json"]["messages"][1]["content"]
    assert blocks[0] == {"type": "text", "text": "what is this?"}
    assert blocks[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_a_message_with_no_image_keeps_the_plain_string_body(http):
    """The list-of-blocks form is only for pictures. Some OpenAI-compatible
    servers accept only a bare string, and the three features that send no image
    must go on working against those."""
    http.queue.append(answer_openai("hi"))
    llm.complete(openai(), system="s", user="hi")
    assert http.calls[0]["json"]["messages"][1]["content"] == "hi"


def test_an_oversized_image_is_refused_here_rather_than_by_the_provider(http, monkeypatch):
    """A 400 from Anthropic saying "image exceeds maximum size" reaches the
    dialog as a sentence about somebody else's API. Said here, it can name the
    file's own size and the setting that raises the limit."""
    monkeypatch.setattr(llm.get_settings(), "llm_max_image_bytes", 100)
    with pytest.raises(llm.LLMError) as caught:
        llm.complete(anthropic(), system="s", user="u", images=[("image/png", b"x" * 200)])
    assert "llm_max_image_bytes" in str(caught.value)
    assert not http.calls          # and nothing was sent


def test_a_type_no_model_can_read_is_refused_by_name(http):
    with pytest.raises(llm.LLMError) as caught:
        llm.complete(anthropic(), system="s", user="u", images=[("image/tiff", PNG)])
    assert "image/tiff" in str(caught.value)
    assert not http.calls
