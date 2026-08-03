"""End-to-end test of the real agent against a live IMAP server (GreenMail).

Skipped unless: the meerail server is up, GreenMail is listening on 3143, and the
agent venv exists (agent/run.sh has been run once). Start GreenMail with:

  docker run -d --name greenmail -p 3143:3143 -p 3025:3025 \
    -e GREENMAIL_OPTS='-Dgreenmail.setup.test.all -Dgreenmail.hostname=0.0.0.0 \
    -Dgreenmail.auth.disabled' greenmail/standalone:2.1.0
"""

import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from pathlib import Path

import pytest

import dbfixture
from conftest import status_for
from helpers import SERVER, api, make_message, port_open

AGENT_DIR = Path(__file__).resolve().parent.parent / "agent"
sys.path.insert(0, str(AGENT_DIR))
import imaplib_compat  # noqa: E402,F401  (patches imaplib for imapclient on 3.14+)

pytest.importorskip("imapclient")
from imapclient import IMAPClient  # noqa: E402
AGENT_PY = AGENT_DIR / ".venv" / "bin" / "python"
IMAP_PORT = 3143
T0 = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)

# The agent writes to the database itself, so the config it gets must point at
# the same one the tests read through.
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://meerail:meerail@127.0.0.1:5432/meerail")
TIKA_URL = os.environ.get("TIKA_URL", "http://127.0.0.1:9998")


def _mb(email: str, imap_name: str) -> dict:
    st = status_for(email)
    return next(m for m in st["mailboxes"] if m["imap_name"] == imap_name)


def _run_agent(config_path: Path) -> None:
    # run.sh normally puts the repo root on the path so the agent can import the
    # shared `core` package; invoking main.py directly, we do it ourselves.
    env = dict(os.environ)
    repo_root = str(AGENT_DIR.parent)
    env["PYTHONPATH"] = repo_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = subprocess.run([str(AGENT_PY), "main.py", "--once", "--config", str(config_path)],
                          cwd=str(AGENT_DIR), capture_output=True, timeout=180, env=env, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"agent failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}")


def _write_config(tmp_path: Path, email: str) -> Path:
    config = tmp_path / "meerail.toml"
    config.write_text(
        f'[database]\nurl = "{DATABASE_URL}"\n\n'
        f'[agent]\ntika_url = "{TIKA_URL}"\n'
        'poll_interval = 30\nbatch_size = 200\n\n'
        f'[[agent.account]]\nemail = "{email}"\nimap_host = "127.0.0.1"\n'
        f'imap_port = {IMAP_PORT}\n'
        'imap_security = "plain"\nsmtp_host = "127.0.0.1"\nsmtp_port = 3025\n'
        f'smtp_security = "plain"\nusername = "{email}"\npassword = "x"\nverify_cert = false\n'
    )
    return config


def _seen(flags) -> bool:
    return any((f if isinstance(f, bytes) else str(f).encode()).lower() == b"\\seen" for f in flags)


@pytest.mark.skipif(not port_open("127.0.0.1", IMAP_PORT), reason="GreenMail not on :3143")
@pytest.mark.skipif(not AGENT_PY.exists(), reason="agent venv missing (run agent/run.sh once)")
def test_agent_syncs_and_prunes_from_real_imap(require_server, tmp_path):
    email = f"gm-{uuid.uuid4().hex[:10]}@example.com"
    acc = dbfixture.create_account(email, label="gmtest")

    try:
        # Seed a 2-message thread into GreenMail; first is already read.
        with IMAPClient("127.0.0.1", port=IMAP_PORT, ssl=False, use_uid=True) as c:
            c.login(email, "whatever")  # auth disabled -> creates the mailbox
            g1 = make_message("<g1@green>", "Review GREENALPHA", "carol@corp.com", email,
                              "let's review", T0)
            g2 = make_message("<g2@green>", "Re: Review GREENALPHA", "dave@corp.com", email,
                              "works for me", T0 + timedelta(hours=2),
                              in_reply_to="<g1@green>", refs=["<g1@green>"])
            c.append("INBOX", g1, flags=["\\Seen"])
            c.append("INBOX", g2)

        config = _write_config(tmp_path, email)

        _run_agent(config)
        inbox = _mb(email, "INBOX")
        assert inbox["total"] == 2
        assert inbox["unread"] == 1  # g1 was \Seen

        # Delete one message in GreenMail, re-sync -> server prunes it.
        with IMAPClient("127.0.0.1", port=IMAP_PORT, ssl=False, use_uid=True) as c:
            c.login(email, "whatever")
            c.select_folder("INBOX")
            uids = c.search(["ALL"])
            c.delete_messages([max(uids)])
            c.expunge()
        _run_agent(config)
        assert _mb(email, "INBOX")["total"] == 1
    finally:
        api("DELETE", f"/api/accounts/{acc['id']}")


@pytest.mark.skipif(not port_open("127.0.0.1", IMAP_PORT), reason="GreenMail not on :3143")
@pytest.mark.skipif(not AGENT_PY.exists(), reason="agent venv missing (run agent/run.sh once)")
def test_flag_writeback_reaches_real_imap(require_server, tmp_path):
    """Mark read in meerail -> agent -> the \\Seen flag appears on the IMAP server."""
    email = f"gm-wb-{uuid.uuid4().hex[:10]}@example.com"
    acc = dbfixture.create_account(email, label="wb")

    try:
        token = "WBTOKEN" + uuid.uuid4().hex[:6]
        with IMAPClient("127.0.0.1", port=IMAP_PORT, ssl=False, use_uid=True) as c:
            c.login(email, "x")
            c.append("INBOX", make_message("<wb1@green>", f"Writeback {token}", "carol@corp.com",
                                            email, "please read me", T0))  # unread
            c.select_folder("INBOX")
            uid = c.search(["ALL"])[-1]

        config = _write_config(tmp_path, email)
        _run_agent(config)                              # backfill

        _, sr = api("GET", f"/api/search?q={token}&account_id={acc['id']}")
        message_id = sr["rows"][0]["id"]
        api("POST", f"/api/messages/{message_id}/mark?seen=1")   # mark read in meerail

        _run_agent(config)                              # applies the flag to GreenMail

        with IMAPClient("127.0.0.1", port=IMAP_PORT, ssl=False, use_uid=True) as c:
            c.login(email, "x")
            c.select_folder("INBOX")
            flags = c.get_flags([uid]).get(uid, ())
        assert _seen(flags), f"\\Seen not set on the server (flags={flags})"
    finally:
        api("DELETE", f"/api/accounts/{acc['id']}")


@pytest.mark.skipif(not port_open("127.0.0.1", IMAP_PORT), reason="GreenMail not on :3143")
@pytest.mark.skipif(not AGENT_PY.exists(), reason="agent venv missing (run agent/run.sh once)")
def test_formatted_send_reaches_the_server_as_html(require_server, tmp_path):
    """The whole chain behind "Send as HTML email": the server builds the MIME,
    the agent relays it over real SMTP, and a real mail server hands back a
    message that is still HTML.

    Worth doing end-to-end rather than against the MIME in the outbound row,
    because the interesting failures are not in what we build — they are in what
    survives being put on the wire. Bare LF line endings, which is what
    ``as_string()`` writes and what ``sendmail`` will happily transmit, leave the
    boundaries unrecognisable to a strict parser; and a multipart/alternative
    does not reach the recipient intact at all, which is why there is only one
    body here to check.
    """
    email = f"gm-{uuid.uuid4().hex[:10]}@example.com"
    acc = dbfixture.create_account(email, label="gmsend")
    try:
        with IMAPClient("127.0.0.1", port=IMAP_PORT, ssl=False, use_uid=True) as c:
            c.login(email, "whatever")      # auth disabled -> creates the mailbox

        code, _ = api("POST", "/api/compose/send", {
            "account_id": acc["id"], "to": [email], "subject": "GREENHTML",
            "body_text": "# Heading\n\nSome **bold** text.",
            "body_html": "<html><body><h1>Heading</h1>"
                         "<p>Some <strong>bold</strong> text.</p></body></html>"})
        assert code == 200

        _run_agent(_write_config(tmp_path, email))

        with IMAPClient("127.0.0.1", port=IMAP_PORT, ssl=False, use_uid=True) as c:
            c.login(email, "whatever")
            c.select_folder("INBOX")
            uids = c.search(["SUBJECT", "GREENHTML"])
            assert uids, "the message never reached the mail server"
            raw = c.fetch([max(uids)], ["RFC822"])[max(uids)][b"RFC822"]

        msg = message_from_bytes(raw, policy=policy.default)
        assert msg.get_content_type() == "text/html", \
            f"structure changed in transit: {msg.get_content_type()}"
        assert not msg.is_multipart()
        assert "<strong>bold</strong>" in msg.get_content()
        # The markdown source is not on the wire, so it must not have leaked
        # into the body either — the recipient gets the rendering, once.
        assert "**bold**" not in msg.get_content()
    finally:
        api("DELETE", f"/api/accounts/{acc['id']}")
