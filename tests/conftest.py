"""Pytest fixtures for meerail.

Unit tests (test_parse.py) need only the shared `core` deps. Integration tests
read through the server's HTTP API and write through `core.ingest` (the agent's
path), so they need both the server running and the database reachable; they are
skipped when the server isn't up.

Run with an interpreter that has core's deps — `agent/.venv/bin/python -m pytest`.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

import dbfixture
from helpers import SERVER, api, make_message, server_up

T0 = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)

# The suite truncates every table it can reach, so it must never be pointed at a
# real mailbox. A database whose name ends in this is declared disposable.
TEST_DB_SUFFIX = "_test"


def _database_name(url: str) -> str:
    try:
        return make_url(url).database or ""
    except Exception:
        return ""


def pytest_configure(config):
    """Abort the whole run — before collection — unless the target DB is disposable.

    Both halves matter, and they are separately wrong-able:

      * this process writes through `core.ingest` using DATABASE_URL, and
      * the assertions read back through the server at MEERAIL_URL, which has a
        DATABASE_URL of its own that we cannot see from here.

    Checking only the first would still let the suite truncate the test database
    while asserting against production, so we ask the server which database it is
    on and require the same disposable name. Defaults for both point at the
    production stack (localhost:5432 / localhost:8000), which is exactly the
    accident this exists to stop.
    """
    if os.environ.get("MEERAIL_ALLOW_DIRTY_DB") == "1":
        return

    from core.config import get_settings

    url = get_settings().database_url
    name = _database_name(url)
    if not name.endswith(TEST_DB_SUFFIX):
        pytest.exit(
            f"refusing to run: DATABASE_URL points at {name!r}, which is not a "
            f"*{TEST_DB_SUFFIX} database.\n"
            "This suite truncates every table it can reach — pointing it at the "
            "compose stack would destroy real mail.\n"
            "Run `make test` (isolated stack on ports 55432/18000), or set "
            "MEERAIL_ALLOW_DIRTY_DB=1 if you really mean it.",
            returncode=3,
        )

    # The server is optional (integration tests skip without it), but if one is
    # reachable it must not be a production one.
    code, body = _probe()
    if code == 200 and isinstance(body, dict):
        served = body.get("database")
        if served is not None and not str(served).endswith(TEST_DB_SUFFIX):
            pytest.exit(
                f"refusing to run: the server at {SERVER} is on database "
                f"{served!r}, not a *{TEST_DB_SUFFIX} one.\n"
                "Tests would seed the test database but assert against "
                "production. Run `make test`, or point MEERAIL_URL at the test "
                "server (default http://127.0.0.1:18000).",
                returncode=3,
            )


def _probe():
    """GET /healthz, treating an unreachable server as 'no server' rather than an error."""
    try:
        return api("GET", "/healthz")
    except Exception:
        return 0, None


@pytest.fixture(scope="session", autouse=True)
def clean_database():
    """Start every session from an empty schema.

    `make test` discards the volume, so a full run is already clean; this covers
    the other loop — re-running pytest repeatedly against a test stack left up —
    where leftover rows from the previous run would otherwise leak into
    assertions. RESTART IDENTITY also resets the sequences, so account ids are
    stable run to run.
    """
    from core.database import Base, engine, init_db

    init_db()  # idempotent; the server does this too, but tests may run first
    tables = [f'"{t.name}"' for t in Base.metadata.sorted_tables]
    if tables:
        with engine.begin() as conn:
            conn.execute(text(
                f"TRUNCATE TABLE {', '.join(tables)} RESTART IDENTITY CASCADE"
            ))
    yield


@pytest.fixture(scope="session")
def require_server():
    if not server_up():
        pytest.skip("meerail server not reachable at MEERAIL_URL (run: make test-up)")


@pytest.fixture
def account(require_server):
    """Create a throwaway account; delete it (cascades) on teardown."""
    email = f"pytest-{uuid.uuid4().hex[:10]}@example.com"
    acc = dbfixture.create_account(email, label="pytest")
    yield acc
    api("DELETE", f"/api/accounts/{acc['id']}")


def status_for(email: str) -> dict | None:
    code, body = api("GET", "/api/sync/status")
    assert code == 200
    return next((r for r in body["accounts"] if r["email"] == email), None)


def mailbox(email: str, role: str) -> dict | None:
    st = status_for(email)
    if not st:
        return None
    return next((m for m in st["mailboxes"] if m["role"] == role), None)


def ingest_one(email: str, account_id: int, token: str, frm: str = "sender@ex.com",
               uid: int = 1) -> tuple[int, str]:
    """Ingest one message (body contains `token`) into INBOX; return (message_id, rfc_message_id).

    Goes straight to the database via core.ingest, which is what the agent does —
    there is no ingest HTTP API any more.
    """
    mid = f"m-{uuid.uuid4().hex}@t"
    raw = make_message(f"<{mid}>", f"Subj {token}", frm, email, f"{token} body text", T0)
    dbfixture.ingest_raw_message(email, raw, uid=uid)
    _, r = api("GET", f"/api/search?q={token}&account_id={account_id}")
    return r["rows"][0]["id"], mid
