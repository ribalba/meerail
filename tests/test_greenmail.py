"""End-to-end test of the real agent against a live IMAP server (GreenMail).

Skipped unless: the meerail server is up, GreenMail is listening on 3143, and the
agent venv exists (agent/run.sh has been run once). Start GreenMail with:

  docker run -d --name greenmail -p 3143:3143 -p 3025:3025 \
    -e GREENMAIL_OPTS='-Dgreenmail.setup.test.all -Dgreenmail.hostname=0.0.0.0 \
    -Dgreenmail.auth.disabled' greenmail/standalone:2.1.0
"""

import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import status_for
from helpers import SERVER, api, make_message, port_open

pytest.importorskip("imapclient")
from imapclient import IMAPClient  # noqa: E402

AGENT_DIR = Path(__file__).resolve().parent.parent / "agent"
AGENT_PY = AGENT_DIR / ".venv" / "bin" / "python"
IMAP_PORT = 3143
T0 = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)


def _mb(email: str, imap_name: str) -> dict:
    st = status_for(email)
    return next(m for m in st["mailboxes"] if m["imap_name"] == imap_name)


def _run_agent(config_path: Path) -> None:
    subprocess.run([str(AGENT_PY), "main.py", "--once", "--config", str(config_path)],
                   cwd=str(AGENT_DIR), check=True, capture_output=True, timeout=120)


def _write_config(tmp_path: Path, email: str) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        f'server_url = "{SERVER}"\nagent_token = ""\npoll_interval = 30\nbatch_size = 200\n\n'
        f'[[account]]\nemail = "{email}"\nimap_host = "127.0.0.1"\nimap_port = {IMAP_PORT}\n'
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
    code, acc = api("POST", "/api/accounts", {"email": email, "label": "gmtest"})
    assert code == 201, acc

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
    code, acc = api("POST", "/api/accounts", {"email": email, "label": "wb"})
    assert code == 201

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
