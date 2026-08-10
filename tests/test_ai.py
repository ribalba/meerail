"""The AI features from the outside: what is stored, what is returned, and what
gets sent to a provider.

Two halves, and both are about a boundary rather than about an answer:

  * the key. It is a credential for a metered third-party service, sitting in
    the same database as the mail. It is encrypted at rest and never returned to
    the browser — every call is made by the server precisely so it does not have
    to be. Neither is visible from a test that only checks the feature works,
    which is why they are checked here.
  * the thread. "Ask about this conversation" is the one button in meerail that
    sends mail out of the building, so what it composes is pinned: which
    messages, in which order, with which headers, and what it does with a thread
    too long to fit.

No model is called. Nothing here needs one: the questions are about the app's own
behaviour, and llm's request shapes are covered in test_llm_unit.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest

import dbfixture
from helpers import api, make_message

T0 = datetime(2026, 3, 2, 9, 30)

PROVIDER_KEY = "ai_provider"
MODEL_KEY = "ai_model"
KEY_ROW = "ai_key_anthropic"
SECRET = "sk-ant-" + uuid.uuid4().hex


def _rows() -> dict[str, str]:
    from core.models import Setting

    with dbfixture.session() as db:
        return {k: (db.get(Setting, k).value if db.get(Setting, k) else "")
                for k in (PROVIDER_KEY, MODEL_KEY, KEY_ROW)}


def _write(key: str, value: str | None) -> None:
    from core.models import Setting

    with dbfixture.session() as db:
        row = db.get(Setting, key)
        if value is None:
            if row:
                db.delete(row)
        elif row:
            row.value = value
        else:
            db.add(Setting(key=key, value=value))


@pytest.fixture
def configured(require_server):
    """An install with a saved Anthropic key, written the way the router writes
    one — encrypted. Torn down whichever way the test goes, because these rows
    are install-wide and would otherwise leak into every later test."""
    from app.security import encrypt_secret

    _write(PROVIDER_KEY, "anthropic")
    _write(MODEL_KEY, "claude-opus-5")
    _write(KEY_ROW, encrypt_secret(SECRET))
    yield
    for key in (PROVIDER_KEY, MODEL_KEY, KEY_ROW):
        _write(key, None)


@pytest.fixture
def unconfigured(require_server):
    for key in (PROVIDER_KEY, MODEL_KEY, KEY_ROW):
        _write(key, None)
    yield
    for key in (PROVIDER_KEY, MODEL_KEY, KEY_ROW):
        _write(key, None)


# --- What the browser is told ------------------------------------------------


def test_the_key_never_comes_back_to_the_page(configured):
    """This module proxies every call so the key stays on the server. Returning
    it on the way to drawing a settings form would hand it to whatever reads the
    DOM — an extension, a screenshot, the browser's own cache — and give that up
    for nothing.

    Checked against the whole serialized body rather than a named field, because
    the failure this guards against is a *new* field leaking it, not the one
    somebody remembered to check.
    """
    code, body = api("GET", "/api/ai/config")
    assert code == 200
    assert SECRET not in repr(body)
    # What the form actually needs: that there is one, not what it is.
    assert body["keys"]["anthropic"] is True
    assert body["enabled"] is True
    assert body["model"] == "claude-opus-5"


def test_the_key_is_not_stored_in_the_clear(configured):
    """A backup, or a stray `select * from settings`, should not be a working
    key on somebody else's account."""
    stored = _rows()[KEY_ROW]
    assert stored
    assert SECRET not in stored

    from app.security import decrypt_secret
    assert decrypt_secret(stored) == SECRET


def test_nothing_configured_reads_as_off(unconfigured):
    """Which is what takes the robot buttons off the toolbar — an install with no
    provider should not carry a button that can only fail."""
    code, body = api("GET", "/api/ai/config")
    assert code == 200
    assert body["enabled"] is False
    assert body["keys"] == {"anthropic": False, "openai": False, "compatible": False}


def test_the_features_refuse_rather_than_fail_when_nothing_is_set_up(unconfigured):
    """409, not 500. The buttons are hidden, so reaching any of these means the
    setting was cleared in another tab — a state, not a crash."""
    code, _ = api("POST", "/api/ai/search", {"description": "the invoice from ada"})
    assert code == 409
    code, _ = api("POST", "/api/ai/thread", {"message_id": 1})
    assert code == 409
    code, _ = api("POST", "/api/ai/remind-suggest",
                  {"message_id": 1, "now": "2026-08-09T14:32"})
    assert code == 409
    code, _ = api("POST", "/api/ai/attachment", {"attachment_id": 1})
    assert code == 409


def test_a_clock_the_server_cannot_read_is_refused_before_a_model_is_called(configured):
    """The suggestion is a moment on the reader's calendar, worked out against
    the local time they send. A malformed one would be compared against nothing,
    so it fails validation rather than reaching the model."""
    code, _ = api("POST", "/api/ai/remind-suggest",
                  {"message_id": 1, "now": "next tuesday"})
    assert code == 422


def test_an_unknown_provider_is_refused(require_server):
    code, _ = api("PUT", "/api/ai/config",
                  {"provider": "definitely-not-a-provider", "model": "m", "api_key": "k"})
    assert code == 400


def test_a_custom_endpoint_without_a_base_url_is_refused(require_server):
    """`compatible` is a URL and nothing else — there is no default to fall back
    to, and saving one without it would store a configuration that cannot work."""
    code, body = api("PUT", "/api/ai/config",
                     {"provider": "compatible", "model": "llama", "api_key": ""})
    assert code == 400
    assert "base URL" in body["detail"]


def test_saving_without_a_model_is_refused(require_server):
    code, body = api("PUT", "/api/ai/config",
                     {"provider": "anthropic", "model": "", "api_key": "sk-x"})
    assert code == 400
    assert "model" in body["detail"].lower()


def test_turning_it_off_forgets_every_key(configured):
    code, body = api("DELETE", "/api/ai/config")
    assert code == 200
    assert body["enabled"] is False
    assert _rows() == {PROVIDER_KEY: "", MODEL_KEY: "", KEY_ROW: ""}


# --- What would be sent ------------------------------------------------------


def _thread(email: str, count: int, subject: str) -> list[str]:
    """Ingest `count` messages of one conversation, oldest first."""
    root = f"root-{uuid.uuid4().hex}@t"
    ids = [root]
    dbfixture.ingest_raw_message(email, make_message(
        f"<{root}>", subject, "Ada Lovelace <ada@example.com>", email,
        "First message body.", T0), uid=1)
    for n in range(1, count):
        mid = f"reply{n}-{uuid.uuid4().hex}@t"
        ids.append(mid)
        dbfixture.ingest_raw_message(email, make_message(
            f"<{mid}>", f"Re: {subject}", "Grace Hopper <grace@example.com>", email,
            f"Reply number {n}.", T0 + timedelta(hours=n),
            in_reply_to=f"<{root}>", refs=[f"<{root}>"], cc="carol@example.com"),
            uid=1 + n)
    return ids


def _newest(db, email: str):
    from core.models import Account, Message

    return (db.query(Message).join(Account, Account.id == Message.account_id)
            .filter(Account.email == email)
            .order_by(Message.date_sent.desc()).first())


def _render(email: str, max_chars: int = 240_000):
    """Render the account's whole conversation the way POST /thread would."""
    from app import threadtext

    with dbfixture.session() as db:
        newest = _newest(db, email)
        return threadtext.render(db, threadtext.thread_messages(db, newest), max_chars)


def test_the_whole_conversation_goes_oldest_first_with_its_headers(account):
    """The dialog promises "the whole conversation", and a model reading a thread
    out of order draws the wrong conclusion about what was decided last."""
    _thread(account["email"], 3, "Board meeting")
    text, info = _render(account["email"])

    assert info["messages"] == 3 and info["included"] == 3 and info["dropped"] == 0
    assert text.index("First message body.") < text.index("Reply number 1.")
    assert text.index("Reply number 1.") < text.index("Reply number 2.")
    assert "--- Message 1 of 3 ---" in text
    assert "From: Ada Lovelace <ada@example.com>" in text
    assert "Subject: Board meeting" in text
    assert "Date: 2026-03-02 09:30 UTC" in text
    assert "Cc: carol@example.com" in text


def test_bcc_is_not_passed_on(account):
    """The one header whose entire purpose is not to be shared onward. It adds
    nothing to a summary, and this is mail going to a third party."""
    email = account["email"]
    mid = f"b-{uuid.uuid4().hex}@t"
    raw = make_message(f"<{mid}>", "Quiet one", "ada@example.com", email, "hello", T0)
    raw = raw.replace(b"To: ", b"Bcc: secret-watcher@example.com\r\nTo: ", 1)
    dbfixture.ingest_raw_message(email, raw)

    text, _ = _render(email)
    assert "secret-watcher@example.com" not in text


def test_attachments_are_named_but_not_sent(account):
    """A filename is context worth having — "the report you attached" is a thing
    threads say. The bytes are not: they would multiply the size of every call
    and none of the two features needs them."""
    email = account["email"]
    dbfixture.ingest_raw_message(email, make_message(
        f"<att-{uuid.uuid4().hex}@t>", "With a file", "ada@example.com", email,
        "See attached.", T0, text_attachment="the secret contents of the file"))

    text, _ = _render(email)
    assert "Attachments: notes.txt" in text
    assert "the secret contents of the file" not in text


def test_a_thread_too_long_keeps_its_recent_end_and_says_what_it_dropped(account):
    """Every question is about the recent end — "draft a reply" is about the last
    message — so that is what survives the budget. What was left out is reported
    rather than silently dropped: a summary that covers half a conversation is
    the one failure the person cannot see for themselves.
    """
    _thread(account["email"], 6, "Long one")
    # Small enough that only the last message or two can fit.
    text, info = _render(account["email"], max_chars=400)

    assert info["dropped"] > 0
    assert info["included"] + info["dropped"] == info["messages"] == 6
    assert "Reply number 5." in text          # the newest survived
    assert "First message body." not in text  # the oldest did not
    assert "would not fit" in text            # and the model is told so


def test_a_message_with_no_thread_is_its_own_conversation(account):
    """The same rule the reader and the search results use, so "the whole thread"
    means on the wire what it means on screen."""
    email = account["email"]
    dbfixture.ingest_raw_message(email, make_message(
        None, "No message id", "ada@example.com", email, "standalone", T0))

    text, info = _render(email)
    assert info["messages"] == 1
    assert "standalone" in text


# --- What of an attachment can be sent ---------------------------------------
#
# The robot beside a file has to be honest about what it is looking at: a
# document goes as the text Tika pulled out of it, a picture goes as a picture,
# and a zip gets no robot at all — because a model handed a filename and nothing
# else will describe the filename, at length and convincingly.


def _att(**kw):
    from core.models import Attachment

    return Attachment(**{"filename": "f", "content_type": "application/pdf",
                         "size_bytes": 10, "extracted_text": None, "content": None, **kw})


def test_a_document_goes_as_the_text_that_was_extracted_from_it(require_server):
    from app.routers.ai import _attachment_source

    text, images = _attachment_source(_att(extracted_text="Invoice 42\nTotal £91.00",
                                           content=b"%PDF-1.4 binary"))
    assert text == "Invoice 42\nTotal £91.00"
    assert images == []          # the bytes stay here; only the words travel


def test_a_long_document_is_cut_and_says_so(require_server, monkeypatch):
    """Silently sending half a contract and answering as if it were whole is the
    failure the person cannot see."""
    from app.routers.ai import _attachment_source
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "llm_max_attachment_chars", 50)
    text, _ = _attachment_source(_att(extracted_text="x" * 500))
    assert text.startswith("x" * 50)
    assert "was cut here" in text


def test_a_picture_goes_as_a_picture(require_server):
    from app.routers.ai import _attachment_source

    text, images = _attachment_source(
        _att(content_type="image/png", content=b"\x89PNG\r\n\x1a\n"))
    assert text == ""
    assert images == [("image/png", b"\x89PNG\r\n\x1a\n")]


def test_an_extraction_that_found_nothing_does_not_stand_in_for_the_picture(require_server):
    """Tika stores an empty string for a scan it could not read. Treated as
    "there is text", that would send an empty file and ask what it says."""
    from app.routers.ai import _attachment_source

    _, images = _attachment_source(
        _att(content_type="image/jpeg", extracted_text="   ", content=b"\xff\xd8\xff"))
    assert images == [("image/jpeg", b"\xff\xd8\xff")]


def test_a_plain_text_file_is_read_directly(require_server):
    """Nothing extracts those, because there is nothing to extract."""
    from app.routers.ai import _attachment_source

    text, images = _attachment_source(
        _att(content_type="text/csv", content="a,b\n1,2".encode()))
    assert text == "a,b\n1,2"
    assert images == []


def test_a_file_with_nothing_readable_in_it_is_refused(require_server):
    """Rather than sent as a name for the model to invent an answer about."""
    from fastapi import HTTPException
    from app.routers.ai import _attachment_source

    with pytest.raises(HTTPException) as caught:
        _attachment_source(_att(content_type="application/zip", content=b"PK\x03\x04"))
    assert caught.value.status_code == 409
    assert "nothing readable" in caught.value.detail


def test_an_attachment_whose_bytes_were_pruned_is_refused(require_server):
    """Outside the content window the row keeps the name and size and nothing
    else — so the robot has nothing to read and says which of the two it is."""
    from fastapi import HTTPException
    from app.routers.ai import _attachment_source

    with pytest.raises(HTTPException) as caught:
        _attachment_source(_att(content_type="image/png", content=None))
    assert caught.value.status_code == 409
    assert "content window" in caught.value.detail


def test_the_reader_is_told_which_attachments_have_text(account):
    """`has_text` is what decides whether a robot is drawn beside a chip, and
    only the server can see it — the extracted text is deliberately not sent to
    the browser."""
    email = account["email"]
    dbfixture.ingest_raw_message(email, make_message(
        f"<ht-{uuid.uuid4().hex}@t>", "With a file", "ada@example.com", email,
        "See attached.", T0, text_attachment="the quick brown fox jumps"))
    dbfixture.extract_all()

    with dbfixture.session() as db:
        message_id = _newest(db, email).id      # read inside the session; the row detaches on exit
    code, body = api("GET", f"/api/messages/{message_id}")
    assert code == 200
    att = body["attachments"][0]
    assert att["has_text"] is True
    # And the text itself stays on the server — the chip needs a boolean, not
    # the contents of the file.
    assert "the quick brown fox" not in repr(body)


def test_deleted_mail_is_not_sent(account):
    """`_readable` guards the endpoint, and this guards the rest of the thread:
    mail emptied out of Trash must not still be able to leave the building as
    part of a conversation somebody asked about."""
    email = account["email"]
    _thread(email, 3, "Half deleted")

    from core.models import Account, Message, MessageLocation
    with dbfixture.session() as db:
        oldest = (db.query(Message).join(Account, Account.id == Message.account_id)
                  .filter(Account.email == email)
                  .order_by(Message.date_sent.asc()).first())
        for loc in db.query(MessageLocation).filter_by(message_pk=oldest.id):
            loc.deleted = True

    text, info = _render(email)
    assert info["messages"] == 2
    assert "First message body." not in text
