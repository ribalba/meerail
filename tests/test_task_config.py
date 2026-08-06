"""What the Meerato private URL does and does not do on its way through the app.

The URL carries a token that creates tasks in somebody's Meerato. app/meerato.py
proxies every call from the server rather than letting the browser make it — for
CORS first, and so that the token stays here second. Two things used to give that
away for nothing:

  * the settings row held the string in plaintext, in the same database as the
    mail, so a backup or a stray `select * from settings` was a working
    credential; and
  * GET /api/tasks/config returned it verbatim to the page, which is exactly
    where the proxying exists to keep it from.

Driven through the live server, because the point is what crosses the wire and
what is on disk — neither of which a unit test can see. The masking rules
themselves are unit-tested in test_tasks_unit.py.
"""

import uuid

import pytest

import dbfixture
from helpers import api

TOKEN = "tok-" + uuid.uuid4().hex
PLAINTEXT_URL = f"https://meerato.example.invalid/api/create?token={TOKEN}"
SETTING_KEY = "meerato_url"


def _row():
    from core.models import Setting

    with dbfixture.session() as db:
        row = db.get(Setting, SETTING_KEY)
        return row.value if row else None


def _write_row(value):
    from core.models import Setting

    with dbfixture.session() as db:
        row = db.get(Setting, SETTING_KEY)
        if value is None:
            if row:
                db.delete(row)
        elif row:
            row.value = value
        else:
            db.add(Setting(key=SETTING_KEY, value=value))


@pytest.fixture
def saved_plaintext_url(require_server):
    """A row as an install written before this was encrypted still has it."""
    _write_row(PLAINTEXT_URL)
    yield
    _write_row(None)


def test_the_token_does_not_come_back_to_the_page(saved_plaintext_url):
    code, cfg = api("GET", "/api/tasks/config")

    assert code == 200
    assert cfg["configured"] is True
    assert TOKEN not in cfg["url"]
    # The host survives, which is the only reason anything comes back: the field
    # is edited in place to correct an address, not retyped whole.
    assert cfg["url"].startswith("https://meerato.example.invalid/")


def test_a_url_saved_before_this_was_encrypted_is_re_stored(saved_plaintext_url):
    """Reading it is what migrates it. Waiting for somebody to open Settings and
    press Save would leave the token sitting in the clear indefinitely on every
    install that already had one — which is all of them."""
    assert TOKEN in _row()          # the premise: plaintext on disk

    api("GET", "/api/tasks/config")

    stored = _row()
    assert stored is not None
    assert TOKEN not in stored
    assert stored != PLAINTEXT_URL


def test_nothing_configured_says_so(require_server):
    _write_row(None)
    code, cfg = api("GET", "/api/tasks/config")

    assert code == 200
    assert cfg == {"configured": False, "url": ""}


def test_a_ciphertext_no_key_can_read_is_not_mistaken_for_a_url(require_server):
    """secret_key changed under a stored value. Nothing can recover it, and the
    one thing that must not happen is the ciphertext being handed to
    parse_endpoint and surfacing as a puzzle further down."""
    _write_row("gAAAAABn" + "x" * 60)
    try:
        code, cfg = api("GET", "/api/tasks/config")

        assert code == 200
        assert cfg["configured"] is False

        # And the buttons that would use it fail as "not configured", which is
        # the state the UI already knows how to draw.
        assert api("GET", "/api/tasks/options")[0] == 409
    finally:
        _write_row(None)
